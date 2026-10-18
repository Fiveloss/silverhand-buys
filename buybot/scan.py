"""/scan: how safe a token looks from its own history, read from its Transfer logs.

What a scan reports:
- the share of the supply in the 10 biggest wallets, leaving out where tokens sit for
  trading or are gone: the bonding curve, Flap's Portal, the pools, Uniswap's
  PoolManager and routers, the Pons factory, 0x0, 0x…dead and the token contract;
- snipers: wallets that got the token from the market in the first SNIPER_BLOCKS
  blocks after it opened (the curve funded or the first liquidity added), and how much
  of the supply they still hold;
- the dev (the Pons deployer, else whoever sent the transaction of the first mint): what
  they hold now, how much of what they got they sold into the market and how much they
  sent to other wallets;
- how many wallets have sold, and a copy-ticker warning when an older token the bot
  knows (stock tokens and other quotes, tokens chats tracked) has the same symbol.

Not checked: whether the token can be sold at all (a honeypot). That takes simulating a
sale (eth_call with state overrides, or a trace), which the public RPC is not reliable
for, so the scan says it did not check rather than guess. The number of wallets that did
sell is the plain fact shown instead.

The requests (a one-off: never on the watcher's tick; one scan at a time, and kept for
SCAN_TTL so taps in a busy chat cost nothing):
- one batch: decimals, supply, symbol, name, the Pons factory's record, Flap's getTokenV7;
- one batch: the first mint (a Transfer from 0x0) over the whole chain, in pieces of the
  RPC's 10M-block span;
- the token's pools from their creation events since that block (pools.candidates, one
  call per factory and side): pools are told by who created them, not by what a contract
  says about itself; and the dev's transaction, when the dev is not on record;
- every Transfer since the mint, window by window: the public RPC answers at most 10,000
  logs a request, so a window grows while it is quiet and shrinks when it is busy, and
  the walk stops after MAX_TRANSFERS (then holders and the dev's sales are not shown);
- one batch of balanceOf for the biggest holders, the dev and the snipers (the RPC keeps
  state for the last ~10 minutes only, which is all this needs).
A token a few days old costs about fifteen requests.
"""

import time
from dataclasses import dataclass, field

from . import abi, chain, flap, pools as amm, quotes

# Blocks after the market opens in which a buyer counts as a sniper: 50 blocks are ~5 s
# (a block every ~0.1 s). Bots buy in the opening blocks; a person who saw the launch
# and then bought is rarely that fast.
SNIPER_BLOCKS = 50
BLOCKS_PER_SECOND = 10
SCAN_TTL = 600              # seconds a scan is kept and shown again
MAX_TRANSFERS = 50_000      # a walk this long stops: memory and the RPC's patience
MAX_WINDOWS = 40
FIRST_WINDOW = 100_000      # blocks, ~3 h
MIN_WINDOW = 1_000          # the smallest window a timed-out read shrinks to
LOG_SPAN = 10_000_000       # the public RPC's widest eth_getLogs with one address
TOP = 10
MAX_HOPS = 5
DEAD = "0x000000000000000000000000000000000000dead"
SEL_NAME, SEL_SYMBOL = chain.SEL_NAME, chain.SEL_SYMBOL


class ScanError(Exception):
    """Why a token cannot be scanned; `key` names the text shown (i18n)."""

    def __init__(self, message: str, key: str):
        super().__init__(message)
        self.key = key


@dataclass
class Scan:
    token: str
    symbol: str
    name: str
    supply: int                       # base units
    decimals: int
    at: float                         # when it was made
    first_block: int | None = None    # the first mint
    launch_block: int | None = None   # the market opened
    made_ts: float | None = None      # when the first mint was, estimated from blocks
    complete: bool = True             # every Transfer was read
    transfers: int = 0
    holders: int | None = None
    top_share: float | None = None    # 0..1 of the supply in the TOP biggest wallets
    top: list = field(default_factory=list)   # (wallet, share) of the biggest few
    snipers: int | None = None
    sniper_share: float | None = None
    dev: str | None = None
    dev_share: float | None = None    # what the dev holds now
    dev_sold: float | None = None     # 0..1 of what the dev got, sold into the market
    dev_moved: float | None = None    # 0..1 of what the dev got, sent to other wallets
    sellers: int | None = None
    copies: list = field(default_factory=list)  # (symbol, address) of older tokens


