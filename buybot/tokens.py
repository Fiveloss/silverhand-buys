"""Reading a token's record from the chain when a chat starts tracking it.

A Pons v2 launch is read from the Pons factory (its bonding curve, then its Uniswap v4
pool); a Flap.sh launch still on its curve from Flap's Portal; a vibe/vibe launch (where
the chain has vibe/vibe, see chain.VIBE) from its curve, confirmed by the factory's own
LaunchCreated log (its curve, then its Uniswap v4 pool). Any other token (and a
Flap token after graduation) is tracked through its Uniswap-style pools
(pools.find_pools): the deepest pool's quote becomes the token's main quote, in which
its market cap and chart are kept.
"""

import logging
import time

from . import abi, chain, flap, pools, quotes, vibe
from .db import Token

log = logging.getLogger(__name__)


class TokenError(Exception):
    """Why a token cannot be tracked. `key` names the text shown to the admin (i18n),
    with `kw` filled in; the message itself is for the log."""

    def __init__(self, message: str, key: str | None = None, **kw):
        super().__init__(message)
        self.key = key
        self.kw = kw


class NoPools(TokenError):
    """A token with no pool the bot can read yet."""


def _clean(text: str | None, limit: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text[:limit] or "?"


async def read_erc20_meta(rpc, address: str) -> tuple[str, int] | None:
    """(symbol, decimals) of a token, or None if it does not answer like an ERC-20."""
    try:
        decimals = abi.decode_uint(await rpc.eth_call(address, chain.SEL_DECIMALS) or "0x")
        symbol = abi.decode_string(await rpc.eth_call(address, chain.SEL_SYMBOL) or "0x")
    except Exception as e:
        log.info("erc20 meta of %s unreadable: %s", address, e)
        return None
    if decimals is None or decimals > 36:
        return None
    return _clean(symbol, 12), decimals


async def _basics(rpc, address: str) -> tuple[str, str, int, int]:
    """(name, symbol, decimals, supply) of the token being tracked."""
    try:
        decimals = abi.decode_uint(await rpc.eth_call(address, chain.SEL_DECIMALS) or "0x")
        supply = abi.decode_uint(await rpc.eth_call(address, chain.SEL_TOTAL_SUPPLY) or "0x")
    except Exception:
        decimals = supply = None
    if decimals is None or supply is None or decimals > 36 or supply == 0:
        raise TokenError("not a token (no decimals or supply)", "track_not_token")
    name = _clean(abi.decode_string(await rpc.eth_call(address, chain.SEL_NAME) or "0x"), 40)
    symbol = _clean(abi.decode_string(await rpc.eth_call(address, chain.SEL_SYMBOL) or "0x"), 16)
    return name, symbol, decimals, supply


async def load_token(rpc, address: str, rates=None) -> tuple[Token, list[pools.Pool]]:
    """The token's record and, for a token outside Pons, the pools to watch."""
    address = address.lower()
    ret = await rpc.eth_call(chain.PONS_FACTORY,
                             abi.call_data(chain.SEL_GET_LAUNCHED_TOKEN, address))
    rec = chain.decode_launched_token(ret or "0x")
    if rec is not None and rec.exists and rec.token == address:
        return await _load_pons(rpc, address, rec), []
    st = await flap.state(rpc, address)
    if st is not None and st.status == flap.STATUS_CURVE:
        return await _load_flap(rpc, address, st), []
    factories = chain.vibe_contracts(await rpc.chain_id() if hasattr(rpc, "chain_id") else None)
    if factories:
        found = await find_any_vibe_launch(rpc, address, factories)
        if found is not None:
            return await _load_vibe(rpc, address, *found, factories), []
    if rates is None:
        raise TokenError("not a Pons v2 token", "track_not_token")
    return await load_dex_token(rpc, address, rates)


async def _quote(rpc, address: str) -> tuple[str, int, str]:
    """(symbol, decimals, kind) of a pair's quote: from quotes.QUOTES, else read ("other")."""
    known = quotes.quote_info(address)
    if known:
        return known
    meta = await read_erc20_meta(rpc, address)
    if meta is None:
        raise TokenError("quote currency unreadable", "track_quote_unreadable")
    return meta[0], meta[1], "other"


async def _load_flap(rpc, address: str, st: flap.FlapState) -> Token:
    """A token on its Flap curve, in the quote Flap names for it (ETH or a token)."""
    name, symbol, decimals, supply = await _basics(rpc, address)
    q_symbol, q_decimals, q_kind = await _quote(rpc, st.quote)
    head = await rpc.block_number()
    return Token(
        address=address, curve="", deployer=await creator(rpc, address, head) or "", pair=st.quote,
        quote_symbol=q_symbol, quote_decimals=q_decimals, quote_kind=q_kind, name=name, symbol=symbol,
        decimals=decimals, total_supply=supply, threshold=0, pool_id="", curve_k=None, phantom=None,
        graduated=False, added_at=int(time.time()), kind="flap",
    )


async def _load_pons(rpc, address: str, rec: chain.LaunchedToken) -> Token:
    name, symbol, decimals, supply = await _basics(rpc, address)
    q_symbol, q_decimals, q_kind = await _quote(rpc, rec.pair)

    curve_k = phantom = None
    if not rec.graduated:
        curve_k, phantom = await read_curve_constants(rpc, rec.curve)

    return Token(
        address=address, curve=rec.curve, deployer=rec.deployer, pair=rec.pair,
        quote_symbol=q_symbol, quote_decimals=q_decimals, quote_kind=q_kind,
        name=name, symbol=symbol, decimals=decimals, total_supply=supply,
        threshold=rec.threshold,
        pool_id=chain.pool_id(address, rec.pair, rec.pool_fee, rec.tick_spacing, chain.PONS_HOOK),
        curve_k=curve_k, phantom=phantom, graduated=rec.graduated, added_at=int(time.time()),
    )


SEL_VIBE_CURVE = "0x7165485d"   # curve(), on a vibe/vibe token
SEL_VIBE_CREATED = "0xcf09e0d0"  # the launch's block time, on its curve
VIBE_WINDOW = 1_000_000          # blocks searched on each side of the estimated launch block


async def find_vibe_launch(rpc, token: str, contracts: chain.VibeContracts) -> vibe.Launch | None:
    """The factory's LaunchCreated for this token, or None when it is not a vibe/vibe launch.

    The token names its curve and the curve its launch time, which places the launch block
    closely enough for one log query. Only the factory's own log counts: any contract can
    claim a curve, and any curve can claim the factory. A call that reverts means "not a
    vibe/vibe token"; any other RPC failure is raised (the chain is down, not the token)."""
    try:
        curve = abi.word_address(abi.to_bytes(await rpc.eth_call(token, SEL_VIBE_CURVE) or "0x"), 0)
        if not curve:
            return None
        # a curve of another factory (or of none): not this factory's launch, no log search
        got = abi.word_address(abi.to_bytes(await rpc.eth_call(curve, vibe.SEL_FACTORY) or "0x"), 0)
        if got != contracts.factory:
            return None
        if contracts.gen == "curve":
            return await _find_c_launch(rpc, token, curve, contracts)
        created = abi.decode_uint(await rpc.eth_call(curve, SEL_VIBE_CREATED) or "0x")
    except Exception as e:  # rpc.RpcError carries the node's message
        if "revert" in str(e).lower():
            return None
        raise
    if not curve or not created:
        return None
    head = await rpc.block_number()
    back = min(head, 1_000_000)
    t_head, t_back = await rpc.block_time(head), await rpc.block_time(head - back)
    per_second = back / max(1, t_head - t_back)
    guess = head - int(max(0, t_head - created) * per_second)
    logs = await rpc.get_logs(max(0, guess - VIBE_WINDOW), min(head, guess + VIBE_WINDOW),
                              address=contracts.factory,
                              topics=[vibe.TOPIC_LAUNCH_CREATED, None, None, abi.address_topic(token)])
    for lg in logs:
        launch = vibe.decode_launch(lg, contracts.factory)
        if launch and launch.token == token and launch.curve == curve:
            return launch
    return None


async def find_any_vibe_launch(rpc, token: str, factories) -> tuple[vibe.Launch, chain.VibeContracts] | None:
    """The token's launch by whichever vibe/vibe factory made it, with that factory."""
    for contracts in factories:
        launch = await find_vibe_launch(rpc, token, contracts)
        if launch is not None:
            return launch, contracts
    return None


C_LAUNCH_WINDOW = 10_000_000  # the RPC's span for one address and one value per topic


async def _find_c_launch(rpc, token: str, curve: str, contracts: chain.VibeContracts) -> vibe.Launch | None:
    """The curve factory's LaunchCreated for this token: its curves keep no launch time, but
    a log query with the token in its topic may span 10M blocks, so a few windows back from
    the head reach the factory's start."""
    head = await rpc.block_number()
    to = head
    while to >= contracts.from_block:
        frm = max(contracts.from_block, to - C_LAUNCH_WINDOW + 1)
        logs = await rpc.get_logs(frm, to, address=contracts.factory,
                                  topics=[vibe.TOPIC_C_LAUNCH_CREATED, None, abi.address_topic(token)])
        for lg in logs:
            launch = vibe.decode_launch(lg, contracts.factory)
            if launch and launch.token == token and launch.curve == curve:
                return launch
        to = frm - 1
    return None


async def eth_pool_kind(rpc, quote: str) -> str | None:
    """"ethpool:<quote>:<pool>" when the quote has a deep enough v4 pool against ETH
    (quotes.ETH_POOL_*), else None. Only for a vibe/vibe curve's quote: the factory takes
    a curve's quote from its own list, so a token made up to fake dollars never gets here."""
    pool = pools.eth_pool_id(quote)
    try:
        price = await pools.eth_pool_price(rpc, quote, pool)
    except Exception as e:
        log.info("ETH pool of %s unreadable: %s", quote, e)
        return None
    return f"ethpool:{quote}:{pool}" if price else None


async def _load_vibe(rpc, address: str, launch: vibe.Launch, contracts: chain.VibeContracts,
                     factories=()) -> Token:
    """A vibe/vibe launch: its curve, then the v4 pool the curve graduates into. The quote is
    the curve's (the curve factory's launch event does not name it)."""
    st = await vibe.state(rpc, launch.curve, contracts.factory)
    if st is None or st.token != address:  # the factory named this curve: the RPC failed us
        raise RuntimeError(f"vibe curve {launch.curve} unreadable")
    name, symbol, decimals, supply = await _basics(rpc, address)
    q_symbol, q_decimals, q_kind = await _quote(rpc, st.quote)
    if q_kind == "other":  # often another vibe/vibe token: valued by its own curve or pool
        q_found = await find_any_vibe_launch(rpc, st.quote, factories or (contracts,))
        if q_found is not None:
            q_launch, q_contracts = q_found
            if q_contracts.gen == "v6":  # its launch event names its quote
                q_quote = q_launch.quote
            else:
                q_st = await vibe.state(rpc, q_launch.curve, q_contracts.factory)
                q_quote = q_st.quote if q_st else None
            if q_quote == abi.ZERO_ADDRESS:
                q_pool = vibe.pool_id(q_launch.token, abi.ZERO_ADDRESS, q_contracts.hook)
                q_kind = f"vibe:{q_launch.curve}:{q_pool}"
    if q_kind == "other" and q_decimals == 18:  # a testnet stock token: its pool against ETH
        q_kind = await eth_pool_kind(rpc, st.quote) or q_kind
    pool = vibe.pool_id(address, st.quote, contracts.hook)
    graduated = st.complete or st.pool_id is not None
    if st.pool_id is not None and st.pool_id != pool:
        log.warning("vibe %s: the curve names pool %s, not %s", address, st.pool_id, pool)
        pool = st.pool_id
    return Token(
        address=address, curve=launch.curve, deployer=launch.launcher, pair=st.quote,
        quote_symbol=q_symbol, quote_decimals=q_decimals, quote_kind=q_kind, name=name, symbol=symbol,
        decimals=decimals, total_supply=supply, threshold=st.target, pool_id=pool,
        curve_k=None if graduated else st.k, phantom=None if graduated else st.phantom,
        graduated=graduated, added_at=int(time.time()), kind="vibe",
    )


async def load_dex_token(rpc, address: str, rates) -> tuple[Token, list[pools.Pool]]:
    name, symbol, decimals, supply = await _basics(rpc, address)
    head = await rpc.block_number()
    found = await pools.find_pools(rpc, rates, address, head)
    if not found:
        raise NoPools(f"{symbol}: no pool with liquidity", "track_no_pools", symbol=symbol)
    main = found[0]
    return Token(
        address=address, curve="", deployer=await creator(rpc, address, head) or "",
        pair=main.quote, quote_symbol=main.quote_symbol, quote_decimals=main.quote_decimals,
        quote_kind=main.quote_kind, name=name, symbol=symbol, decimals=decimals,
        total_supply=supply, threshold=0, pool_id="", curve_k=None, phantom=None,
        graduated=True, added_at=int(time.time()), kind="dex",
    ), found


async def creator(rpc, token: str, head: int) -> str | None:
    """Who created the token: the sender of the transaction that first minted it. Its
    buys get the 👨‍💻 DEV mark, as a Pons deployer's do."""
    try:
        logs = await rpc.get_logs(0, head, address=token,
                                  topics=[chain.TOPIC_TRANSFER, abi.address_topic(abi.ZERO_ADDRESS)])
        if logs and hasattr(rpc, "tx_sender"):
            first = min(logs, key=lambda lg: (int(lg["blockNumber"], 16), int(lg["logIndex"], 16)))
            return await rpc.tx_sender(first["transactionHash"])
    except Exception as e:
        log.info("creator of %s unknown: %s", token, e)
    return None


async def read_curve_constants(rpc, curve: str) -> tuple[int | None, int | None]:
    """k = quote * tokens of the curve, and its virtual (phantom) quote reserve.

    Any read that fails leaves that value None: the bot then shows the trade's
    own price and no graduation bar, instead of a made-up one.
    """
    try:
        data = abi.to_bytes(await rpc.eth_call(curve, chain.SEL_GET_RESERVES) or "0x")
    except Exception as e:  # a read error must not block tracking; the post just shows less
        log.warning("getReserves on %s failed: %s", curve, e)
        return None, None
    q, t = abi.word_uint(data, 0), abi.word_uint(data, 1)
    if len(data) != 64 or not q or not t:
        return None, None
    k = q * t
    for sel in (chain.SEL_REAL_QUOTE_RESERVE, chain.SEL_PHANTOM_QUOTE):
        try:
            value = abi.decode_uint(await rpc.eth_call(curve, sel) or "0x")
        except Exception as e:
            log.info("curve read %s on %s failed: %s", sel, curve, e)
            continue
        if value is None or value > q:
            continue
        return k, (q - value if sel == chain.SEL_REAL_QUOTE_RESERVE else value)
    return k, None
