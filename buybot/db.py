"""SQLite storage. One file, WAL mode, used from the event loop's thread only.

The watcher writes a tick's outbox rows and its new last block in one
transaction, so a restart neither loses nor repeats a buy. Big integers
(supplies, thresholds in wei) are stored as TEXT: they overflow SQLite's int64.
"""

import json
import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    address TEXT PRIMARY KEY,
    curve TEXT NOT NULL,
    deployer TEXT NOT NULL,
    pair TEXT NOT NULL,
    quote_symbol TEXT NOT NULL,
    quote_decimals INTEGER NOT NULL,
    quote_kind TEXT NOT NULL,
    name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    decimals INTEGER NOT NULL,
    total_supply TEXT NOT NULL,
    threshold TEXT NOT NULL,
    pool_id TEXT NOT NULL,
    curve_k TEXT,
    phantom TEXT,
    graduated INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    token TEXT,
    min_usd REAL NOT NULL DEFAULT 0,
    emoji TEXT NOT NULL DEFAULT '🟢',
    step_usd REAL NOT NULL DEFAULT 10,
    media_kind TEXT,
    media_file_id TEXT,
    pro_until INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    sells INTEGER NOT NULL DEFAULT 0,
    chart INTEGER NOT NULL DEFAULT 1,
    lang TEXT NOT NULL DEFAULT 'en',
    added_by INTEGER,
    kind TEXT,
    thread_id INTEGER,
    whale_usd REAL NOT NULL DEFAULT 1000
);
CREATE INDEX IF NOT EXISTS chats_token ON chats(token);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    dedupe TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE (chat_id, dedupe)
);
CREATE TABLE IF NOT EXISTS trades (
    token TEXT NOT NULL,
    uid TEXT NOT NULL,       -- tx:logIndex
    ts INTEGER NOT NULL,     -- block time
    price REAL NOT NULL,     -- whole quote units per whole token, right after the trade
    quote REAL NOT NULL,     -- trade size in whole quote units
    side INTEGER NOT NULL,   -- 1 buy, -1 sell
    wallet TEXT,             -- the buyer or seller, when known
    PRIMARY KEY (token, uid)
);
CREATE INDEX IF NOT EXISTS trades_token_ts ON trades(token, ts);
CREATE TABLE IF NOT EXISTS chat_stats (
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,       -- UTC date, YYYY-MM-DD
    buys INTEGER NOT NULL DEFAULT 0,
    sells INTEGER NOT NULL DEFAULT 0,
    usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, day)
);
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    refunded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    url TEXT NOT NULL,
    days INTEGER NOT NULL,
    price INTEGER NOT NULL,
    status TEXT NOT NULL,
    starts_at INTEGER,
    ends_at INTEGER,
    charge_id TEXT,
    created_at INTEGER NOT NULL
);
"""

CHAT_SETTINGS = {"title", "token", "min_usd", "emoji", "step_usd", "media_kind", "media_file_id",
                 "active", "sells", "chart", "lang", "added_by", "kind", "thread_id", "whale_usd"}
# Columns added after the first release: (table, column, definition). A database
# made before them gets them on start.
MIGRATIONS = [("chats", "sells", "INTEGER NOT NULL DEFAULT 0"),
              ("chats", "chart", "INTEGER NOT NULL DEFAULT 1"),
              ("chats", "lang", "TEXT NOT NULL DEFAULT 'en'"),
              ("chats", "added_by", "INTEGER"),
              ("chats", "kind", "TEXT"),
              ("chats", "thread_id", "INTEGER"),
              ("chats", "whale_usd", "REAL NOT NULL DEFAULT 1000"),
              ("trades", "wallet", "TEXT")]


@dataclass
class Token:
    address: str
    curve: str
    deployer: str
    pair: str
    quote_symbol: str
    quote_decimals: int
    quote_kind: str  # "eth" | "usd" | "other"
    name: str
    symbol: str
    decimals: int
    total_supply: int
    threshold: int
    pool_id: str
    curve_k: int | None   # quoteReserve * tokenReserve of the curve (constant product)
    phantom: int | None   # the curve's virtual quote reserve, not counted toward graduation
    graduated: bool
    added_at: int


@dataclass
class Chat:
    chat_id: int
    title: str
    token: str | None
    min_usd: float
    emoji: str
    step_usd: float
    media_kind: str | None
    media_file_id: str | None
    pro_until: int
    active: bool
    created_at: int
    sells: bool = False  # post sells too
    chart: int = 1       # the chart with each post: 0 off, 1 animated, 2 still
    lang: str = "en"     # posts and answers in the chat
    added_by: int | None = None  # who added the bot (or claimed the chat as its admin)
    kind: str | None = None      # "channel", "supergroup", "group"
    thread_id: int | None = None # the forum topic posts go to (set by /track sent in it)
    whale_usd: float = 1000.0    # buys from this many dollars get the 🐋 mark; 0 turns it off

    def is_pro(self, now: float | None = None) -> bool:
        return self.pro_until > (time.time() if now is None else now)


@dataclass
class OutboxRow:
    id: int
    chat_id: int
    kind: str
    payload: dict
    created_at: int
    attempts: int


@dataclass
class Ad:
    id: int
    user_id: int
    text: str
    url: str
    days: int
    price: int
    status: str  # pending | approved | paid | rejected
    starts_at: int | None
    ends_at: int | None


def _token(r: sqlite3.Row) -> Token:
    return Token(r["address"], r["curve"], r["deployer"], r["pair"], r["quote_symbol"],
                 r["quote_decimals"], r["quote_kind"], r["name"], r["symbol"], r["decimals"],
                 int(r["total_supply"]), int(r["threshold"]), r["pool_id"],
                 int(r["curve_k"]) if r["curve_k"] else None,
                 int(r["phantom"]) if r["phantom"] is not None else None,
                 bool(r["graduated"]), r["added_at"])


def _chat(r: sqlite3.Row) -> Chat:
    return Chat(r["chat_id"], r["title"], r["token"], r["min_usd"], r["emoji"], r["step_usd"],
                r["media_kind"], r["media_file_id"], r["pro_until"], bool(r["active"]),
                r["created_at"], bool(r["sells"]), int(r["chart"]), r["lang"], r["added_by"], r["kind"],
                r["thread_id"], r["whale_usd"])


def _ad(r: sqlite3.Row) -> Ad:
    return Ad(r["id"], r["user_id"], r["text"], r["url"], r["days"], r["price"], r["status"],
              r["starts_at"], r["ends_at"])


class Db:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def backup(self, folder: str, keep: int = 7, now: float | None = None) -> str | None:
        """A copy of the database for today (UTC) in `folder`, made with SQLite's own
        backup, which is safe while the bot writes; the `keep` newest are kept.
        Returns the new file, or None when today's copy already exists."""
        from pathlib import Path
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() if now is None else now))
        target = Path(folder) / f"bot-{day}.db"
        if target.exists():
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        dst = sqlite3.connect(tmp)
        try:
            self.conn.backup(dst)
        finally:
            dst.close()
        tmp.replace(target)
        for old in sorted(Path(folder).glob("bot-*.db"))[:-keep]:
            old.unlink()
        return str(target)

    # --- meta ---
    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def last_block(self) -> int | None:
        v = self.get_meta("last_block")
        return int(v) if v is not None else None

    def set_last_block(self, block: int) -> None:
        self.set_meta("last_block", str(block))

    def dm_chat(self, user_id: int) -> int | None:
        """The channel this person last set up from the private chat."""
        v = self.get_meta(f"dm_chat:{user_id}")
        return int(v) if v is not None else None

    def set_dm_chat(self, user_id: int, chat_id: int) -> None:
        self.set_meta(f"dm_chat:{user_id}", str(chat_id))

    def user_lang(self, user_id: int) -> str | None:
        return self.get_meta(f"lang:{user_id}")

    def set_user_lang(self, user_id: int, lang: str) -> None:
        self.set_meta(f"lang:{user_id}", lang)

    def users_by_lang(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT value, COUNT(*) FROM meta WHERE key LIKE 'lang:%' "
                                 "GROUP BY value").fetchall()
        return {r[0]: r[1] for r in rows}

    # --- tokens ---
    def upsert_token(self, t: Token) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO tokens(address, curve, deployer, pair, quote_symbol, quote_decimals, "
                "quote_kind, name, symbol, decimals, total_supply, threshold, pool_id, curve_k, "
                "phantom, graduated, added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET graduated=MAX(graduated, excluded.graduated), "
                "curve_k=COALESCE(tokens.curve_k, excluded.curve_k), "
                "phantom=COALESCE(tokens.phantom, excluded.phantom)",
                (t.address, t.curve, t.deployer, t.pair, t.quote_symbol, t.quote_decimals,
                 t.quote_kind, t.name, t.symbol, t.decimals, str(t.total_supply), str(t.threshold),
                 t.pool_id, str(t.curve_k) if t.curve_k else None,
                 str(t.phantom) if t.phantom is not None else None, int(t.graduated), t.added_at))

    def get_token(self, address: str) -> Token | None:
        r = self.conn.execute("SELECT * FROM tokens WHERE address=?", (address.lower(),)).fetchone()
        return _token(r) if r else None

    def tracked_tokens(self) -> list[Token]:
        rows = self.conn.execute(
            "SELECT * FROM tokens WHERE address IN "
            "(SELECT DISTINCT token FROM chats WHERE active=1 AND token IS NOT NULL)").fetchall()
        return [_token(r) for r in rows]

    def mark_graduated(self, address: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE tokens SET graduated=1 WHERE address=?", (address.lower(),))

    # --- chats ---
    def ensure_chat(self, chat_id: int, title: str = "") -> Chat:
        with self.conn:
            self.conn.execute("INSERT INTO chats(chat_id, title, created_at) VALUES (?,?,?) "
                              "ON CONFLICT(chat_id) DO UPDATE SET active=1, "
                              "title=CASE WHEN excluded.title='' THEN chats.title "
                              "ELSE excluded.title END",
                              (chat_id, title, int(time.time())))
        return self.get_chat(chat_id)

    def get_chat(self, chat_id: int) -> Chat | None:
        r = self.conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        return _chat(r) if r else None

    def update_chat(self, chat_id: int, **fields) -> None:
        bad = set(fields) - CHAT_SETTINGS
        if bad:
            raise ValueError(f"unknown chat fields: {bad}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE chats SET {cols} WHERE chat_id=?", (*fields.values(), chat_id))

    def deactivate_chat(self, chat_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))
            self.conn.execute("DELETE FROM outbox WHERE chat_id=?", (chat_id,))

    def migrate_chat(self, old_id: int, new_id: int) -> None:
        """A group became a supergroup: Telegram gave it a new id."""
        with self.conn:
            if self.conn.execute("SELECT 1 FROM chats WHERE chat_id=?", (new_id,)).fetchone():
                self.conn.execute("DELETE FROM chats WHERE chat_id=?", (old_id,))
            else:
                self.conn.execute("UPDATE chats SET chat_id=? WHERE chat_id=?", (new_id, old_id))
            self.conn.execute("UPDATE OR IGNORE outbox SET chat_id=? WHERE chat_id=?", (new_id, old_id))
            self.conn.execute("DELETE FROM outbox WHERE chat_id=?", (old_id,))
            self.conn.execute("UPDATE OR IGNORE chat_stats SET chat_id=? WHERE chat_id=?", (new_id, old_id))

    def chats_of_user(self, user_id: int) -> list[Chat]:
        rows = self.conn.execute("SELECT * FROM chats WHERE active=1 AND added_by=? ORDER BY created_at",
                                 (user_id,)).fetchall()
        return [_chat(r) for r in rows]

    def active_chats(self, offset: int = 0, limit: int = 20) -> tuple[list[Chat], int]:
        """A page of active chats, newest first, and how many there are."""
        rows = self.conn.execute("SELECT * FROM chats WHERE active=1 ORDER BY created_at DESC "
                                 "LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        total = self.conn.execute("SELECT COUNT(*) FROM chats WHERE active=1").fetchone()[0]
        return [_chat(r) for r in rows], total

    def pro_chats(self, now: float | None = None) -> list[Chat]:
        t = int(time.time() if now is None else now)
        rows = self.conn.execute("SELECT * FROM chats WHERE pro_until>? ORDER BY pro_until DESC",
                                 (t,)).fetchall()
        return [_chat(r) for r in rows]

    def set_pro_until(self, chat_id: int, until: int) -> None:
        """The owner's hand on Pro: grant up to a date, or take it away (until = now)."""
        with self.conn:
            self.conn.execute("UPDATE chats SET pro_until=? WHERE chat_id=?", (until, chat_id))

    def recent_payments(self, limit: int = 15) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM payments ORDER BY created_at DESC LIMIT ?",
                                 (limit,)).fetchall()

    def unclaimed_chats(self, limit: int = 50) -> list[Chat]:
        """Active chats joined before the bot recorded who added it."""
        rows = self.conn.execute("SELECT * FROM chats WHERE active=1 AND added_by IS NULL "
                                 "ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        return [_chat(r) for r in rows]

    # --- what the bot posted, per chat and UTC day ---
    def add_chat_stat(self, chat_id: int, kind: str, trades: int, usd: float, now: float | None = None) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() if now is None else now))
        col = "sells" if kind == "sell" else "buys"
        with self.conn:
            self.conn.execute(f"INSERT INTO chat_stats(chat_id, day, {col}, usd) VALUES (?,?,?,?) "
                              f"ON CONFLICT(chat_id, day) DO UPDATE SET {col}={col}+excluded.{col}, "
                              "usd=usd+excluded.usd", (chat_id, day, trades, usd))

    def chat_posted(self, chat_id: int, now: float | None = None) -> dict[str, int]:
        """Trades posted in a chat: today, the last 7 days and all time."""
        t = time.time() if now is None else now
        today = time.strftime("%Y-%m-%d", time.gmtime(t))
        week = time.strftime("%Y-%m-%d", time.gmtime(t - 6 * 86400))
        out = {}
        for name, since in (("today", today), ("week", week), ("all", "0000")):
            r = self.conn.execute("SELECT COALESCE(SUM(buys + sells), 0) FROM chat_stats "
                                  "WHERE chat_id=? AND day>=?", (chat_id, since)).fetchone()
            out[name] = r[0]
        return out

    def chats_for_token(self, token: str) -> list[Chat]:
        rows = self.conn.execute("SELECT * FROM chats WHERE token=? AND active=1",
                                 (token.lower(),)).fetchall()
        return [_chat(r) for r in rows]

    def extend_pro(self, chat_id: int, seconds: int, now: int | None = None) -> int:
        now = int(time.time()) if now is None else now
        with self.conn:
            self.conn.execute("UPDATE chats SET pro_until=MAX(pro_until, ?) + ? WHERE chat_id=?",
                              (now, seconds, chat_id))
        chat = self.get_chat(chat_id)
        return chat.pro_until if chat else 0

    # --- outbox ---
    def commit_tick(self, rows: list[tuple[int, str, str, dict]], last_block: int,
                    graduated: list[str] = (), trades: list[tuple] = ()) -> int:
        """Store a tick's messages, trades for charts, graduations and progress
        atomically. Returns outbox rows added."""
        now = int(time.time())
        added = 0
        with self.conn:
            for chat_id, kind, dedupe, payload in rows:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO outbox(chat_id, kind, dedupe, payload, created_at) "
                    "VALUES (?,?,?,?,?)", (chat_id, kind, dedupe, json.dumps(payload), now))
                added += cur.rowcount
            self._insert_trades(trades)
            for token in graduated:
                self.conn.execute("UPDATE tokens SET graduated=1 WHERE address=?", (token,))
            self.conn.execute("INSERT INTO meta(key, value) VALUES('last_block', ?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (str(last_block),))
        return added

    # --- trades, for charts ---
    def _insert_trades(self, trades) -> None:
        rows = [r if len(r) == 7 else (*r, None) for r in trades]
        # a trade seen again keeps its row but learns its wallet if it lacked one
        self.conn.executemany("INSERT INTO trades(token, uid, ts, price, quote, side, wallet) "
                              "VALUES (?,?,?,?,?,?,?) ON CONFLICT(token, uid) DO UPDATE SET "
                              "wallet=COALESCE(trades.wallet, excluded.wallet)", rows)

    def add_trades(self, trades: list[tuple]) -> None:
        """(token, uid, ts, price, quote, side) rows; repeats are ignored."""
        with self.conn:
            self._insert_trades(trades)

    def trades_since(self, token: str, ts: int) -> list[tuple[int, float, float, int]]:
        rows = self.conn.execute("SELECT ts, price, quote, side FROM trades WHERE token=? AND ts>=? "
                                 "ORDER BY ts, uid", (token.lower(), ts)).fetchall()
        return [tuple(r) for r in rows]

    def top_buyers(self, token: str, since: int, limit: int = 10) -> tuple[list[tuple[str, float, int]], int]:
        """The wallets that bought the most (in quote units) since a time, and how many
        different wallets bought at all."""
        rows = self.conn.execute(
            "SELECT wallet, SUM(quote) AS q, COUNT(*) FROM trades WHERE token=? AND ts>=? AND side=1 "
            "AND wallet IS NOT NULL GROUP BY wallet ORDER BY q DESC LIMIT ?",
            (token.lower(), since, limit)).fetchall()
        n = self.conn.execute("SELECT COUNT(DISTINCT wallet) FROM trades WHERE token=? AND ts>=? AND side=1 "
                              "AND wallet IS NOT NULL", (token.lower(), since)).fetchone()[0]
        return [tuple(r) for r in rows], n

    def last_trade_ts(self, token: str) -> int | None:
        r = self.conn.execute("SELECT MAX(ts) FROM trades WHERE token=?", (token.lower(),)).fetchone()
        return r[0] if r else None

    def prune_trades(self, before_ts: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM trades WHERE ts<?", (before_ts,))

    def add_outbox(self, chat_id: int, kind: str, dedupe: str, payload: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO outbox(chat_id, kind, dedupe, payload, "
                              "created_at) VALUES (?,?,?,?,?)",
                              (chat_id, kind, dedupe, json.dumps(payload), int(time.time())))

    def pending_outbox(self, limit: int = 1000) -> list[OutboxRow]:
        rows = self.conn.execute("SELECT * FROM outbox ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [OutboxRow(r["id"], r["chat_id"], r["kind"], json.loads(r["payload"]),
                          r["created_at"], r["attempts"]) for r in rows]

    def delete_outbox(self, ids: list[int]) -> None:
        if ids:
            with self.conn:
                self.conn.executemany("DELETE FROM outbox WHERE id=?", [(i,) for i in ids])

    def bump_attempts(self, ids: list[int]) -> None:
        if ids:
            with self.conn:
                self.conn.executemany("UPDATE outbox SET attempts=attempts+1 WHERE id=?",
                                      [(i,) for i in ids])

    def clear_chat_outbox(self, chat_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM outbox WHERE chat_id=?", (chat_id,))

    def outbox_size(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    # --- payments ---
    def record_payment(self, charge_id: str, user_id: int, amount: int, payload: str) -> bool:
        """False if this charge was already recorded (Telegram may repeat an update)."""
        with self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO payments(charge_id, user_id, amount, "
                                    "payload, created_at) VALUES (?,?,?,?,?)",
                                    (charge_id, user_id, amount, payload, int(time.time())))
        return cur.rowcount == 1

    def get_payment(self, charge_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM payments WHERE charge_id=?", (charge_id,)).fetchone()

    def mark_refunded(self, charge_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE payments SET refunded=1 WHERE charge_id=?", (charge_id,))

    # --- ads ---
    def create_ad(self, user_id: int, text: str, url: str, days: int, price: int) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO ads(user_id, text, url, days, price, status, "
                                    "created_at) VALUES (?,?,?,?,?, 'pending', ?)",
                                    (user_id, text, url, days, price, int(time.time())))
        return cur.lastrowid

    def get_ad(self, ad_id: int) -> Ad | None:
        r = self.conn.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
        return _ad(r) if r else None

    def set_ad_status(self, ad_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE ads SET status=? WHERE id=?", (status, ad_id))

    def schedule_paid_ad(self, ad_id: int, charge_id: str, now: int | None = None) -> tuple[int, int]:
        """Queue a paid ad after every ad already paid for: slots never overlap."""
        now = int(time.time()) if now is None else now
        with self.conn:
            ad = self.conn.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
            last_end = self.conn.execute(
                "SELECT MAX(ends_at) FROM ads WHERE status='paid' AND id<>?", (ad_id,)).fetchone()[0]
            starts = max(now, last_end or 0)
            ends = starts + ad["days"] * 86400
            self.conn.execute("UPDATE ads SET status='paid', starts_at=?, ends_at=?, charge_id=? "
                              "WHERE id=?", (starts, ends, charge_id, ad_id))
        return starts, ends

    def current_ad(self, now: int | None = None) -> Ad | None:
        now = int(time.time()) if now is None else now
        r = self.conn.execute("SELECT * FROM ads WHERE status='paid' AND starts_at<=? AND ends_at>? "
                              "ORDER BY starts_at LIMIT 1", (now, now)).fetchone()
        return _ad(r) if r else None

    def ads_by_status(self, status: str) -> list[Ad]:
        rows = self.conn.execute("SELECT * FROM ads WHERE status=? ORDER BY id", (status,)).fetchall()
        return [_ad(r) for r in rows]

    # --- stats ---
    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        now = int(time.time())
        return {
            "chats_active": q("SELECT COUNT(*) FROM chats WHERE active=1"),
            "chats_tracking": q("SELECT COUNT(*) FROM chats WHERE active=1 AND token IS NOT NULL"),
            "chats_pro": q(f"SELECT COUNT(*) FROM chats WHERE active=1 AND pro_until>{now}"),
            "tokens_tracked": len(self.tracked_tokens()),
            "outbox": self.outbox_size(),
            "stars_total": q("SELECT COALESCE(SUM(amount),0) FROM payments WHERE refunded=0"),
            "ads_pending": q("SELECT COUNT(*) FROM ads WHERE status='pending'"),
        }
