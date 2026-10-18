"""SQLite storage. One file, WAL mode, used from the event loop's thread only.

The watcher writes a tick's outbox rows and its new last block in one
transaction, so a restart neither loses nor repeats a buy. Big integers
(supplies, thresholds in wei) are stored as TEXT: they overflow SQLite's int64.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

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
    added_at INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'pons'   -- 'pons' (curve, then its v4 pool) | 'flap' (curve) | 'dex' (pools)
);
CREATE TABLE IF NOT EXISTS pools (
    token TEXT NOT NULL,
    venue TEXT NOT NULL,     -- v2 | v3 | v4
    pool TEXT NOT NULL,      -- the pair or pool address, or the v4 PoolId
    quote TEXT NOT NULL,
    quote_symbol TEXT NOT NULL,
    quote_decimals INTEGER NOT NULL,
    quote_kind TEXT NOT NULL,
    dex TEXT NOT NULL,
    liquidity_usd REAL,
    PRIMARY KEY (token, pool)
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
    pro_until INTEGER NOT NULL DEFAULT 0,   -- unused since 0.4 (no paid plan); older code still reads it
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
              ("trades", "wallet", "TEXT"),
              ("tokens", "kind", "TEXT NOT NULL DEFAULT 'pons'")]


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
    kind: str = "pons"    # "pons": its curve, then its v4 pool; "flap": its Flap curve;
                          # "dex": the pools in `pools`


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
    active: bool
    created_at: int
    sells: bool = False  # post sells too
    chart: int = 1       # the chart with each post: 0 off, 1 animated, 2 still
    lang: str = "en"     # posts and answers in the chat
    added_by: int | None = None  # who added the bot (or claimed the chat as its admin)
    kind: str | None = None      # "channel", "supergroup", "group"
    thread_id: int | None = None # the forum topic posts go to (set by /track sent in it)
    whale_usd: float = 1000.0    # buys from this many dollars get the 🐋 mark; 0 turns it off


@dataclass
class OutboxRow:
    id: int
    chat_id: int
    kind: str
    payload: dict
    created_at: int
    attempts: int


def _token(r: sqlite3.Row) -> Token:
    return Token(r["address"], r["curve"], r["deployer"], r["pair"], r["quote_symbol"],
                 r["quote_decimals"], r["quote_kind"], r["name"], r["symbol"], r["decimals"],
                 int(r["total_supply"]), int(r["threshold"]), r["pool_id"],
                 int(r["curve_k"]) if r["curve_k"] else None,
                 int(r["phantom"]) if r["phantom"] is not None else None,
                 bool(r["graduated"]), r["added_at"], r["kind"])


def _chat(r: sqlite3.Row) -> Chat:
    return Chat(r["chat_id"], r["title"], r["token"], r["min_usd"], r["emoji"], r["step_usd"],
                r["media_kind"], r["media_file_id"], bool(r["active"]),
                r["created_at"], bool(r["sells"]), int(r["chart"]), r["lang"], r["added_by"], r["kind"],
                r["thread_id"], r["whale_usd"])


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
        self._drop_paid_plan()
        self.conn.commit()

    def _drop_paid_plan(self) -> None:
        """0.4 removed Pro, ads and Stars payments. Their tables go when they are empty
        (a self-hosted copy that holds real payments keeps them, with a warning), and so
        do the cached menu animations of the removed screens. chats.pro_until stays,
        unused, so that older code can still read the chats table."""
        for table in ("ads", "payments"):
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (table,)).fetchone() is None:
                continue
            if self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                log.warning("table %s has rows from the removed paid plan; kept", table)
            else:
                self.conn.execute(f"DROP TABLE {table}")
        self.conn.execute("DELETE FROM meta WHERE key LIKE 'anim:%:pro:%' OR key LIKE 'anim:%:ads:%'")

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
                "phantom, graduated, added_at, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET graduated=MAX(graduated, excluded.graduated), "
                "curve_k=COALESCE(tokens.curve_k, excluded.curve_k), "
                "phantom=COALESCE(tokens.phantom, excluded.phantom)",
                (t.address, t.curve, t.deployer, t.pair, t.quote_symbol, t.quote_decimals,
                 t.quote_kind, t.name, t.symbol, t.decimals, str(t.total_supply), str(t.threshold),
                 t.pool_id, str(t.curve_k) if t.curve_k else None,
                 str(t.phantom) if t.phantom is not None else None, int(t.graduated), t.added_at,
                 t.kind))

    def get_token(self, address: str) -> Token | None:
        r = self.conn.execute("SELECT * FROM tokens WHERE address=?", (address.lower(),)).fetchone()
        return _token(r) if r else None

    def tokens_with_symbol(self, symbol: str) -> list[tuple[str, str, int]]:
        """(symbol, address, added_at) of every token a chat ever tracked with this symbol
        ($ and case aside): what /scan compares a ticker against."""
        want = (symbol or "").strip().lstrip("$").lower()
        rows = self.conn.execute("SELECT symbol, address, added_at FROM tokens").fetchall()
        return [(r[0], r[1], r[2]) for r in rows if r[0].strip().lstrip("$").lower() == want]

    def tracked_tokens(self) -> list[Token]:
        rows = self.conn.execute(
            "SELECT * FROM tokens WHERE address IN "
            "(SELECT DISTINCT token FROM chats WHERE active=1 AND token IS NOT NULL)").fetchall()
        return [_token(r) for r in rows]

    def mark_graduated(self, address: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE tokens SET graduated=1 WHERE address=?", (address.lower(),))

    def set_main_quote(self, address: str, pair: str, symbol: str, decimals: int, kind: str) -> None:
        """A dex token's main quote (its deepest pool's) changed: charts and market cap
        follow it. Trades kept in other units are dropped, not mixed with the new ones
        (ETH and WETH, or two dollar quotes, are the same units)."""
        address = address.lower()
        with self.conn:
            old = self.conn.execute("SELECT pair, quote_kind FROM tokens WHERE address=?", (address,)).fetchone()
            if old is None or old["pair"] == pair:
                return
            self.conn.execute("UPDATE tokens SET pair=?, quote_symbol=?, quote_decimals=?, quote_kind=? "
                              "WHERE address=?", (pair, symbol, decimals, kind, address))
            if not (old["quote_kind"] == kind and kind in ("eth", "usd")):
                self.conn.execute("DELETE FROM trades WHERE token=?", (address,))

    def graduate_to_pools(self, address: str, pools: list) -> None:
        """A curve token (Flap) graduated: from now on it is followed through its pools."""
        address = address.lower()
        with self.conn:
            self.conn.execute("UPDATE tokens SET kind='dex', graduated=1 WHERE address=?", (address,))
        self.set_pools(address, pools)
        main = pools[0]
        self.set_main_quote(address, main.quote, main.quote_symbol, main.quote_decimals, main.quote_kind)

    def reprice_quotes(self, known: dict) -> int:
        """Tokens stored with a quote the bot could not value, that it now can (a new
        entry in quotes.QUOTES): they get the quote's kind. Returns how many changed."""
        n = 0
        with self.conn:
            for r in self.conn.execute("SELECT address, pair FROM tokens WHERE quote_kind='other'").fetchall():
                if r["pair"] in known:
                    self.conn.execute("UPDATE tokens SET quote_kind=? WHERE address=?",
                                      (known[r["pair"]][2], r["address"]))
                    n += 1
            for r in self.conn.execute("SELECT token, pool, quote FROM pools WHERE quote_kind='other'").fetchall():
                if r["quote"] in known:
                    self.conn.execute("UPDATE pools SET quote_kind=? WHERE token=? AND pool=?",
                                      (known[r["quote"]][2], r["token"], r["pool"]))
                    n += 1
        return n

    # --- pools of dex tokens ---
    def set_pools(self, token: str, pools: list) -> None:
        """Replace the pools watched for a token (pools.Pool items)."""
        token = token.lower()
        with self.conn:
            self.conn.execute("DELETE FROM pools WHERE token=?", (token,))
            self.conn.executemany(
                "INSERT INTO pools(token, venue, pool, quote, quote_symbol, quote_decimals, quote_kind, "
                "dex, liquidity_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                [(token, p.venue, p.id, p.quote, p.quote_symbol, p.quote_decimals, p.quote_kind, p.dex,
                  p.liquidity_usd) for p in pools])

    def pools_of(self, token: str) -> list:
        from .pools import Pool
        rows = self.conn.execute("SELECT * FROM pools WHERE token=? ORDER BY liquidity_usd DESC",
                                 (token.lower(),)).fetchall()
        return [Pool(r["token"], r["venue"], r["pool"], r["quote"], r["quote_symbol"], r["quote_decimals"],
                     r["quote_kind"], r["dex"], r["liquidity_usd"]) for r in rows]

    def tracked_pools(self) -> list:
        """The pools of every tracked dex token."""
        from .pools import Pool
        rows = self.conn.execute(
            "SELECT p.* FROM pools p JOIN tokens t ON t.address=p.token WHERE t.kind='dex' AND p.token IN "
            "(SELECT DISTINCT token FROM chats WHERE active=1 AND token IS NOT NULL)").fetchall()
        return [Pool(r["token"], r["venue"], r["pool"], r["quote"], r["quote_symbol"], r["quote_decimals"],
                     r["quote_kind"], r["dex"], r["liquidity_usd"]) for r in rows]

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

    def day_counts(self, token: str, since: int) -> dict:
        """Trades of a token since a time: buys, sells, their volume in its main quote, and
        how many different wallets bought and sold (where the wallet is known)."""
        r = self.conn.execute(
            "SELECT SUM(side=1), SUM(side=-1), COALESCE(SUM(CASE WHEN side=1 THEN quote END), 0), "
            "COALESCE(SUM(CASE WHEN side=-1 THEN quote END), 0), "
            "COUNT(DISTINCT CASE WHEN side=1 THEN wallet END), COUNT(DISTINCT CASE WHEN side=-1 THEN wallet END) "
            "FROM trades WHERE token=? AND ts>=?", (token.lower(), since)).fetchone()
        return {"buys": r[0] or 0, "sells": r[1] or 0, "buy_quote": r[2], "sell_quote": r[3],
                "buyers": r[4], "sellers": r[5]}

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

    # --- stats ---
    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "chats_active": q("SELECT COUNT(*) FROM chats WHERE active=1"),
            "chats_tracking": q("SELECT COUNT(*) FROM chats WHERE active=1 AND token IS NOT NULL"),
            "tokens_tracked": len(self.tracked_tokens()),
            "outbox": self.outbox_size(),
        }
