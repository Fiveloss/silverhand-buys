"""Robinhood Chain facts for Pons v2 and Uniswap v4, and decoders for their logs.

Where the facts come from (all captured from mainnet, see fixtures/ and tests.py):
- Pons v2 factory, TokenLaunched, CurveBuy/CurveSell layouts, getLaunchedToken's
  15-word return, the factory's graduation event: decoded from real
  transactions (captures published by the realorrug project, Apache-2.0).
- After graduation a token trades in a Uniswap v4 pool keyed
  (currency0, currency1, fee, tickSpacing, hooks) with the Pons meme hook;
  its PoolId is keccak256(abi.encode(PoolKey)) and matches DexScreener's id.

`tools/preflight.py` re-checks every one of these against the live chain
before the bot is started on a server.
"""

from dataclasses import dataclass
from math import isqrt

from . import abi
from .keccak import keccak256

CHAIN_ID = 4663

PONS_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
PONS_HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
NATIVE = abi.ZERO_ADDRESS

# The quote assets the bot can value in dollars are listed in quotes.py.

# --- event topics (each equals keccak256 of the signature in its comment) ---
# TokenLaunched(address indexed token, address indexed curve, address indexed deployer,
#               address pairToken, uint256 launchConfigId, uint256 graduationThreshold)
TOPIC_TOKEN_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
# CurveBuy(address indexed buyer, address indexed recipient,
#          uint256 quoteIn, uint256 tokensOut, uint256 fee, uint256 tax)
TOPIC_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
# CurveSell(address indexed seller, address indexed recipient,
#           uint256 tokensIn, uint256 quoteOut, uint256 fee, uint256 tax)
TOPIC_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
# The factory's graduation event: (address indexed token, uint256 quoteRaised,
# uint256 tokensToFactory). Its signature string is not published; the hash
# and layout are taken from a captured graduation transaction.
TOPIC_GRADUATED = "0xcdb72f157fd3666758a6ce201387ffb52038c7562e4fff352828da1096c4b6b4"
# Uniswap v4 PoolManager: Swap(bytes32 indexed id, address indexed sender, int128 amount0,
#                              int128 amount1, uint160 sqrtPriceX96, uint128 liquidity,
#                              int24 tick, uint24 fee)
TOPIC_V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
# ERC-20 Transfer(address indexed from, address indexed to, uint256 value): a pool buy moves
# the token from the pool to the buyer, which names the wallet the Swap does not
# (pools.payouts).
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


# --- selectors ---
SEL_GET_LAUNCHED_TOKEN = "0x3cf28b5a"  # getLaunchedToken(address), on the factory
SEL_REAL_QUOTE_RESERVE = "0x4f1f58fd"  # realQuoteReserve(), on a curve
SEL_GET_RESERVES = "0x0902f1ac"        # getReserves() -> (quote, token), on a curve
SEL_PHANTOM_QUOTE = "0xc57eadfc"       # phantomQuote(), on a curve
SEL_NAME = "0x06fdde03"
SEL_SYMBOL = "0x95d89b41"
SEL_DECIMALS = "0x313ce567"
SEL_TOTAL_SUPPLY = "0x18160ddd"
SEL_BALANCE_OF = "0x70a08231"          # balanceOf(address)


@dataclass(frozen=True)
class Launch:
    token: str
    curve: str
    deployer: str
    pair: str  # NATIVE for ETH
    config: int
    threshold: int


@dataclass(frozen=True)
class LaunchedToken:
    token: str
    curve: str
    deployer: str
    fee_recipient: str
    pair: str  # NATIVE for ETH
    threshold: int
    pool_fee: int
    tick_spacing: int
    creator_tax_bps: int
    phase: int  # 0 on the curve, 2 graduated (observed values)
    exists: bool

    @property
    def graduated(self) -> bool:
        return self.phase == 2


@dataclass(frozen=True)
class CurveBuy:
    curve: str
    caller: str     # the trader or a router acting for one
    recipient: str  # who received the tokens: the buyer we show
    quote_in: int   # gross: what the buyer paid, fee and tax included
    tokens_out: int
    fee: int
    tax: int
    block: int
    tx: str
    log_index: int


@dataclass(frozen=True)
class Graduated:
    token: str
    quote_raised: int
    tokens_to_factory: int
    block: int
    tx: str
    log_index: int


@dataclass(frozen=True)
class CurveSell:
    curve: str
    caller: str     # the trader or a router acting for one
    recipient: str  # who received the quote
    tokens_in: int
    quote_out: int  # net: what the seller received; fee and tax come off the curve on top
    fee: int
    tax: int
    block: int
    tx: str
    log_index: int


@dataclass(frozen=True)
class V4Swap:
    pool_id: str
    sender: str
    amount0: int  # swapper's delta: negative = paid into the pool
    amount1: int
    sqrt_price_x96: int
    block: int
    tx: str
    log_index: int


