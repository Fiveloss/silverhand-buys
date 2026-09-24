"""The watcher: new blocks -> buys, sells and graduations of tracked tokens -> outbox.

Per tick it makes at most: one eth_blockNumber, one eth_getLogs per 100
tracked curves (curve buys and sells plus the factory's graduations), and one
per 100 graduated pools (Uniswap v4 swaps). Price and curve progress come from
the event itself (chain.curve_after_buy / curve_after_sell, sqrtPriceX96). Only
the few trades that will be posted (at most four a tick) cost more: the wallet's
balance, and for v4 the transaction's sender. Sells go only to chats that turned
them on.
"""

import asyncio
import logging
import time

from . import abi, chain
from .db import Chat, Db, Token

log = logging.getLogger(__name__)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def usd_value(amount: float, kind: str, eth_usd: float | None) -> float | None:
    if kind == "usd":
        return amount
    if kind == "eth" and eth_usd:
        return amount * eth_usd
    return None


BLOCKS_PER_SECOND = 10    # Robinhood Chain: a block about every 0.1 s
TRADES_KEPT = 26 * 3600   # seconds of trades kept for charts
# marks a token's last day as loaded; a new name loads every token's day once more
# (v2: with the wallets of v4 buys)
BACKFILL_KEY = "backfill2:"


def log_time(lg: dict, clock) -> int:
    """A log's block time. The public RPC fills blockTimestamp only sometimes (often
    0x0), so otherwise clock(block number) estimates it."""
    try:
        ts = int(lg.get("blockTimestamp") or "0x0", 16)
    except (TypeError, ValueError):
        ts = 0
    return ts if ts > 0 else int(clock(int(lg["blockNumber"], 16)))


def trade_rows(events: list) -> list[tuple]:
    """Kept trades (token, uid, ts, price, quote, side, wallet) from decoded events."""
    return [(t.address, dedupe, p["ts"], p["price"], p["quote"], 1 if kind == "buy" else -1, p.get("buyer"))
            for _, _, kind, t, dedupe, p in events if kind in ("buy", "sell") and p.get("price")]


def name_v4_buyers(events: list, payouts: dict) -> None:
    """Give v4 buys the wallet the pool paid the token to (see chain.pool_payouts)."""
    for _, _, kind, t, _, p in events:
        if kind == "buy" and p.get("venue") == "v4" and not p.get("buyer"):
            wallet = payouts.get((t.address, p["tx"]))
            if wallet:
                p["buyer"] = wallet
                p["dev"] = wallet == t.deployer


def holder_note(kind: str, balance: float, tokens: float, supply: float) -> dict | None:
    """What a trade did to the wallet's position, from its balance right after it:
    {"new": True} for a first buy, {"all": True} for a sell of everything, else the
    position's change (+0.35 = a third more) and the wallet's share of the supply."""
    if tokens <= 0 or supply <= 0:
        return None
    share = balance / supply
    if kind == "buy":
        if balance < tokens * 0.99:
            return None  # the tokens went to another address
        before = balance - tokens
        if before < tokens * 0.01:
            return {"new": True, "share": share}
        return {"change": tokens / before, "share": share}
    if balance < tokens * 0.001:
        return {"all": True}
    return {"change": -tokens / (balance + tokens), "share": share}


MC_LEVELS = [10_000 * m * 10 ** e for e in range(6) for m in (1, 2.5, 5)]  # $10K … $5B
CURVE_LEVELS = [0.5, 0.75, 0.9]


def level_reached(value: float, levels: list[float]) -> float | None:
    """The highest level at or below value."""
    below = [x for x in levels if x <= value]
    return max(below) if below else None


def passes_filter(chat: Chat, usd: float | None) -> bool:
    """Below the chat's minimum -> skipped. Unknown dollar value -> posted."""
    return usd is None or usd >= chat.min_usd