def norm_symbol(symbol: str) -> str:
    return (symbol or "").strip().lstrip("$").casefold()


def _move(lg: dict) -> tuple[int, str, int, str, str, int] | None:
    """(block, tx, log index, from, to, value) of a Transfer log, or None."""
    t = [x.lower() for x in lg.get("topics") or []]
    if len(t) != 3 or t[0] != chain.TOPIC_TRANSFER:
        return None
    frm, to = abi.topic_address(t[1]), abi.topic_address(t[2])
    value = abi.decode_uint(lg.get("data") or "0x")
    try:
        pos = int(lg["blockNumber"], 16), lg["transactionHash"].lower(), int(lg["logIndex"], 16)
    except (KeyError, TypeError, ValueError):
        return None
    if frm is None or to is None or value is None:
        return None
    return (*pos, frm, to, value)


class Ledger:
    """Balances and the facts a scan needs, from a token's Transfers fed in order. It
    keeps one number per wallet and the moves of the first blocks only, so a long
    history passes through in windows without piling up."""

    def __init__(self, markets: set[str], dev: str | None = None, sniper_blocks: int = SNIPER_BLOCKS):
        self.markets = markets        # where buys come from and sells go
        self.dev = dev
        self.sniper_blocks = sniper_blocks
        self.balances: dict[str, int] = {}
        self.launch: int | None = None
        self.launch_tx: str | None = None
        self.early: list[tuple] = []  # moves in the sniper window
        self.sellers: set[str] = set()
        self.dev_got = self.dev_sold = self.dev_moved = self.dev_liquidity = 0
        self.transfers = 0

    def add(self, logs: list[dict]) -> None:
        moves = sorted(m for m in map(_move, logs) if m)
        for block, tx, index, frm, to, value in moves:
            self.transfers += 1
            self.balances[frm] = self.balances.get(frm, 0) - value
            self.balances[to] = self.balances.get(to, 0) + value
            into = to in self.markets
            if self.launch is None and into:
                self.launch, self.launch_tx = block, tx  # the curve funded, the first liquidity
            if self.launch is not None and block <= self.launch + self.sniper_blocks:
                self.early.append((block, tx, index, frm, to, value))
            if into and frm not in self.markets and tx != self.launch_tx:
                self.sellers.add(frm)
            if self.dev is None:
                continue
            if to == self.dev:
                self.dev_got += value
            if frm == self.dev:
                if not into:
                    self.dev_moved += value
                elif tx == self.launch_tx:
                    self.dev_liquidity += value  # the pool the dev opened, not a sale
                else:
                    self.dev_sold += value

    def sniper_wallets(self, excluded: set[str]) -> set[str]:
        """Who got the token from the market in the opening blocks: where it stopped, through
        the bots' contracts that pass it on in the same transaction (as pools.payouts)."""
        by_tx: dict[str, list[tuple]] = {}
        for mv in self.early:
            by_tx.setdefault(mv[1], []).append(mv)
        out = set()
        for moves in by_tx.values():
            moves.sort(key=lambda m: m[2])
            for _, _, index, frm, to, value in moves:
                if frm not in self.markets or to in self.markets:
                    continue
                at, who, v = index, to, value
                for _ in range(MAX_HOPS):
                    nxt = next(((m[2], m[4], m[5]) for m in moves if m[2] > at and m[3] == who
                                and m[4] not in self.markets and m[5] * 100 >= v * 99), None)
                    if nxt is None:
                        break
                    at, who, v = nxt
                if who not in excluded and who != self.dev:
                    out.add(who)
        return out

    def holders(self, excluded: set[str]) -> dict[str, int]:
        return {a: b for a, b in self.balances.items() if b > 0 and a not in excluded}


def excluded_for(token: str, markets: set[str]) -> set[str]:
    """Addresses that are never holders, snipers or sellers."""
    return markets | amm.ROUTERS | {abi.ZERO_ADDRESS, DEAD, token.lower(), chain.POOL_MANAGER,
                                    chain.PONS_FACTORY, flap.PORTAL}