def _log_pos(log: dict) -> tuple[int, str, int] | None:
    try:
        return int(log["blockNumber"], 16), log["transactionHash"].lower(), int(log["logIndex"], 16)
    except (KeyError, TypeError, ValueError):
        return None


def _topics(log: dict) -> list[str]:
    return [t.lower() for t in log.get("topics") or []]


def decode_token_launched(log: dict) -> Launch | None:
    t = _topics(log)
    if log.get("address", "").lower() != PONS_FACTORY or len(t) != 4 or t[0] != TOPIC_TOKEN_LAUNCHED:
        return None
    data = abi.to_bytes(log.get("data", "0x"))
    token, curve, deployer = (abi.topic_address(x) for x in t[1:4])
    pair = abi.word_address(data, 0)
    config, threshold = abi.word_uint(data, 1), abi.word_uint(data, 2)
    if None in (token, curve, deployer, pair, config, threshold):
        return None
    return Launch(token, curve, deployer, pair, config, threshold)


def decode_launched_token(ret_hex: str) -> LaunchedToken | None:
    """The factory's getLaunchedToken(address) record: fifteen static words."""
    try:
        data = abi.to_bytes(ret_hex)
    except ValueError:
        return None
    if len(data) < 15 * 32:
        return None
    addrs = [abi.word_address(data, i) for i in range(5)]
    nums = [abi.word_uint(data, i) for i in range(5, 15)]
    if None in addrs or None in nums:
        return None
    exists_word = nums[9]
    if exists_word not in (0, 1):
        return None
    tick = nums[2]
    if tick >= 1 << 255:
        tick -= 1 << 256
    return LaunchedToken(
        token=addrs[0], curve=addrs[1], deployer=addrs[2], fee_recipient=addrs[3], pair=addrs[4],
        threshold=nums[0], pool_fee=nums[1], tick_spacing=tick, creator_tax_bps=nums[3],
        phase=nums[5], exists=exists_word == 1,
    )


def decode_curve_buy(log: dict) -> CurveBuy | None:
    t = _topics(log)
    pos = _log_pos(log)
    if len(t) != 3 or t[0] != TOPIC_CURVE_BUY or pos is None:
        return None
    data = abi.to_bytes(log.get("data", "0x"))
    caller, recipient = abi.topic_address(t[1]), abi.topic_address(t[2])
    vals = [abi.word_uint(data, i) for i in range(4)]
    if caller is None or recipient is None or None in vals:
        return None
    return CurveBuy(log["address"].lower(), caller, recipient, *vals, *pos)


def decode_curve_sell(log: dict) -> CurveSell | None:
    t = _topics(log)
    pos = _log_pos(log)
    if len(t) != 3 or t[0] != TOPIC_CURVE_SELL or pos is None:
        return None
    data = abi.to_bytes(log.get("data", "0x"))
    caller, recipient = abi.topic_address(t[1]), abi.topic_address(t[2])
    vals = [abi.word_uint(data, i) for i in range(4)]
    if caller is None or recipient is None or None in vals:
        return None
    return CurveSell(log["address"].lower(), caller, recipient, *vals, *pos)


def decode_graduated(log: dict) -> Graduated | None:
    t = _topics(log)
    pos = _log_pos(log)
    if log.get("address", "").lower() != PONS_FACTORY or len(t) != 2 or t[0] != TOPIC_GRADUATED:
        return None
    if pos is None:
        return None
    data = abi.to_bytes(log.get("data", "0x"))
    token = abi.topic_address(t[1])
    raised, moved = abi.word_uint(data, 0), abi.word_uint(data, 1)
    if token is None or raised is None or moved is None:
        return None
    return Graduated(token, raised, moved, *pos)


def decode_v4_swap(log: dict) -> V4Swap | None:
    t = _topics(log)
    pos = _log_pos(log)
    if log.get("address", "").lower() != POOL_MANAGER or len(t) != 3 or t[0] != TOPIC_V4_SWAP:
        return None
    if pos is None:
        return None
    data = abi.to_bytes(log.get("data", "0x"))
    a0, a1 = abi.word_int(data, 0), abi.word_int(data, 1)
    sqrt_p = abi.word_uint(data, 2)
    sender = abi.topic_address(t[2])
    if None in (a0, a1, sqrt_p, sender):
        return None
    return V4Swap(t[1], sender, a0, a1, sqrt_p, *pos)


def currencies(token: str, pair: str) -> tuple[str, str]:
    """Uniswap v4 orders a pool's two currencies by address; native ETH is 0x0."""
    a, b = token.lower(), pair.lower()
    return (a, b) if int(a, 16) < int(b, 16) else (b, a)


def pool_id(token: str, pair: str = NATIVE, fee: int = 0, tick_spacing: int = 200,
            hooks: str = PONS_HOOK) -> str:
    c0, c1 = currencies(token, pair)
    tick_word = tick_spacing % (1 << 256)  # int24 sign-extended
    encoded = (abi.encode_address(c0) + abi.encode_address(c1) + abi.encode_uint(fee)
               + abi.encode_uint(tick_word) + abi.encode_address(hooks))
    return "0x" + keccak256(bytes.fromhex(encoded)).hex()


