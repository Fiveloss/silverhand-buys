"""Uniswap-style pools of any token: Uniswap v2 pairs, v3 pools (and Ramses v3, a
fork with the same events) and v4 pools, as found and read on Robinhood Chain.

Facts (each checked against mainnet by tools/preflight.py):
- The Uniswap v2 and v3 factories and the v4 PoolManager are Uniswap's own
  deployments on chain 4663 (developers.uniswap.org). Ramses v3's factory is the one
  DexScreener lists. Only pools created by these are accepted: any contract can emit
  a "Swap" event, so a pool from an unknown factory could post fake buys.
- A pool is found from its creation event over the whole chain, which the public RPC
  answers in one call when the token is a topic filter: v2 PairCreated, v3
  PoolCreated, v4 Initialize. The other currency must be a quote the bot can value
  (quotes.QUOTES); pools against anything else are used only when there is nothing
  better.
- Deltas: a v3 Swap reports amounts from the pool's side (positive = paid into the
  pool), v4 from the swapper's side (positive = taken out). A v2 Swap gives in/out
  amounts, and the Sync right before it in the same transaction the reserves after.
- v4 keeps every pool's state inside the PoolManager: slot0 (sqrtPriceX96 in its low
  160 bits) at keccak256(poolId . 6) and the active liquidity three slots after it,
  readable through extsload.
"""

import logging
from dataclasses import dataclass

from . import abi, quotes
from .chain import POOL_MANAGER, TOPIC_TRANSFER, TOPIC_V4_SWAP, currencies, v4_token_price
from .keccak import keccak256

log = logging.getLogger(__name__)

# Swap(address indexed sender, address indexed recipient, int256 amount0, int256 amount1,
#      uint160 sqrtPriceX96, uint128 liquidity, int24 tick)
TOPIC_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
# Swap(address indexed sender, uint256 amount0In, uint256 amount1In, uint256 amount0Out,
#      uint256 amount1Out, address indexed to)
TOPIC_V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
# Sync(uint112 reserve0, uint112 reserve1)
TOPIC_V2_SYNC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"
# PoolCreated(address indexed token0, address indexed token1, uint24 indexed fee,
#             int24 tickSpacing, address pool)
TOPIC_V3_POOL_CREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
# PairCreated(address indexed token0, address indexed token1, address pair, uint256)
TOPIC_V2_PAIR_CREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
# Initialize(bytes32 indexed id, address indexed currency0, address indexed currency1,
#            uint24 fee, int24 tickSpacing, address hooks, uint160 sqrtPriceX96, int24 tick)
TOPIC_V4_INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"

SEL_EXTSLOAD = "0x1e2eaeaf"         # extsload(bytes32), on the PoolManager
SEL_V2_RESERVES = "0x0902f1ac"      # getReserves() on a v2 pair
SEL_V3_SLOT0 = "0x3850c7bd"         # slot0() on a v3 pool: sqrtPriceX96 first
SEL_V3_LIQUIDITY = "0x1a686502"     # liquidity() on a v3 pool: the active liquidity
V4_POOLS_SLOT = 6
V4_LIQUIDITY_OFFSET = 3

V3_FACTORIES = {
    "0x1f7d7550b1b028f7571e69a784071f0205fd2efa": "Uniswap v3",
    "0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8": "Ramses v3",
}
V2_FACTORIES = {
    "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f": "Uniswap v2",
}
# The first block with a pool of each factory, where it is past the chain's first 10M
# blocks (looked up on mainnet in Oct 2026); the others are asked from block 0.
FACTORY_FROM = {
    "0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8": 16_079_383,  # Ramses v3
}
# Routers can receive a token on a trader's behalf; they are never "the buyer".
ROUTERS = {
    POOL_MANAGER,
    "0x8876789976decbfcbbbe364623c63652db8c0904",  # Universal Router
    "0x204faca1764b154221e35c0d20abb3c525710498",  # Universal Router 2.1.2
    "0xcaf681a66d020601342297493863e78c959e5cb2",  # SwapRouter02
    "0x89e5db8b5aa49aa85ac63f691524311aeb649eba",  # Uniswap v2 router
}

MIN_POOL_USD = 1_000     # pools besides the deepest need this much liquidity ...
MIN_POOL_SHARE = 0.02    # ... and 2% of the deepest one's
MIN_MAIN_USD = 100       # the deepest pool itself needs only this: a token just launched
MAX_POOLS = 6
MAX_READS = 12           # pools whose liquidity is read, busiest first
ACTIVE_WINDOWS = (36_000, 864_000)  # blocks: the last hour, then the last day


