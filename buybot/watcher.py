"""The watcher: new blocks -> buys, sells and graduations of tracked tokens -> outbox.

Three kinds of tokens are tracked:
- Pons v2 launches: buys and sells on their bonding curve, then in their Uniswap v4
  pool after graduation;
- Flap.sh launches on their bonding curve (all of them trade through one Portal);
  after graduation they become the next kind, with their Uniswap v2 pair;
- any other token, through its Uniswap v2/v3/v4 (and Ramses v3) pools, found when a
  chat starts tracking it and looked for again every hour (pools.find_pools).

Per tick it makes at most: one eth_blockNumber, one eth_getLogs per 100 tracked
curves (curve buys and sells plus the factory's graduations), one for Flap's Portal
when a Flap token is tracked, one per 100 pools of each venue (v4 swaps, v3 swaps, v2
swaps and syncs), and one per 25 tokens with pools (or on Flap) for their Transfers,
which name the wallet a swap does not. Price and curve progress come from the events
themselves. Only the few trades that will be
posted (at most four a tick) cost more: the wallet's balance, and the transaction's
sender when no Transfer named the wallet. Sells go only to chats that turned them on.

A sale by the token's creator (Pons deployer, Flap or pool token creator) is a 🚨 DEV
SELL: posted in every chat of the token, sells on or off, whatever its size. It is told
by the creator's own Transfer in the sale's transaction, so a sale through a router or a
trading bot counts too. Pool and Flap tokens' Transfers are read anyway (above); for
Pons tokens still on the curve one more eth_getLogs, in the same batch, asks only for
Transfers sent by their creators.

A pool trades against its own quote (ETH, USDG, a stock token...); its price and size
are converted into the token's main quote with the dollar rates of both, so charts
and market caps stay in one unit. A trade that cannot be converted is still posted,
but kept out of the chart.
"""

import asyncio
import logging
import time

from . import abi, chain, flap, heavy, pools as amm
from .db import Chat, Db, Token
from .quotes import WETH, Rates, usd_value

log = logging.getLogger(__name__)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


BLOCKS_PER_SECOND = 10    # Robinhood Chain: a block about every 0.1 s
TRADES_KEPT = 26 * 3600   # seconds of trades kept for charts
# marks a token's last day as loaded; a new name loads every token's day once more
# (v2: with the wallets of v4 buys)
BACKFILL_KEY = "backfill2:"
POOL_REFRESH = 3600       # seconds between looks for new pools of a dex token
POOL_VENUES = ("v2", "v3", "v4")
# Blocks left unread behind the head. The public RPC is several nodes behind a balancer, and
# a node a little behind the one that gave the head answers eth_getLogs for blocks it does not
# have yet with an empty list: those buys would be skipped for good. 30 blocks is ~3 s.
HEAD_LAG = 30
FLAP_TOPICS = [flap.TOPIC_BOUGHT, flap.TOPIC_SOLD, flap.TOPIC_PROGRESS, flap.TOPIC_LAUNCHED]


def pons_pool(t: Token) -> amm.Pool:
    """A graduated Pons token's Uniswap v4 pool, in the form every pool has."""
    return amm.Pool(t.address, "v4", t.pool_id, t.pair, t.quote_symbol, t.quote_decimals,
                    t.quote_kind, "Uniswap v4")


def flap_pool(t: Token, pair: str) -> amm.Pool:
    """The Uniswap v2 pair a Flap token graduates into: against its quote, WETH for ETH."""
    if t.pair == abi.ZERO_ADDRESS:
        return amm.Pool(t.address, "v2", pair, WETH, "WETH", 18, "eth", "Uniswap v2")
    return amm.Pool(t.address, "v2", pair, t.pair, t.quote_symbol, t.quote_decimals, t.quote_kind, "Uniswap v2")


def log_time(lg: dict, clock) -> int:
    """A log's block time. The public RPC fills blockTimestamp only sometimes (often
    0x0), so otherwise clock(block number) estimates it."""
    try:
        ts = int(lg.get("blockTimestamp") or "0x0", 16)
    except (TypeError, ValueError):
        ts = 0
    return ts if ts > 0 else int(clock(int(lg["blockNumber"], 16)))


