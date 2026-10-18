"""Flap.sh on Robinhood Chain: tokens on its bonding curve, all traded through one Portal.

Facts (docs.flap.sh, checked on mainnet: fixtures/flap_mainnet.json and preflight):
- One Portal proxy runs every curve. Its events have no indexed fields, so the bot
  reads all of the Portal's trades (a few dozen an hour on this chain) and keeps
  those of tracked tokens.
- The quote is per token: native ETH or an ERC-20 (stock tokens seen on mainnet, e.g.
  GME, although Flap's docs still say ETH only); getTokenV7 names it.
- TokenBought(ts, token, buyer, amount, eth, fee, postPrice): `eth` is what the buyer
  paid in the quote, fee included (it equals the value of a direct ETH buy's
  transaction); `postPrice` is the price after the trade in quote per token, 18 decimals.
- TokenSold has the same layout; `eth` is what the seller got.
- FlapTokenProgressChanged(token, newProgress) in the same transaction: how full the
  curve is, 1e18 = 100%.
- LaunchedToDEX(token, pool, amount, eth): graduation. On this chain the pool is a
  Uniswap v2 pair against the quote (WETH for ETH), from Uniswap's own factory.
- getTokenV7(token) -> 17 words; status 1 = on the curve, 4 = on the DEX, and it
  reverts for a token Flap did not launch.
"""

from dataclasses import dataclass

from . import abi

PORTAL = "0x26605f322f7ff986f381bb9a6e3f5dab0beaeb09"
# TokenBought(uint256 ts, address token, address buyer, uint256 amount, uint256 eth,
#             uint256 fee, uint256 postPrice)
TOPIC_BOUGHT = "0xa800a2038683844fac66747f771bfdfae862eb28b16bcfa387afa9fbacce8ff7"
# TokenSold(uint256 ts, address token, address seller, uint256 amount, uint256 eth,
#           uint256 fee, uint256 postPrice)
TOPIC_SOLD = "0x03a4693e592f5e75dc7c136acb39b146d2b4966c0e509c34f362dee02b3b861a"
# FlapTokenProgressChanged(address token, uint256 newProgress)
TOPIC_PROGRESS = "0x4c35e20d1e9bce377c7d9ec1572d934e46d62961f4da8af5beb8002d5906742d"
# LaunchedToDEX(address token, address pool, uint256 amount, uint256 eth)
TOPIC_LAUNCHED = "0x6e4f47630b8745b8cacbd44f42a8a33e7eea7cc08ef22fc7630f4f385784ff7d"
SEL_GET_TOKEN_V7 = "0xf99abb9e"  # getTokenV7(address)
STATUS_CURVE, STATUS_DEX = 1, 4
WAD = 10 ** 18


@dataclass(frozen=True)
class FlapTrade:
    kind: str       # "buy" | "sell"
    token: str
    trader: str
    tokens: int
    eth: int        # the quote paid (buy, fee included) or received (sell), base units
    fee: int
    price: float    # quote per whole token after the trade
    block: int
    tx: str
    log_index: int


@dataclass(frozen=True)
class FlapState:
    status: int
    price: float    # quote per whole token
    progress: float  # 0..1
    pool: str | None
    quote: str      # abi.ZERO_ADDRESS for native ETH


def _pos(lg: dict):
    try:
        return int(lg["blockNumber"], 16), lg["transactionHash"].lower(), int(lg["logIndex"], 16)
    except (KeyError, TypeError, ValueError):
        return None


def _topic0(lg: dict) -> str:
    return (lg.get("topics") or [""])[0].lower()


def decode_trade(lg: dict) -> FlapTrade | None:
    kind = {TOPIC_BOUGHT: "buy", TOPIC_SOLD: "sell"}.get(_topic0(lg))
    pos = _pos(lg)
    if kind is None or pos is None or lg.get("address", "").lower() != PORTAL:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    token, trader = abi.word_address(data, 1), abi.word_address(data, 2)
    amount, eth, fee, price = (abi.word_uint(data, i) for i in (3, 4, 5, 6))
    if None in (token, trader, amount, eth, fee, price):
        return None
    return FlapTrade(kind, token, trader, amount, eth, fee, price / WAD, *pos)


def decode_progress(lg: dict) -> tuple[str, str, float] | None:
    """(token, tx, progress 0..1)."""
    pos = _pos(lg)
    if _topic0(lg) != TOPIC_PROGRESS or pos is None or lg.get("address", "").lower() != PORTAL:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    token, value = abi.word_address(data, 0), abi.word_uint(data, 1)
    if token is None or value is None:
        return None
    return token, pos[1], min(value / WAD, 1.0)


def decode_launched(lg: dict) -> tuple[str, str, int, int, int, str, int] | None:
    """(token, pair, tokens added, eth added, block, tx, log index)."""
    pos = _pos(lg)
    if _topic0(lg) != TOPIC_LAUNCHED or pos is None or lg.get("address", "").lower() != PORTAL:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    token, pool = abi.word_address(data, 0), abi.word_address(data, 1)
    amount, eth = abi.word_uint(data, 2), abi.word_uint(data, 3)
    if None in (token, pool, amount, eth):
        return None
    return token, pool, amount, eth, *pos


def decode_state(ret_hex: str) -> FlapState | None:
    try:
        data = abi.to_bytes(ret_hex)
    except ValueError:
        return None
    if len(data) < 17 * 32:
        return None
    status, price, progress = abi.word_uint(data, 0), abi.word_uint(data, 3), abi.word_uint(data, 14)
    pool, quote = abi.word_address(data, 13), abi.word_address(data, 9)
    if None in (status, price, progress, quote):
        return None
    return FlapState(status, price / WAD, min(progress / WAD, 1.0),
                     pool if pool and pool != abi.ZERO_ADDRESS else None, quote)


async def state(rpc, token: str) -> FlapState | None:
    """The token's state on Flap, or None when Flap did not launch it (the call reverts)."""
    try:
        return decode_state(await rpc.eth_call(PORTAL, abi.call_data(SEL_GET_TOKEN_V7, token)) or "0x")
    except Exception:
        return None