@dataclass(frozen=True)
class Pool:
    token: str
    venue: str            # "v2" | "v3" | "v4"
    id: str               # the pair or pool address; for v4 the PoolId
    quote: str
    quote_symbol: str
    quote_decimals: int
    quote_kind: str       # quotes.QUOTES kinds, or "other"
    dex: str              # "Uniswap v3", "Ramses v3" ...
    liquidity_usd: float | None = None


@dataclass(frozen=True)
class Candidate:
    venue: str
    id: str
    quote: str
    dex: str
    block: int = 0  # when the pool was created


@dataclass(frozen=True)
class V3Swap:
    pool: str
    amount0: int   # the pool's delta: positive = paid into the pool
    amount1: int
    sqrt_price_x96: int
    block: int
    tx: str
    log_index: int


@dataclass(frozen=True)
class V2Swap:
    pair: str
    amount0_in: int
    amount1_in: int
    amount0_out: int
    amount1_out: int
    block: int
    tx: str
    log_index: int


def _pos(lg: dict) -> tuple[int, str, int] | None:
    try:
        return int(lg["blockNumber"], 16), lg["transactionHash"].lower(), int(lg["logIndex"], 16)
    except (KeyError, TypeError, ValueError):
        return None


def _topic0(lg: dict) -> str:
    return (lg.get("topics") or [""])[0].lower()


def decode_v3_swap(lg: dict) -> V3Swap | None:
    pos = _pos(lg)
    if _topic0(lg) != TOPIC_V3_SWAP or pos is None or len(lg.get("topics") or []) != 3:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    a0, a1, sp = abi.word_int(data, 0), abi.word_int(data, 1), abi.word_uint(data, 2)
    if None in (a0, a1, sp):
        return None
    return V3Swap(lg["address"].lower(), a0, a1, sp, *pos)


def decode_v2_swap(lg: dict) -> V2Swap | None:
    pos = _pos(lg)
    if _topic0(lg) != TOPIC_V2_SWAP or pos is None or len(lg.get("topics") or []) != 3:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    vals = [abi.word_uint(data, i) for i in range(4)]
    if None in vals:
        return None
    return V2Swap(lg["address"].lower(), *vals, *pos)


def decode_v2_sync(lg: dict) -> tuple[str, str, int, int, int] | None:
    """(pair, tx, log index, reserve0, reserve1)."""
    pos = _pos(lg)
    if _topic0(lg) != TOPIC_V2_SYNC or pos is None:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    r0, r1 = abi.word_uint(data, 0), abi.word_uint(data, 1)
    if r0 is None or r1 is None:
        return None
    return lg["address"].lower(), pos[1], pos[2], r0, r1


def v3_trade(swap: V3Swap, token: str, quote: str) -> tuple[str, int, int] | None:
    """("buy" | "sell", quote amount, token amount) of a v3 swap, from the pool's deltas."""
    c0, _ = currencies(token, quote)
    token_delta, quote_delta = ((swap.amount0, swap.amount1) if c0 == token.lower()
                                else (swap.amount1, swap.amount0))
    if token_delta < 0 < quote_delta:
        return "buy", quote_delta, -token_delta
    if quote_delta < 0 < token_delta:
        return "sell", -quote_delta, token_delta
    return None


def v2_trade(swap: V2Swap, token: str, quote: str) -> tuple[str, int, int] | None:
    c0, _ = currencies(token, quote)
    if c0 == token.lower():
        t_out, t_in, q_in, q_out = swap.amount0_out, swap.amount0_in, swap.amount1_in, swap.amount1_out
    else:
        t_out, t_in, q_in, q_out = swap.amount1_out, swap.amount1_in, swap.amount0_in, swap.amount0_out
    if t_out > t_in and q_in > q_out:
        return "buy", q_in - q_out, t_out - t_in
    if t_in > t_out and q_out > q_in:
        return "sell", q_out - q_in, t_in - t_out
    return None


def v2_price(reserve0: int, reserve1: int, token: str, quote: str,
             token_decimals: int, quote_decimals: int) -> float | None:
    """Price of one whole token in whole quote units, from a pair's reserves."""
    c0, _ = currencies(token, quote)
    r_token, r_quote = (reserve0, reserve1) if c0 == token.lower() else (reserve1, reserve0)
    if r_token <= 0:
        return None
    return r_quote / r_token * 10 ** (token_decimals - quote_decimals)


def v2_syncs(logs: list[dict]) -> dict[tuple[str, str], list[tuple[int, int, int]]]:
    """(pair, tx) -> [(log index, reserve0, reserve1)] of the Sync events in the logs."""
    out: dict = {}
    for lg in logs:
        s = decode_v2_sync(lg)
        if s:
            out.setdefault((s[0], s[1]), []).append((s[2], s[3], s[4]))
    return out