def summarize(s: Scan, ledger: Ledger, excluded: set[str], balances: dict[str, int]) -> Scan:
    """Fill a scan from a ledger; `balances` are those read back from the chain (they win
    over the sums of the logs). Without the whole history only what does not depend on it
    is filled: the snipers (the opening blocks are read first) and the dev's balance."""
    supply = s.supply
    s.transfers = ledger.transfers
    s.launch_block = ledger.launch
    snipers = ledger.sniper_wallets(excluded) if ledger.launch is not None else None
    if snipers is not None:
        s.snipers = len(snipers)
        if all(w in balances for w in snipers) or s.complete:
            held = sum(balances.get(w, max(ledger.balances.get(w, 0), 0)) for w in snipers)
            s.sniper_share = held / supply
    if s.dev and s.dev in balances:
        s.dev_share = balances[s.dev] / supply
    if not s.complete:
        return s
    if s.dev:
        if s.dev_share is None:
            s.dev_share = max(ledger.balances.get(s.dev, 0), 0) / supply
        base = ledger.dev_got - ledger.dev_liquidity
        if base > 0:
            s.dev_sold = min(ledger.dev_sold / base, 1.0)
            s.dev_moved = min(ledger.dev_moved / base, 1.0)
    held = ledger.holders(excluded)
    held.update({a: b for a, b in balances.items() if a not in excluded})
    held = {a: b for a, b in held.items() if b > 0}
    s.holders = len(held)
    top = sorted(held.items(), key=lambda kv: -kv[1])[:TOP]
    s.top_share = sum(b for _, b in top) / supply
    s.top = [(a, b / supply) for a, b in top[:3]]
    s.sellers = len(ledger.sellers - excluded - {s.dev})
    return s


def find_copies(symbol: str, address: str, made_ts: float | None, known: list[tuple]) -> list[tuple[str, str]]:
    """Older tokens with the same symbol: (symbol, address). `known` holds (symbol, address,
    added_at), added_at None for the quotes (stock tokens, WETH, USDG), which are always the
    original. A token a chat tracked counts as older when it was tracked before this one
    was even made; when that cannot be told, it is not called a copy."""
    want = norm_symbol(symbol)
    out = []
    for sym, addr, added_at in known:
        if not want or norm_symbol(sym) != want or addr.lower() == address.lower():
            continue
        if added_at is None or (made_ts is not None and added_at < made_ts):
            out.append((sym, addr.lower()))
    return out


def known_tokens(db, symbol: str) -> list[tuple]:
    """The tokens the bot knows with this symbol: quotes and every token chats tracked."""
    want = norm_symbol(symbol)
    out = [(sym, addr, None) for addr, (sym, _, _) in quotes.QUOTES.items() if norm_symbol(sym) == want]
    if db is not None:
        out += db.tokens_with_symbol(symbol)
    return out


# --- requests ---------------------------------------------------------------------

async def _calls(rpc, calls: list[tuple[str, str]]) -> list[str | None]:
    """eth_call each (to, data) at the latest block, in one batch where the client can;
    a call that failed comes back None."""
    batch = getattr(rpc, "batch", None)
    if batch is not None:
        res = await batch([("eth_call", [{"to": to, "data": data}, "latest"]) for to, data in calls])
        return [r if isinstance(r, str) else None for r in res]
    out = []
    for to, data in calls:
        try:
            out.append(await rpc.eth_call(to, data))
        except Exception:
            out.append(None)
    return out


async def _logs(rpc, queries: list[tuple]) -> list[list[dict]]:
    many = getattr(rpc, "get_logs_many", None)
    if many is not None:
        return await many(queries)
    return [await rpc.get_logs(*q) for q in queries]


def _string(ret: str | None, limit: int) -> str:
    return (abi.decode_string(ret or "0x") or "?").replace("\n", " ").strip()[:limit] or "?"