class Watcher:
    def __init__(self, rpc, db: Db, eth_usd, *, poll_seconds: float = 3.0,
                 max_range: int = 2000, max_catchup: int = 3000, chunk: int = 100,
                 phase_refresh: float = 300.0):
        self.rpc = rpc
        self.db = db
        self.eth_usd = eth_usd
        self.poll_seconds = poll_seconds
        self.max_range = max_range
        self.max_catchup = max_catchup
        self.chunk = chunk
        self.phase_refresh = phase_refresh
        self._phase_checked = 0.0
        self._pruned = 0.0
        self.head: int | None = None
        self.last_ok = time.time()
        self.last_error: str | None = None
        self.skipped_blocks = 0

    async def run(self) -> None:
        backoff = self.poll_seconds
        while True:
            try:
                await self.tick()
                self.last_ok = time.time()
                self.last_error = None
                backoff = self.poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("watcher tick failed")
                backoff = min(backoff * 2, 60)
            await asyncio.sleep(backoff)

    async def tick(self) -> int:
        head = await self.rpc.block_number()
        self.head = head
        last = self.db.last_block()
        if last is None or last > head:
            # First start: begin at the tip. Old buys are never posted.
            self.db.set_last_block(head)
            return 0
        if head == last:
            return 0
        if head - last > self.max_catchup:
            skip_to = head - self.max_catchup
            self.skipped_blocks += skip_to - last
            log.warning("behind by %d blocks, skipping to %d", head - last, skip_to)
            last = skip_to
        frm, to = last + 1, min(head, last + self.max_range)

        tokens = self.db.tracked_tokens()
        if not tokens:
            self.db.set_last_block(to)
            return 0
        if time.time() - self._phase_checked > self.phase_refresh:
            await self.refresh_phases(tokens)
            tokens = self.db.tracked_tokens()
        rows, graduated, trades = await self.collect(frm, to, tokens)
        if time.time() - self._pruned > 3600:
            self._pruned = time.time()
            self.db.prune_trades(int(time.time() - TRADES_KEPT))
        return self.db.commit_tick(rows, to, graduated, trades)

    async def refresh_phases(self, tokens: list[Token]) -> None:
        """Catch graduations whose event we never saw (added mid-graduation, or skipped
        while catching up): ask the factory directly, a few calls every few minutes."""
        self._phase_checked = time.time()
        for t in tokens:
            if t.graduated:
                continue
            try:
                ret = await self.rpc.eth_call(chain.PONS_FACTORY,
                                              abi.call_data(chain.SEL_GET_LAUNCHED_TOKEN, t.address))
            except Exception as e:
                log.info("phase check for %s failed: %s", t.address, e)
                continue
            rec = chain.decode_launched_token(ret or "0x")
            if rec and rec.exists and rec.graduated:
                log.info("%s graduated (found by phase check)", t.symbol)
                self.db.mark_graduated(t.address)

    async def collect(self, frm: int, to: int, tokens: list[Token]):
        by_curve = {t.curve: t for t in tokens}
        by_addr = {t.address: t for t in tokens}

        logs: list[dict] = []
        # Only tokens still on the curve can buy there or graduate. Once every tracked
        # token has graduated, this query is skipped entirely.
        curves = [t.curve for t in tokens if not t.graduated]
        for i, chunk in enumerate(_chunks(curves, self.chunk)):
            addresses = chunk + ([chain.PONS_FACTORY] if i == 0 else [])
            logs += await self.rpc.get_logs(frm, to, address=addresses,
                                            topics=[[chain.TOPIC_CURVE_BUY, chain.TOPIC_CURVE_SELL,
                                                     chain.TOPIC_GRADUATED]])

        graduated_now = set()
        for lg in logs:
            if (lg.get("topics") or [""])[0].lower() == chain.TOPIC_GRADUATED:
                g = chain.decode_graduated(lg)
                if g and g.token in by_addr:
                    graduated_now.add(g.token)

        # A v4 pool exists only after graduation; ask for pools of tokens that
        # graduated before or inside this range, so no swap right after it is missed.
        pools = {t.pool_id: t for t in tokens if t.graduated or t.address in graduated_now}
        for chunk in _chunks(list(pools), self.chunk):
            logs += await self.rpc.get_logs(frm, to, address=chain.POOL_MANAGER,
                                            topics=[chain.TOPIC_V4_SWAP, chunk])
        payouts = {}
        for chunk in _chunks([t.address for t in pools.values()], self.chunk):
            payouts.update(chain.pool_payouts(await self.rpc.get_logs(
                frm, to, address=chunk, topics=[chain.TOPIC_TRANSFER, abi.address_topic(chain.POOL_MANAGER)])))

        eth_usd = await self.eth_usd.get()
        now, head = time.time(), self.head or to
        events = self._events(logs, by_curve, by_addr, pools, eth_usd,
                              lambda b: now - (head - b) / BLOCKS_PER_SECOND)
        name_v4_buyers(events, payouts)
        events = events + self._milestones(events)
        chats_cache: dict[str, list[Chat]] = {}
        rows = []
        for _, _, kind, t, dedupe, payload in events:
            chats = chats_cache.setdefault(t.address, self.db.chats_for_token(t.address))
            for chat in chats:
                if kind == "sell" and not chat.sells:
                    continue
                if kind in ("buy", "sell") and not passes_filter(chat, payload["usd"]):
                    continue
                rows.append((chat.chat_id, kind, dedupe, payload))
        await self._about_traders([(p_kind, t, p) for _, _, p_kind, t, _, p in events
                                   if p_kind in ("buy", "sell") and any(r[3] is p for r in rows)])
        return rows, sorted(graduated_now), trade_rows(events)

    async def _about_traders(self, trades: list[tuple[str, Token, dict]], limit: int = 4) -> None:
        """For the few trades that will be posted (at most `limit` a tick; the rest go
        out without this rather than late): who traded, and what they hold now.

        A v4 Swap names the router, not the trader, so the wallet is the one that sent
        the transaction. The wallet's balance after the trade says whether it is a new
        holder and how much its position changed. A balance that cannot hold what was
        just bought means the tokens went elsewhere: then nothing is said about it."""
        for kind, t, p in trades[:limit]:
            if p["venue"] == "v4" and p.get("buyer") is None and hasattr(self.rpc, "tx_sender"):
                try:
                    p["buyer"] = await self.rpc.tx_sender(p["tx"])
                except Exception as e:
                    log.info("tx sender of %s unknown: %s", p["tx"], e)
                p["dev"] = bool(p.get("buyer")) and p["buyer"] == t.deployer
            if not p.get("buyer"):
                continue
            try:
                raw = abi.decode_uint(await self.rpc.eth_call(
                    t.address, abi.call_data(chain.SEL_BALANCE_OF, p["buyer"])))
            except Exception as e:
                log.info("balance of %s unknown: %s", p["buyer"], e)
                continue
            if raw is None:
                continue
            p["holder"] = holder_note(kind, raw / 10 ** t.decimals, p["tokens"],
                                      t.total_supply / 10 ** t.decimals)

    def _milestones(self, events: list) -> list:
        """A token's market cap passing a round level ($10K, $25K, $50K …) or its curve
        passing 50/75/90% full, each only the first time: the highest level reached is
        kept per token, so swings around a level post nothing. The first trade seen of a
        token only sets where it stands, so levels passed before tracking are not posted."""
        out = []
        for block, index, kind, t, _, p in events:
            if kind not in ("buy", "sell"):
                continue
            for name, value, levels in (("mc", p.get("mc_usd"), MC_LEVELS),
                                        ("curve", p.get("progress"), CURVE_LEVELS)):
                if value is None:
                    continue
                key = f"level:{name}:{t.address}"
                reached = level_reached(value, levels)
                seen = self.db.get_meta(key)
                if seen is None:
                    self.db.set_meta(key, str(reached or 0))
                    continue
                if reached is None or reached <= float(seen):
                    continue
                self.db.set_meta(key, str(reached))
                if kind != "buy":
                    continue  # a sell never sets a new high worth a post
                out.append((block, index, "milestone", t, f"level:{name}:{reached}", {
                    "token": t.address, "symbol": t.symbol, "what": name, "level": reached,
                    "mc_usd": p.get("mc_usd"), "progress": p.get("progress"), "usd": p.get("usd"),
                    "quote": p["quote"], "quote_symbol": t.quote_symbol, "tx": p["tx"]}))
        return out

    def _events(self, logs: list[dict], by_curve: dict, by_addr: dict, pools: dict, eth_usd, clock):
        """Decoded trades and graduations of the given tokens, oldest first:
        (block, log_index, kind, token, dedupe, payload)."""
        events = []
        for lg in logs:
            topic0 = (lg.get("topics") or [""])[0].lower()
            ev = None
            if topic0 == chain.TOPIC_CURVE_BUY:
                b = chain.decode_curve_buy(lg)
                t = by_curve.get(b.curve) if b else None
                ev = self._curve_buy_event(b, t, eth_usd) if t else None
            elif topic0 == chain.TOPIC_CURVE_SELL:
                sl = chain.decode_curve_sell(lg)
                t = by_curve.get(sl.curve) if sl else None
                ev = self._curve_sell_event(sl, t, eth_usd) if t else None
            elif topic0 == chain.TOPIC_GRADUATED:
                g = chain.decode_graduated(lg)
                t = by_addr.get(g.token) if g else None
                if t:
                    raised = g.quote_raised / 10 ** t.quote_decimals
                    ev = (g.block, g.log_index, "grad", t, f"grad:{g.tx}:{g.log_index}", {
                        "token": t.address, "symbol": t.symbol, "name": t.name,
                        "raised": raised, "quote_symbol": t.quote_symbol,
                        "usd": usd_value(raised, t.quote_kind, eth_usd), "tx": g.tx})
            elif topic0 == chain.TOPIC_V4_SWAP:
                s = chain.decode_v4_swap(lg)
                t = pools.get(s.pool_id) if s else None
                ev = self._v4_trade_event(s, t, eth_usd) if t else None
            if ev:
                ev[5]["ts"] = log_time(lg, clock)
                events.append(ev)
        events.sort(key=lambda e: (e[0], e[1]))
        return events

    def needs_backfill(self, t: Token) -> bool:
        """True when a token's last day of trades is not in the database: never loaded,
        or loaded long ago and not kept up since (trades are kept only while tracked)."""
        done = self.db.get_meta(f"{BACKFILL_KEY}{t.address}")
        if done is None:
            return True
        last = self.db.last_trade_ts(t.address)
        return time.time() - float(done) > 20 * 3600 and (last is None or last < time.time() - 3600)

    async def backfill_missing(self) -> None:
        """After a restart: history for tracked tokens whose charts and stats would be thin."""
        for t in self.db.tracked_tokens():
            if not self.needs_backfill(t):
                continue
            try:
                await self.backfill(t)
            except Exception as e:
                log.warning("backfill of %s failed: %s", t.symbol, e)

    async def backfill(self, t: Token, hours: float = 24.0, window: int = 50_000) -> int:
        """Load the last hours of a token's trades, so its first chart has history.
        A few eth_getLogs calls: the RPC takes wide block ranges for one address."""
        head = await self.rpc.block_number()
        start = max(0, head - int(hours * 3600 * BLOCKS_PER_SECOND))
        now = time.time()
        try:  # two real block times; the ones between are interpolated
            t_start, t_head = await self.rpc.block_time(start), await self.rpc.block_time(head)
        except Exception as e:
            log.info("block times unknown (%s), estimating", e)
            t_start, t_head = now - (head - start) / BLOCKS_PER_SECOND, now
        span = max(1, head - start)

        def clock(b: int) -> float:
            return t_start + (b - start) * (t_head - t_start) / span

        eth_usd = await self.eth_usd.get()
        pools = {t.pool_id: t} if t.graduated else {}
        added = 0
        # window by window, so a busy token's day of logs is never all in memory at once
        for frm in range(start, head + 1, window):
            to = min(head, frm + window - 1)
            logs = await self.rpc.get_logs(frm, to, address=t.curve,
                                           topics=[[chain.TOPIC_CURVE_BUY, chain.TOPIC_CURVE_SELL]])
            payouts = {}
            if t.graduated:
                logs += await self.rpc.get_logs(frm, to, address=chain.POOL_MANAGER,
                                                topics=[chain.TOPIC_V4_SWAP, [t.pool_id]])
                payouts = chain.pool_payouts(await self.rpc.get_logs(
                    frm, to, address=t.address,
                    topics=[chain.TOPIC_TRANSFER, abi.address_topic(chain.POOL_MANAGER)]))
            events = self._events(logs, {t.curve: t}, {}, pools, eth_usd, clock)
            name_v4_buyers(events, payouts)
            rows = trade_rows(events)
            self.db.add_trades(rows)
            added += len(rows)
        if hours >= 24:
            self.db.set_meta(f"{BACKFILL_KEY}{t.address}", str(int(now)))
        log.info("backfilled %d trades of %s", added, t.symbol)
        return added

    def _buy_payload(self, t: Token, quote_raw: int, tokens_raw: int, price: float | None,
                     progress: float | None, venue: str, buyer: str | None, tx: str,
                     eth_usd: float | None) -> dict:
        quote = quote_raw / 10 ** t.quote_decimals
        mc_quote = price * t.total_supply / 10 ** t.decimals if price else None
        return {
            "token": t.address, "symbol": t.symbol, "name": t.name,
            "quote_symbol": t.quote_symbol, "quote": quote,
            "usd": usd_value(quote, t.quote_kind, eth_usd),
            "tokens": tokens_raw / 10 ** t.decimals,
            "buyer": buyer, "dev": bool(buyer and buyer == t.deployer), "tx": tx,
            "mc_quote": mc_quote,
            "mc_usd": usd_value(mc_quote, t.quote_kind, eth_usd) if mc_quote else None,
            "progress": progress, "venue": venue, "price": price, "deployer": t.deployer,
        }

    def _curve_buy_event(self, b: chain.CurveBuy, t: Token, eth_usd):
        price, progress = chain.curve_after_buy(b, t.curve_k, t.phantom, t.threshold,
                                                t.decimals, t.quote_decimals)
        if price is None:  # unknown curve shape: fall back to this trade's own average price
            price = chain.trade_price(b.quote_in - b.fee - b.tax, b.tokens_out,
                                      t.decimals, t.quote_decimals)
        payload = self._buy_payload(t, b.quote_in, b.tokens_out, price, progress, "curve",
                                    b.recipient, b.tx, eth_usd)
        return b.block, b.log_index, "buy", t, f"buy:{b.tx}:{b.log_index}", payload

    def _curve_sell_event(self, sl: chain.CurveSell, t: Token, eth_usd):
        price, progress = chain.curve_after_sell(sl, t.curve_k, t.phantom, t.threshold,
                                                 t.decimals, t.quote_decimals)
        if price is None:
            price = chain.trade_price(sl.quote_out + sl.fee + sl.tax, sl.tokens_in,
                                      t.decimals, t.quote_decimals)
        payload = self._buy_payload(t, sl.quote_out, sl.tokens_in, price, progress, "curve",
                                    sl.recipient, sl.tx, eth_usd)
        return sl.block, sl.log_index, "sell", t, f"sell:{sl.tx}:{sl.log_index}", payload

    def _v4_trade_event(self, s: chain.V4Swap, t: Token, eth_usd):
        buy = chain.v4_buy_amounts(s, t.address, t.pair)
        sell = None if buy else chain.v4_sell_amounts(s, t.address, t.pair)
        if not (buy or sell):
            return None
        quote_raw, tokens_raw = buy or sell
        kind = "buy" if buy else "sell"
        price = chain.v4_token_price(s.sqrt_price_x96, t.address, t.pair, t.decimals, t.quote_decimals)
        payload = self._buy_payload(t, quote_raw, tokens_raw, price, None, "v4", None, s.tx, eth_usd)
        return s.block, s.log_index, kind, t, f"{kind}:{s.tx}:{s.log_index}", payload