def trade_rows(events: list) -> list[tuple]:
    """Kept trades (token, uid, ts, price, quote, side, wallet) from decoded events, in
    the token's main quote; trades that could not be converted into it are left out."""
    return [(t.address, dedupe, p["ts"], p["price"], p["quote_main"], 1 if kind == "buy" else -1,
             p.get("buyer"))
            for _, _, kind, t, dedupe, p in events
            if kind in ("buy", "sell") and p.get("price") and p.get("quote_main") is not None]


def name_buyers(events: list, payouts: dict) -> None:
    """Give pool and Flap buys the wallet where the bought tokens stopped (see
    pools.payouts): a Swap names no one, and Flap's event often names a trading bot's
    contract that passes the tokens on."""
    for _, _, kind, t, _, p in events:
        if kind == "buy" and (p.get("venue") in POOL_VENUES or p.get("flap")):
            wallet = payouts.get((t.address, p["tx"]))
            if wallet:
                p["buyer"] = wallet
                p["dev"] = wallet == t.deployer


# Contracts that move everyone's tokens: never "the dev", even where a record names one.
NOT_A_DEV = {abi.ZERO_ADDRESS, chain.POOL_MANAGER, chain.PONS_FACTORY, flap.PORTAL} | amm.ROUTERS


def dev_moves(logs: list[dict], devs: dict[str, str]) -> set[tuple[str, str]]:
    """(token, tx) of every Transfer of a token sent by its creator (devs: token -> creator)."""
    out = set()
    for lg in logs:
        t = [x.lower() for x in lg.get("topics") or []]
        if len(t) != 3 or t[0] != chain.TOPIC_TRANSFER:
            continue
        token = lg.get("address", "").lower()
        dev = devs.get(token)
        if dev and dev not in NOT_A_DEV and abi.topic_address(t[1]) == dev and lg.get("transactionHash"):
            out.add((token, lg["transactionHash"].lower()))
    return out


def mark_dev_sells(events: list, moves: set[tuple[str, str]]) -> None:
    """A sell is the dev's when the creator sold in person (the event names them) or sent
    the token in that transaction (through a router or a bot's contract). The post then
    names the creator, whose balance says how much of the bag went."""
    for _, _, kind, t, _, p in events:
        if kind != "sell" or not t.deployer or t.deployer in NOT_A_DEV:
            continue
        if p.get("buyer") == t.deployer or (t.address, p["tx"]) in moves:
            p.update(buyer=t.deployer, dev=True, dev_sell=True)