def sync_before(syncs: dict, swap: V2Swap) -> tuple[int, int] | None:
    """The reserves a v2 swap left: the pair's last Sync before it in its transaction."""
    before = [s for s in syncs.get((swap.pair, swap.tx), []) if s[0] < swap.log_index]
    if not before:
        return None
    _, r0, r1 = max(before)
    return r0, r1


MAX_HOPS = 5


def payouts(logs: list[dict], payers: set[str]) -> dict[tuple[str, str], str]:
    """(token, tx) -> the wallet that bought the token in that transaction.

    A pool (or the PoolManager) sends the token to someone; a trading bot's contract or
    a router then often passes the same amount on, hop by hop, in the same transaction
    (seen on mainnet: pool -> bot -> ... -> wallet). The wallet is where the tokens stop.
    An end that is a known router is not a wallet, so nothing is named then."""
    moves: dict[tuple[str, str], list[tuple[int, str, str, int]]] = {}
    for lg in logs:
        t = [x.lower() for x in lg.get("topics") or []]
        if len(t) != 3 or t[0] != TOPIC_TRANSFER:
            continue
        frm, to = abi.topic_address(t[1]), abi.topic_address(t[2])
        value = abi.decode_uint(lg.get("data") or "0x")
        pos = _pos(lg)
        if frm and to and value is not None and pos:
            key = (lg["address"].lower(), pos[1])
            moves.setdefault(key, []).append((pos[2], frm, to, value))
    out = {}
    for key, mv in moves.items():
        mv.sort()
        hop = next(((i, to, v) for i, frm, to, v in mv if frm in payers and to not in payers), None)
        if hop is None:
            continue
        for _ in range(MAX_HOPS):
            i, who, v = hop
            nxt = next(((j, to, w) for j, frm, to, w in mv
                        if j > i and frm == who and to not in payers and w * 100 >= v * 99), None)
            if nxt is None:
                break
            hop = nxt
        if hop[1] not in ROUTERS:
            out[key] = hop[1]
    return out


# --- finding a token's pools -------------------------------------------------------

def v4_state_slot(pool_id: str) -> int:
    key = bytes.fromhex(pool_id[2:]) + V4_POOLS_SLOT.to_bytes(32, "big")
    return int.from_bytes(keccak256(key), "big")


async def v4_state(rpc, pool_id: str) -> tuple[int, int] | None:
    """(sqrtPriceX96, active liquidity) of a v4 pool, read from the PoolManager."""
    slot = v4_state_slot(pool_id)
    s0 = abi.decode_uint(await rpc.eth_call(POOL_MANAGER, SEL_EXTSLOAD + format(slot, "064x")) or "0x")
    liq = abi.decode_uint(await rpc.eth_call(
        POOL_MANAGER, SEL_EXTSLOAD + format(slot + V4_LIQUIDITY_OFFSET, "064x")) or "0x")
    if s0 is None or liq is None:
        return None
    return s0 & ((1 << 160) - 1), liq & ((1 << 128) - 1)


def v4_quote_depth(sqrt_price_x96: int, liquidity: int, token: str, quote: str) -> int:
    """The quote side of a v4 pool's active liquidity, in quote base units: L / sqrtP
    for currency0, L * sqrtP for currency1 (a virtual reserve: it measures how deep
    the pool is around the current price, which is what ranks pools)."""
    if sqrt_price_x96 <= 0 or liquidity <= 0:
        return 0
    c0, _ = currencies(token, quote)
    if c0 == quote.lower():
        return liquidity * 2 ** 96 // sqrt_price_x96
    return liquidity * sqrt_price_x96 // 2 ** 96


async def _logs_both_ways(rpc, head: int, address, topic0: str, token: str, others: list[str] | None,
                          first_topic_is_id: bool = False, since: int = 0) -> list[dict]:
    """Creation logs with the token on either side of the pair (and, when `others` is
    given, only those whose other side is one of them). One address and one value per
    topic position: the public RPC then takes 10M blocks a query, against 100k with a list
    of either (a list of two factories over the whole chain was ~900 calls), so factories
    are asked one by one and the other side is filtered here rather than by the RPC.
    Each factory from the block it was made in (FACTORY_FROM), or from `since` (the
    token's own first block) when that is later."""
    tt = abi.address_topic(token)
    pre = [topic0, None] if first_topic_is_id else [topic0]
    pos = len(pre)  # where the first currency topic sits
    a, b = [], []
    for addr in [address] if isinstance(address, str) else address:
        frm = max(FACTORY_FROM.get(addr.lower(), 0), since)
        a += await rpc.get_logs(frm, head, address=addr, topics=pre + [tt])
        b += await rpc.get_logs(frm, head, address=addr, topics=pre + [None, tt])
    if others is None:
        return a + b
    want = {abi.address_topic(x) for x in others}
    return ([lg for lg in a if lg["topics"][pos + 1].lower() in want]
            + [lg for lg in b if lg["topics"][pos].lower() in want])