async def scan(rpc, address: str, head: int, *, token=None, now: float | None = None) -> Scan:
    """Scan a token (see the module's notes). `token` is its record when a chat tracks it.
    Raises ScanError for an address that is not a token or has no mint."""
    address = address.lower()
    now = time.time() if now is None else now
    ret = await _calls(rpc, [(address, chain.SEL_DECIMALS), (address, chain.SEL_TOTAL_SUPPLY),
                             (address, SEL_SYMBOL), (address, SEL_NAME),
                             (chain.PONS_FACTORY, abi.call_data(chain.SEL_GET_LAUNCHED_TOKEN, address)),
                             (flap.PORTAL, abi.call_data(flap.SEL_GET_TOKEN_V7, address))])
    decimals, supply = abi.decode_uint(ret[0] or "0x"), abi.decode_uint(ret[1] or "0x")
    if decimals is None or supply is None or decimals > 36 or supply == 0:
        raise ScanError(f"{address}: no decimals or supply", "scan_not_token")
    s = Scan(address, _string(ret[2], 16), _string(ret[3], 40), supply, decimals, now)
    pons = chain.decode_launched_token(ret[4] or "0x")
    pons = pons if pons and pons.exists and pons.token == address else None
    on_flap = flap.decode_state(ret[5] or "0x") is not None

    span = getattr(rpc, "MAX_LOG_SPAN", LOG_SPAN)
    mint_topics = [chain.TOPIC_TRANSFER, abi.address_topic(abi.ZERO_ADDRESS)]
    parts = await _logs(rpc, [(frm, min(head, frm + span - 1), address, mint_topics)
                              for frm in range(0, head + 1, span)])
    mints = sorted((m for part in parts for m in map(_move, part) if m))
    if not mints:
        raise ScanError(f"{address}: no mint found", "scan_no_history")
    s.first_block, first_tx = mints[0][0], mints[0][1]

    markets = {chain.POOL_MANAGER} | amm.ROUTERS
    if pons:
        markets.add(pons.curve)
    if on_flap:
        markets.add(flap.PORTAL)
    markets |= {c.id for c in await amm.candidates(rpc, address, head, priced_only=False, since=s.first_block)
                if c.venue != "v4"}
    excluded = excluded_for(address, markets)
    if pons:
        dev = pons.deployer
    elif token is not None and getattr(token, "deployer", ""):
        dev = token.deployer
    elif hasattr(rpc, "tx_sender"):
        try:
            dev = await rpc.tx_sender(first_tx)
        except Exception:
            dev = None
    else:
        dev = None
    s.dev = dev if dev and dev not in excluded else None

    ledger = Ledger(markets, s.dev)
    frm, window, windows = s.first_block, FIRST_WINDOW, 0
    while frm <= head:
        if windows >= MAX_WINDOWS or ledger.transfers >= MAX_TRANSFERS:
            s.complete = False
            break
        to = min(head, frm + window - 1)
        windows += 1
        try:
            logs = await rpc.get_logs(frm, to, address=address, topics=[chain.TOPIC_TRANSFER])
        except Exception:
            # a window too busy to come back in time (the public RPC times out on big
            # answers): a quarter of it, until it is as small as it gets
            if window <= MIN_WINDOW:
                raise
            window = max(window // 4, MIN_WINDOW)
            continue
        ledger.add(logs)
        n = len(logs)
        del logs
        frm = to + 1
        if n < 1_000:
            window = min(window * 4, span)
        elif n < 4_000:
            window = min(window * 2, span)
        elif n > 8_000:
            window = max(window // 2, 1_000)

    # read back the balances that are shown: the biggest holders, the dev, the snipers
    wallets = []
    if s.complete:
        wallets += [a for a, _ in sorted(ledger.holders(excluded).items(), key=lambda kv: -kv[1])[:TOP + 5]]
    if s.dev:
        wallets.append(s.dev)
    if ledger.launch is not None:
        wallets += sorted(ledger.sniper_wallets(excluded))[:40]
    wallets = list(dict.fromkeys(wallets))
    balances = {}
    if wallets:
        got = await _calls(rpc, [(address, abi.call_data(chain.SEL_BALANCE_OF, w)) for w in wallets])
        balances = {w: v for w, r in zip(wallets, got) if (v := abi.decode_uint(r or "0x")) is not None}
    summarize(s, ledger, excluded, balances)
    s.made_ts = now - (head - s.first_block) / BLOCKS_PER_SECOND
    return s