def wanted(chat: Chat, kind: str, p: dict) -> bool:
    """Whether a chat gets a trade: a sell only with sells on, a trade only from its minimum
    up. A dev sell always."""
    if kind == "sell" and p.get("dev_sell"):
        return True
    if kind == "sell" and not chat.sells:
        return False
    return kind not in ("buy", "sell") or passes_filter(chat, p["usd"])


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
    def __init__(self, rpc, db: Db, eth_usd, *, rates=None, poll_seconds: float = 3.0,
                 max_range: int = 2000, max_catchup: int = 3000, chunk: int = 100,
                 phase_refresh: float = 300.0, pool_refresh: float = POOL_REFRESH, head_lag: int = 0):
        self.rpc = rpc
        self.db = db
        self.eth_usd = eth_usd
        self.rates = rates if rates is not None else Rates(rpc, eth_usd)
        self.poll_seconds = poll_seconds
        self.max_range = max_range
        self.max_catchup = max_catchup
        self.chunk = chunk
        self.phase_refresh = phase_refresh
        self.pool_refresh = pool_refresh
        self.head_lag = head_lag
        self._phase_checked = 0.0
        self._pools_checked: dict[str, float] = {}
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
                if "Cloudflare" in str(e):  # the RPC shut this IP out for a while: no traceback
                    log.warning("watcher tick skipped: %s", e)
                else:
                    log.exception("watcher tick failed")
                backoff = min(backoff * 2, 60)
            await asyncio.sleep(backoff)

    async def tick(self) -> int:
        head = await self.rpc.block_number() - self.head_lag
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
        while catching up): ask the factory or Flap's Portal directly, a few calls every
        few minutes."""
        self._phase_checked = time.time()
        for t in tokens:
            if t.kind == "flap":
                st = await flap.state(self.rpc, t.address)
                if st and st.status == flap.STATUS_DEX and st.pool:
                    log.info("%s left the Flap curve (found by phase check)", t.symbol)
                    self.db.graduate_to_pools(t.address, [flap_pool(t, st.pool)])
                continue
            if t.graduated or t.kind != "pons":
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

    async def pool_refresher(self, every: float = 60.0) -> None:
        """Runs beside the ticks (a look can take a few seconds of RPC calls, which must
        not hold up the posts): every minute, the dex tokens that are due."""
        while True:
            await asyncio.sleep(every)
            try:
                if self.head is not None:
                    await self.refresh_pools(self.db.tracked_tokens(), self.head)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pool refresh failed")

    async def refresh_pools(self, tokens: list[Token], head: int, per_tick: int = 1) -> None:
        """Look again for the pools of a dex token once an hour (a new pool, or liquidity
        that moved), `per_tick` tokens at a time. A look that finds nothing keeps the
        pools there were."""
        now = time.time()
        due = []
        for t in tokens:
            if t.kind != "dex":
                continue
            seen = self._pools_checked.setdefault(t.address, now)
            if now - seen > self.pool_refresh:
                due.append((seen, t))
        for _, t in sorted(due, key=lambda d: d[0])[:per_tick]:
            self._pools_checked[t.address] = now
            try:
                found = await amm.find_pools(self.rpc, self.rates, t.address, head)
            except Exception as e:
                log.info("pool refresh for %s failed: %s", t.symbol, e)
                continue
            if found:
                self.db.set_pools(t.address, found)
                main = found[0]
                self.db.set_main_quote(t.address, main.quote, main.quote_symbol, main.quote_decimals,
                                       main.quote_kind)

    def markets(self, tokens: list[Token], graduated_now=()) -> dict[str, tuple[Token, amm.Pool]]:
        """Every pool to read, by v4 PoolId or pool address: graduated Pons tokens' v4
        pools (also those that graduate inside this range, so no swap right after it is
        missed) and the pools of dex tokens."""
        by_addr = {t.address: t for t in tokens}
        out = {}
        for t in tokens:
            if t.kind == "pons" and (t.graduated or t.address in graduated_now):
                out[t.pool_id] = (t, pons_pool(t))
        for p in self.db.tracked_pools():
            if p.token in by_addr:
                out[p.id] = (by_addr[p.token], p)
        return out

    def pool_queries(self, frm: int, to: int, markets: dict) -> list[tuple]:
        """The eth_getLogs for the swaps of the given pools (and v2 syncs, which carry a
        pair's reserves), as (from, to, address, topics)."""
        by_venue: dict[str, list[str]] = {}
        for key, (_, p) in markets.items():
            by_venue.setdefault(p.venue, []).append(key)
        out = []
        for chunk in _chunks(by_venue.get("v4", []), self.chunk):
            out.append((frm, to, chain.POOL_MANAGER, [chain.TOPIC_V4_SWAP, chunk]))
        for chunk in _chunks(by_venue.get("v3", []), self.chunk):
            out.append((frm, to, chunk, [amm.TOPIC_V3_SWAP]))
        for chunk in _chunks(by_venue.get("v2", []), self.chunk):
            out.append((frm, to, chunk, [[amm.TOPIC_V2_SWAP, amm.TOPIC_V2_SYNC]]))
        return out

    def payout_queries(self, frm: int, to: int, markets: dict, flap_tokens=()) -> tuple[list[tuple], list[set]]:
        """The eth_getLogs for every Transfer of the traded tokens, not only the pools' own:
        the hops after the pool (a bot's contract passing the tokens to the wallet) come
        from others. Each with the payers its tokens leave from (see pools.payouts)."""
        payers: dict[str, set[str]] = {}
        for _, (t, p) in markets.items():
            payers.setdefault(t.address, set()).add(chain.POOL_MANAGER if p.venue == "v4" else p.id)
        for address in flap_tokens:
            payers.setdefault(address, set()).add(flap.PORTAL)
        queries, sets = [], []
        for chunk in _chunks(sorted(payers), 25):
            queries.append((frm, to, chunk, [chain.TOPIC_TRANSFER]))
            sets.append(set().union(*(payers[a] for a in chunk)))
        return queries, sets

    async def logs_each(self, queries: list[tuple]) -> list[list[dict]]:
        """The answers to several eth_getLogs, in one batch when the client can: one HTTP
        request a tick instead of one a query (the public RPC's Cloudflare counts them)."""
        many = getattr(self.rpc, "get_logs_many", None)
        if many is not None:
            return await many(queries)
        return [await self.rpc.get_logs(*q) for q in queries]

    async def read_markets(self, frm: int, to: int, markets: dict, flap_tokens=(),
                           first=(), devs=None) -> tuple[list[dict], dict, set]:
        """The `first` queries (the curves, the Portal, creators' Transfers), the pools'
        swaps and the payouts of their tokens, all asked together. Returns (the logs of
        `first` and the swaps, (token, tx) -> the wallet the token went to, (token, tx)
        where the token's creator sent it: see dev_moves)."""
        lead = list(first) + self.pool_queries(frm, to, markets)
        transfers, payers = self.payout_queries(frm, to, markets, flap_tokens)
        parts = await self.logs_each(lead + transfers)
        logs = [lg for part in parts[:len(lead)] for lg in part]
        payouts, moves = {}, set()
        for part, who in zip(parts[len(lead):], payers):
            payouts.update(amm.payouts(part, who))
        if devs:
            for part in parts:
                moves |= dev_moves(part, devs)
        return logs, payouts, moves

    async def collect(self, frm: int, to: int, tokens: list[Token]):
        pons = [t for t in tokens if t.kind == "pons"]
        by_curve = {t.curve: t for t in pons}
        by_addr = {t.address: t for t in tokens}

        first = []
        # Only tokens still on the curve can buy there or graduate. Once every tracked
        # token has graduated, this query is skipped entirely.
        curves = [t.curve for t in pons if not t.graduated]
        for i, chunk in enumerate(_chunks(curves, self.chunk)):
            addresses = chunk + ([chain.PONS_FACTORY] if i == 0 else [])
            first.append((frm, to, addresses, [[chain.TOPIC_CURVE_BUY, chain.TOPIC_CURVE_SELL,
                                                chain.TOPIC_GRADUATED]]))
        flaps = [t.address for t in tokens if t.kind == "flap"]
        if flaps:  # the Portal's events name no token in their topics: read them all
            first.append((frm, to, flap.PORTAL, [FLAP_TOPICS]))
        devs = {t.address: t.deployer for t in tokens if t.deployer and t.deployer not in NOT_A_DEV}
        # a curve token's Transfers are not read otherwise: only those its creator sent
        on_curve = [t.address for t in pons if not t.graduated and t.address in devs]
        for chunk in _chunks(on_curve, self.chunk):
            first.append((frm, to, chunk, [chain.TOPIC_TRANSFER,
                                           sorted({abi.address_topic(devs[a]) for a in chunk})]))
        # the whole tick in one request: curves, Portal, creators, pools and payouts
        known = self.markets(tokens)
        logs, payouts, moves = await self.read_markets(frm, to, known, flaps, first, devs)

        graduated_now = set()
        launched = {}  # Flap token -> the Uniswap v2 pair it graduated into
        for lg in logs:
            topic0 = (lg.get("topics") or [""])[0].lower()
            if topic0 == chain.TOPIC_GRADUATED:
                g = chain.decode_graduated(lg)
                if g and g.token in by_addr:
                    graduated_now.add(g.token)
            elif topic0 == flap.TOPIC_LAUNCHED:
                d = flap.decode_launched(lg)
                if d and d[0] in flaps:
                    launched[d[0]] = d[1]

        markets = self.markets(tokens, graduated_now)
        for address, pair in launched.items():  # its pair trades from the graduation on
            markets[pair] = (by_addr[address], flap_pool(by_addr[address], pair))
        opened = {k: v for k, v in markets.items() if k not in known}
        if opened:  # pools that opened inside this range (rare): one more request
            more, more_payouts, more_moves = await self.read_markets(frm, to, opened, list(launched), devs=devs)
            logs += more
            payouts.update(more_payouts)
            moves |= more_moves

        rates = await self.rates.table({t.quote_kind for t in tokens}
                                       | {p.quote_kind for _, p in markets.values()})
        now, head = time.time(), self.head or to
        events = self._events(logs, by_curve, by_addr, markets, rates,
                              lambda b: now - (head - b) / BLOCKS_PER_SECOND)
        name_buyers(events, payouts)
        mark_dev_sells(events, moves)
        events = events + self._milestones(events)
        chats_cache: dict[str, list[Chat]] = {}
        rows = []
        for _, _, kind, t, dedupe, payload in events:
            chats = chats_cache.setdefault(t.address, self.db.chats_for_token(t.address))
            rows += [(chat.chat_id, kind, dedupe, payload) for chat in chats if wanted(chat, kind, payload)]
        posted = [(p_kind, t, p) for _, _, p_kind, t, _, p in events
                  if p_kind in ("buy", "sell") and any(r[3] is p for r in rows)]
        # dev sells first: their share of the bag is what the post is about
        await self._about_traders(sorted(posted, key=lambda x: not x[2].get("dev_sell")))
        for address, pair in launched.items():
            log.info("%s left the Flap curve for the pair %s", by_addr[address].symbol, pair)
            self.db.graduate_to_pools(address, [flap_pool(by_addr[address], pair)])
        return rows, sorted(graduated_now), trade_rows(events)

    async def _about_traders(self, trades: list[tuple[str, Token, dict]], limit: int = 4) -> None:
        """For the few trades that will be posted (at most `limit` a tick; the rest go
        out without this rather than late): who traded, and what they hold now.

        A pool's Swap names the router, not the trader; when no Transfer named the
        wallet, it is the one that sent the transaction. The wallet's balance after the
        trade says whether it is a new holder and how much its position changed. A
        balance that cannot hold what was just bought means the tokens went elsewhere:
        then nothing is said about it."""
        for kind, t, p in trades[:limit]:
            if p["venue"] in POOL_VENUES and p.get("buyer") is None and hasattr(self.rpc, "tx_sender"):
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
                    "quote": p["quote"], "quote_symbol": p.get("quote_symbol", t.quote_symbol), "tx": p["tx"]}))
        return out

    def _events(self, logs: list[dict], by_curve: dict, by_addr: dict, markets: dict, rates: dict, clock):
        """Decoded trades and graduations of the given tokens, oldest first:
        (block, log_index, kind, token, dedupe, payload)."""
        syncs = amm.v2_syncs(logs)
        progress = {}  # (token, tx) -> how full its Flap curve is after that transaction
        for lg in logs:
            got = flap.decode_progress(lg) if (lg.get("topics") or [""])[0].lower() == flap.TOPIC_PROGRESS else None
            if got:
                progress[(got[0], got[1])] = got[2]
        events = []
        for lg in logs:
            topic0 = (lg.get("topics") or [""])[0].lower()
            ev = None
            if topic0 == chain.TOPIC_CURVE_BUY:
                b = chain.decode_curve_buy(lg)
                t = by_curve.get(b.curve) if b else None
                ev = self._curve_buy_event(b, t, rates) if t else None
            elif topic0 == chain.TOPIC_CURVE_SELL:
                sl = chain.decode_curve_sell(lg)
                t = by_curve.get(sl.curve) if sl else None
                ev = self._curve_sell_event(sl, t, rates) if t else None
            elif topic0 == chain.TOPIC_GRADUATED:
                g = chain.decode_graduated(lg)
                t = by_addr.get(g.token) if g else None
                if t:
                    raised = g.quote_raised / 10 ** t.quote_decimals
                    ev = (g.block, g.log_index, "grad", t, f"grad:{g.tx}:{g.log_index}", {
                        "token": t.address, "symbol": t.symbol, "name": t.name,
                        "raised": raised, "quote_symbol": t.quote_symbol,
                        "usd": usd_value(raised, t.quote_kind, rates), "tx": g.tx, "dex": "Uniswap v4"})
            elif topic0 in (flap.TOPIC_BOUGHT, flap.TOPIC_SOLD):
                tr = flap.decode_trade(lg)
                t = by_addr.get(tr.token) if tr else None
                if t and t.kind == "flap":
                    ev = self._flap_trade_event(tr, t, progress.get((tr.token, tr.tx)), rates)
            elif topic0 == flap.TOPIC_LAUNCHED:
                d = flap.decode_launched(lg)
                t = by_addr.get(d[0]) if d else None
                if t and t.kind == "flap":
                    raised = d[3] / 10 ** t.quote_decimals
                    ev = (d[4], d[6], "grad", t, f"grad:{d[5]}:{d[6]}", {
                        "token": t.address, "symbol": t.symbol, "name": t.name, "raised": raised,
                        "quote_symbol": t.quote_symbol, "usd": usd_value(raised, t.quote_kind, rates),
                        "tx": d[5], "dex": "Uniswap v2"})
            elif topic0 == chain.TOPIC_V4_SWAP:
                s = chain.decode_v4_swap(lg)
                m = markets.get(s.pool_id) if s else None
                ev = self._v4_trade_event(s, *m, rates) if m else None
            elif topic0 == amm.TOPIC_V3_SWAP:
                s = amm.decode_v3_swap(lg)
                m = markets.get(s.pool) if s else None
                ev = self._v3_trade_event(s, *m, rates) if m and m[1].venue == "v3" else None
            elif topic0 == amm.TOPIC_V2_SWAP:
                s = amm.decode_v2_swap(lg)
                m = markets.get(s.pair) if s else None
                ev = (self._v2_trade_event(s, *m, amm.sync_before(syncs, s), rates)
                      if m and m[1].venue == "v2" else None)
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
        A few eth_getLogs calls per window: the RPC takes wide block ranges when the
        query names the addresses. One backfill at a time (heavy.py); a window is only
        a megabyte or two, so cards are drawn between and alongside them."""
        async with heavy.backfill():
            return await self._backfill(t, hours, window)

    async def _backfill(self, t: Token, hours: float, window: int) -> int:
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

        if t.kind == "pons":
            markets = {t.pool_id: (t, pons_pool(t))} if t.graduated else {}
        elif t.kind == "flap":
            markets = {}
        else:
            markets = {p.id: (t, p) for p in self.db.pools_of(t.address)}
        rates = await self.rates.table({t.quote_kind} | {p.quote_kind for _, p in markets.values()})
        added = 0
        # window by window, so a busy token's day of logs is never all in memory at once
        for frm in range(start, head + 1, window):
            to = min(head, frm + window - 1)
            first = []
            if t.kind == "pons":
                first.append((frm, to, t.curve, [[chain.TOPIC_CURVE_BUY, chain.TOPIC_CURVE_SELL]]))
            elif t.kind == "flap":
                first.append((frm, to, flap.PORTAL, [[flap.TOPIC_BOUGHT, flap.TOPIC_SOLD]]))
            logs, payouts, _ = await self.read_markets(frm, to, markets,
                                                       [t.address] if t.kind == "flap" else (), first)
            events = self._events(logs, {t.curve: t} if t.kind == "pons" else {}, {t.address: t},
                                  markets, rates, clock)
            events = [e for e in events if e[2] in ("buy", "sell")]
            name_buyers(events, payouts)
            rows = trade_rows(events)
            self.db.add_trades(rows)
            added += len(rows)
        if hours >= 24:
            self.db.set_meta(f"{BACKFILL_KEY}{t.address}", str(int(now)))
        log.info("backfilled %d trades of %s", added, t.symbol)
        return added

    def _payload(self, t: Token, pool: amm.Pool | None, quote_raw: int, tokens_raw: int,
                 price: float | None, progress: float | None, venue: str, buyer: str | None,
                 tx: str, rates: dict) -> dict:
        """A trade's post data. `price` is in the trade's own quote (the pool's, or the
        token's on the curve); the market cap, the chart price and the kept size are in
        the token's main quote."""
        if pool is not None:
            q_symbol, q_decimals, q_kind = pool.quote_symbol, pool.quote_decimals, pool.quote_kind
        else:
            q_symbol, q_decimals, q_kind = t.quote_symbol, t.quote_decimals, t.quote_kind
        quote = quote_raw / 10 ** q_decimals
        rate, main_rate = rates.get(q_kind), rates.get(t.quote_kind)
        if pool is None or pool.quote == t.pair:
            to_main = 1.0
        else:
            to_main = rate / main_rate if rate and main_rate else None
        price_main = price * to_main if price and to_main else None
        mc_quote = price_main * t.total_supply / 10 ** t.decimals if price_main else None
        return {
            "token": t.address, "symbol": t.symbol, "name": t.name,
            "quote_symbol": q_symbol, "quote": quote, "usd": usd_value(quote, q_kind, rates),
            "tokens": tokens_raw / 10 ** t.decimals,
            "buyer": buyer, "dev": bool(buyer and buyer == t.deployer), "tx": tx,
            "mc_quote": mc_quote, "mc_symbol": t.quote_symbol,
            "mc_usd": mc_quote * main_rate if mc_quote and main_rate else None,
            "progress": progress, "venue": venue, "dex": pool.dex if pool else None,
            "price": price_main, "quote_main": quote * to_main if to_main else None,
            "main_usd": main_rate, "deployer": t.deployer,
        }

    def _curve_buy_event(self, b: chain.CurveBuy, t: Token, rates):
        price, progress = chain.curve_after_buy(b, t.curve_k, t.phantom, t.threshold,
                                                t.decimals, t.quote_decimals)
        if price is None:  # unknown curve shape: fall back to this trade's own average price
            price = chain.trade_price(b.quote_in - b.fee - b.tax, b.tokens_out,
                                      t.decimals, t.quote_decimals)
        payload = self._payload(t, None, b.quote_in, b.tokens_out, price, progress, "curve",
                                b.recipient, b.tx, rates)
        return b.block, b.log_index, "buy", t, f"buy:{b.tx}:{b.log_index}", payload

    def _curve_sell_event(self, sl: chain.CurveSell, t: Token, rates):
        price, progress = chain.curve_after_sell(sl, t.curve_k, t.phantom, t.threshold,
                                                 t.decimals, t.quote_decimals)
        if price is None:
            price = chain.trade_price(sl.quote_out + sl.fee + sl.tax, sl.tokens_in,
                                      t.decimals, t.quote_decimals)
        payload = self._payload(t, None, sl.quote_out, sl.tokens_in, price, progress, "curve",
                                sl.recipient, sl.tx, rates)
        return sl.block, sl.log_index, "sell", t, f"sell:{sl.tx}:{sl.log_index}", payload

    def _flap_trade_event(self, tr: flap.FlapTrade, t: Token, progress: float | None, rates):
        # postPrice is quote per whole token (18 decimals whatever the quote's own)
        payload = self._payload(t, None, tr.eth, tr.tokens, tr.price, progress, "curve", tr.trader, tr.tx, rates)
        payload["flap"] = True
        return tr.block, tr.log_index, tr.kind, t, f"{tr.kind}:{tr.tx}:{tr.log_index}", payload

    def _pool_event(self, kind: str, quote_raw: int, tokens_raw: int, price, t: Token,
                    pool: amm.Pool, block: int, index: int, tx: str, rates):
        payload = self._payload(t, pool, quote_raw, tokens_raw, price, None, pool.venue, None, tx, rates)
        return block, index, kind, t, f"{kind}:{tx}:{index}", payload

    def _v4_trade_event(self, s: chain.V4Swap, t: Token, pool: amm.Pool, rates):
        buy = chain.v4_buy_amounts(s, t.address, pool.quote)
        sell = None if buy else chain.v4_sell_amounts(s, t.address, pool.quote)
        if not (buy or sell):
            return None
        price = chain.v4_token_price(s.sqrt_price_x96, t.address, pool.quote, t.decimals, pool.quote_decimals)
        return self._pool_event("buy" if buy else "sell", *(buy or sell), price, t, pool,
                                s.block, s.log_index, s.tx, rates)

    def _v3_trade_event(self, s: amm.V3Swap, t: Token, pool: amm.Pool, rates):
        trade = amm.v3_trade(s, t.address, pool.quote)
        if not trade:
            return None
        price = chain.v4_token_price(s.sqrt_price_x96, t.address, pool.quote, t.decimals, pool.quote_decimals)
        return self._pool_event(trade[0], trade[1], trade[2], price, t, pool, s.block, s.log_index, s.tx, rates)

    def _v2_trade_event(self, s: amm.V2Swap, t: Token, pool: amm.Pool, reserves, rates):
        trade = amm.v2_trade(s, t.address, pool.quote)
        if not trade:
            return None
        price = (amm.v2_price(*reserves, t.address, pool.quote, t.decimals, pool.quote_decimals)
                 if reserves else None)
        if price is None:  # no Sync seen: the trade's own average price
            price = chain.trade_price(trade[1], trade[2], t.decimals, pool.quote_decimals)
        return self._pool_event(trade[0], trade[1], trade[2], price, t, pool, s.block, s.log_index, s.tx, rates)