async def candidates(rpc, token: str, head: int, priced_only: bool = True, since: int = 0) -> list[Candidate]:
    """Every pool of the token from the known factories and the PoolManager (created at
    or after block `since`)."""
    token = token.lower()
    others = list(quotes.QUOTES) if priced_only else None
    found = []
    for lg in await _logs_both_ways(rpc, head, POOL_MANAGER, TOPIC_V4_INITIALIZE, token, others, True, since):
        t = [x.lower() for x in lg["topics"]]
        c0, c1 = abi.topic_address(t[2]), abi.topic_address(t[3])
        quote = c1 if c0 == token else c0
        found.append(Candidate("v4", t[1], quote, "Uniswap v4", int(lg["blockNumber"], 16)))
    for lg in await _logs_both_ways(rpc, head, list(V3_FACTORIES), TOPIC_V3_POOL_CREATED, token, others,
                                    since=since):
        t = [x.lower() for x in lg["topics"]]
        pool = abi.word_address(abi.to_bytes(lg.get("data", "0x")), 1)
        c0, c1 = abi.topic_address(t[1]), abi.topic_address(t[2])
        if pool and lg["address"].lower() in V3_FACTORIES:
            found.append(Candidate("v3", pool, c1 if c0 == token else c0, V3_FACTORIES[lg["address"].lower()],
                                   int(lg["blockNumber"], 16)))
    for lg in await _logs_both_ways(rpc, head, list(V2_FACTORIES), TOPIC_V2_PAIR_CREATED, token, others,
                                    since=since):
        t = [x.lower() for x in lg["topics"]]
        pair = abi.word_address(abi.to_bytes(lg.get("data", "0x")), 0)
        c0, c1 = abi.topic_address(t[1]), abi.topic_address(t[2])
        if pair and lg["address"].lower() in V2_FACTORIES:
            found.append(Candidate("v2", pair, c1 if c0 == token else c0, V2_FACTORIES[lg["address"].lower()],
                                   int(lg["blockNumber"], 16)))
    return [c for c in found if c.quote and c.quote != token]


async def quote_depth(rpc, c: Candidate, token: str) -> int | None:
    """The quote side of a pool's depth: a v2 pair's reserve, or for v3 and v4 the
    virtual reserve of the active liquidity around the price (v4_quote_depth). The
    virtual reserve also measures a pool launched one-sided, holding only the token."""
    if c.venue == "v4":
        state = await v4_state(rpc, c.id)
        return v4_quote_depth(*state, token, c.quote) if state else None
    if c.venue == "v2":
        data = abi.to_bytes(await rpc.eth_call(c.id, SEL_V2_RESERVES) or "0x")
        r0, r1 = abi.word_uint(data, 0), abi.word_uint(data, 1)
        if r0 is None or r1 is None:
            return None
        c0, _ = currencies(token, c.quote)
        return r1 if c0 == token.lower() else r0
    slot0 = abi.to_bytes(await rpc.eth_call(c.id, SEL_V3_SLOT0) or "0x")
    sqrt_p = abi.word_uint(slot0, 0)
    liq = abi.decode_uint(await rpc.eth_call(c.id, SEL_V3_LIQUIDITY) or "0x")
    if sqrt_p is None or liq is None:
        return None
    return v4_quote_depth(sqrt_p, liq, token, c.quote)


def choose(scored: list[tuple[Candidate, float]], limit: int = MAX_POOLS) -> list[tuple[Candidate, float]]:
    """The pools worth watching: deepest first. The deepest needs MIN_MAIN_USD; each
    other one MIN_POOL_USD and MIN_POOL_SHARE of the deepest."""
    scored = sorted((s for s in scored if s[1] > 0), key=lambda s: -s[1])
    if not scored or scored[0][1] < MIN_MAIN_USD:
        return []
    floor = max(MIN_POOL_USD, scored[0][1] * MIN_POOL_SHARE)
    return [scored[0]] + [s for s in scored[1:] if s[1] >= floor][:limit - 1]


