"""Reading a Pons v2 token's record from the chain when a chat starts tracking it."""

import logging
import time

from . import abi, chain
from .db import Token

log = logging.getLogger(__name__)


class TokenError(Exception):
    """Shown to the admin as is, so the message is user-facing English."""


def _clean(text: str | None, limit: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text[:limit] or "?"


async def load_token(rpc, address: str) -> Token:
    address = address.lower()
    ret = await rpc.eth_call(chain.PONS_FACTORY,
                             abi.call_data(chain.SEL_GET_LAUNCHED_TOKEN, address))
    rec = chain.decode_launched_token(ret or "0x")
    if rec is None or not rec.exists or rec.token != address:
        raise TokenError("This address is not a Pons v2 token. Only Pons v2 launches are "
                         "supported for now.")

    decimals = abi.decode_uint(await rpc.eth_call(address, chain.SEL_DECIMALS) or "0x")
    supply = abi.decode_uint(await rpc.eth_call(address, chain.SEL_TOTAL_SUPPLY) or "0x")
    if decimals is None or supply is None or decimals > 36:
        raise TokenError("Could not read the token's decimals or supply. Try again in a minute.")
    name = _clean(abi.decode_string(await rpc.eth_call(address, chain.SEL_NAME) or "0x"), 40)
    symbol = _clean(abi.decode_string(await rpc.eth_call(address, chain.SEL_SYMBOL) or "0x"), 16)

    if rec.pair in chain.KNOWN_QUOTES:
        q_symbol, q_decimals, q_kind = chain.KNOWN_QUOTES[rec.pair]
    else:
        q_symbol = _clean(abi.decode_string(await rpc.eth_call(rec.pair, chain.SEL_SYMBOL) or "0x"), 12)
        q_decimals = abi.decode_uint(await rpc.eth_call(rec.pair, chain.SEL_DECIMALS) or "0x")
        if q_decimals is None or q_decimals > 36:
            raise TokenError("Could not read the token's quote currency. Try again in a minute.")
        q_kind = "other"

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
