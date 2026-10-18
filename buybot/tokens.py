"""Reading a token's record from the chain when a chat starts tracking it.

A Pons v2 launch is read from the Pons factory (its bonding curve, then its Uniswap v4
pool); a Flap.sh launch still on its curve from Flap's Portal. Any other token (and a
Flap token after graduation) is tracked through its Uniswap-style pools
(pools.find_pools): the deepest pool's quote becomes the token's main quote, in which
its market cap and chart are kept.
"""

import logging
import time

from . import abi, chain, flap, pools, quotes
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