async def swap_counts(rpc, cands: list[Candidate], frm: int, to: int, chunk: int = 100) -> dict[str, int]:
    """How many swaps each pool had in a block range: one eth_getLogs per 100 pools of a venue."""
    counts: dict[str, int] = {}
    by_venue: dict[str, list[str]] = {}
    for c in cands:
        by_venue.setdefault(c.venue, []).append(c.id)
    for venue, ids in by_venue.items():
        for i in range(0, len(ids), chunk):
            part = ids[i:i + chunk]
            if venue == "v4":
                logs = await rpc.get_logs(frm, to, address=POOL_MANAGER, topics=[TOPIC_V4_SWAP, part])
                keys = [lg["topics"][1].lower() for lg in logs]
            else:
                topic = TOPIC_V3_SWAP if venue == "v3" else TOPIC_V2_SWAP
                logs = await rpc.get_logs(frm, to, address=part, topics=[topic])
                keys = [lg["address"].lower() for lg in logs]
            for k in keys:
                counts[k] = counts.get(k, 0) + 1
    return counts


async def worth_reading(rpc, cands: list[Candidate], head: int, limit: int = MAX_READS) -> list[Candidate]:
    """At most `limit` pools to read the liquidity of. A token can have hundreds of pools
    (bots open one per fee tier; seen on mainnet: 457 for one token), and reading each
    costs two calls. The busiest of the last hour come first, else of the last day,
    else the newest."""
    if len(cands) <= limit:
        return cands
    for window in ACTIVE_WINDOWS:
        counts = await swap_counts(rpc, cands, max(0, head - window), head)
        active = sorted((c for c in cands if counts.get(c.id)), key=lambda c: -counts[c.id])
        if active:
            return active[:limit]
    return sorted(cands, key=lambda c: -c.block)[:limit]


async def find_pools(rpc, rates, token: str, head: int) -> list[Pool]:
    """The token's pools worth watching, deepest first. Priced quotes (dollars known)
    come first; if the token has none, its deepest pools against anything else."""
    token = token.lower()
    scored = []
    for c in await worth_reading(rpc, await candidates(rpc, token, head), head):
        symbol, decimals, kind = quotes.QUOTES[c.quote]
        try:
            depth = await quote_depth(rpc, c, token)
        except Exception as e:
            log.info("pool %s unreadable: %s", c.id, e)
            continue
        rate = await rates.get(kind)
        if depth and rate:
            scored.append((c, 2 * depth / 10 ** decimals * rate))
    picked = [Pool(token, c.venue, c.id, c.quote, *quotes.QUOTES[c.quote], c.dex, round(usd))
              for c, usd in choose(scored)]
    if picked:
        return picked
    return await _unpriced_pools(rpc, token, head)


async def _unpriced_pools(rpc, token: str, head: int, limit: int = 3) -> list[Pool]:
    """Pools against quotes the bot cannot value: amounts are shown in the quote's units.
    Ranked within the most common quote only, since other units do not compare."""
    from .tokens import read_erc20_meta  # late: tokens imports this module
    found = [c for c in await candidates(rpc, token, head, priced_only=False)
             if c.quote not in quotes.QUOTES]
    if not found:
        return []
    counts: dict[str, int] = {}
    for c in found:
        counts[c.quote] = counts.get(c.quote, 0) + 1
    quote = max(counts, key=lambda q: counts[q])
    meta = await read_erc20_meta(rpc, quote)
    if meta is None:
        return []
    symbol, decimals = meta
    scored = []
    for c in await worth_reading(rpc, [c for c in found if c.quote == quote], head):
        try:
            depth = await quote_depth(rpc, c, token)
        except Exception as e:
            log.info("pool %s unreadable: %s", c.id, e)
            continue
        if depth:
            scored.append((c, depth))
    scored.sort(key=lambda s: -s[1])
    return [Pool(token, c.venue, c.id, quote, symbol, decimals, "other", c.dex, None)
            for c, _ in scored[:limit]]


def price_after(venue: str, sqrt_price_x96: int | None, reserves: tuple[int, int] | None,
                token: str, quote: str, token_decimals: int, quote_decimals: int) -> float | None:
    if venue in ("v3", "v4") and sqrt_price_x96:
        return v4_token_price(sqrt_price_x96, token, quote, token_decimals, quote_decimals)
    if venue == "v2" and reserves:
        return v2_price(*reserves, token, quote, token_decimals, quote_decimals)
    return None


__all__ = ["Pool", "Candidate", "V3Swap", "V2Swap", "TOPIC_V3_SWAP", "TOPIC_V2_SWAP",
           "TOPIC_V2_SYNC", "TOPIC_V4_SWAP", "find_pools", "payouts"]