def v4_buy_amounts(swap: V4Swap, token: str, pair: str) -> tuple[int, int] | None:
    """(quote paid, tokens received) if the swap bought `token`, else None.

    v4 reports deltas from the swapper's side: negative is paid into the pool,
    positive is taken out. A buy pays the quote currency and takes the token.
    """
    c0, _ = currencies(token, pair)
    if c0 == token.lower():
        token_delta, quote_delta = swap.amount0, swap.amount1
    else:
        quote_delta, token_delta = swap.amount0, swap.amount1
    if quote_delta < 0 < token_delta:
        return -quote_delta, token_delta
    return None


def v4_sell_amounts(swap: V4Swap, token: str, pair: str) -> tuple[int, int] | None:
    """(quote received, tokens paid) if the swap sold `token`, else None."""
    c0, _ = currencies(token, pair)
    if c0 == token.lower():
        token_delta, quote_delta = swap.amount0, swap.amount1
    else:
        quote_delta, token_delta = swap.amount0, swap.amount1
    if token_delta < 0 < quote_delta:
        return quote_delta, -token_delta
    return None


def v4_token_price(sqrt_price_x96: int, token: str, pair: str,
                   token_decimals: int, quote_decimals: int) -> float | None:
    """Price of one whole token in whole quote units, from a pool's sqrtPriceX96."""
    if sqrt_price_x96 <= 0:
        return None
    raw = (sqrt_price_x96 / 2 ** 96) ** 2  # currency1 per currency0, base units
    c0, _ = currencies(token, pair)
    quote_per_token_raw = raw if c0 == token.lower() else 1 / raw
    return quote_per_token_raw * 10 ** (token_decimals - quote_decimals)


def trade_price(quote_amount: int, token_amount: int,
                token_decimals: int, quote_decimals: int) -> float | None:
    """Average price of one trade, in whole quote units per whole token."""
    if token_amount <= 0:
        return None
    return quote_amount / token_amount * 10 ** (token_decimals - quote_decimals)


def curve_after_buy(buy: CurveBuy, k: int | None, phantom: int | None, threshold: int,
                    token_decimals: int, quote_decimals: int) -> tuple[float | None, float | None]:
    """(spot price after the buy, graduation progress 0..1) from the event alone.

    A Pons v2 curve is constant product over (virtual + real quote, tokens):
    fee and tax are taken off the quote first, and the rest moves the curve.
    Knowing k = quote * tokens (read once when a token is added), the buy's
    net quote and tokens out fix the reserves before and after it, so no RPC
    call is needed per trade. If the numbers do not fit that model (a curve
    type we have not seen), both values come back None rather than wrong.
    """
    net = buy.quote_in - buy.fee - buy.tax
    if not k or net <= 0 or buy.tokens_out <= 0:
        return None, None
    q_before = (isqrt(net * net + 4 * k * net // buy.tokens_out) - net) // 2
    q_after = q_before + net
    t_after = k // q_after if q_after > 0 else 0
    if q_before <= 0 or t_after <= 0:
        return None, None
    if phantom is not None and (q_before < phantom * 999 // 1000
                                or (threshold and q_after - phantom > threshold * 102 // 100)):
        return None, None
    price = q_after / t_after * 10 ** (token_decimals - quote_decimals)
    progress = None
    if phantom is not None and threshold > 0:
        progress = min(max((q_after - phantom) / threshold, 0.0), 1.0)
    return price, progress


def curve_after_sell(sell: CurveSell, k: int | None, phantom: int | None, threshold: int,
                     token_decimals: int, quote_decimals: int) -> tuple[float | None, float | None]:
    """(spot price after the sell, graduation progress 0..1) from the event alone.

    A sell adds tokensIn to the curve and takes quoteOut + fee + tax off its quote
    reserve (checked against getReserves around mainnet sells). With
    q_before * t_before = k and (q_before - gross) * (t_before + tokensIn) = k the
    reserves before and after follow, as in curve_after_buy.
    """
    gross = sell.quote_out + sell.fee + sell.tax
    if not k or gross <= 0 or sell.tokens_in <= 0:
        return None, None
    q_before = (gross + isqrt(gross * gross + 4 * gross * k // sell.tokens_in)) // 2
    q_after = q_before - gross
    if q_after <= 0:
        return None, None
    t_after = k // q_after
    if t_after <= 0 or (phantom is not None and q_after < phantom * 999 // 1000):
        return None, None
    price = q_after / t_after * 10 ** (token_decimals - quote_decimals)
    progress = None
    if phantom is not None and threshold > 0:
        progress = min(max((q_after - phantom) / threshold, 0.0), 1.0)
    return price, progress
