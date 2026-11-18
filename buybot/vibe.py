"""vibe/vibe V6 launches on Robinhood Chain: a factory, one bonding curve per token, then a
Uniswap v4 pool.

The contracts are not verified and no ABI is published. Event names come from their
signature hashes (checked in tests.py); what each field means was read off the token
Transfers of the same transactions and replayed over a whole curve life on testnet
(fixtures/vibe_v6_testnet.json, docs/VIBE-VIBE.md):

- LaunchCreated (factory): id, launcher, token indexed; data: the curve, then the launch
  config: name, symbol, metadata uri (strings), the quote (0x0 = native ETH, else an
  ERC-20, often another vibe token or a stock token), the charge in bps, the project
  treasury, the graduation target in quote units and the tokens of the creator's first buy.
- InitialPurchase(launcher indexed, tokens gross, tokens to the launcher, quote net, fee):
  the creator's first buy, inside the launch transaction.
- CurveBuy(caller indexed, recipient indexed, tokens gross, tokens to the recipient, tax
  tokens, quote net, fee, bps). The caller is usually vibe/vibe's router; the recipient
  is the buyer. The buyer paid quote net + fee; the curve gave out the gross tokens, of
  which the tax went to the token contract.
- CurveSell(seller indexed, recipient indexed, tokens in, tokens into the curve, tax tokens,
  quote out of the curve, fee). The seller is the wallet; the recipient (often the router,
  which swaps the quote on to ETH) got quote out - fee.
- The charge (bps) is split between a tax taken in tokens and a fee taken in the quote,
  per curve: tax/gross + fee/net = bps / 10000 (200 bps: 1.28% tax + 0.72% fee).
- The curve is constant product over virtual reserves: quote = real + target/3 and tokens =
  left on the curve + curve supply/3 (800M on the curve: 1B minted, 200M kept for the
  pool). At the target the curve price equals the pool's opening price, and
  Graduated(poolId indexed, quote to the pool, tokens to the pool, 0) follows in the same
  transaction. The pool is (token, quote) with fee 0, tickSpacing 60 and the V6 hook; its
  id is the usual keccak256 of that PoolKey (chain.pool_id), with native ETH as currency0
  for ETH curves.
- After graduation the hook taxes pool trades in tokens as well, so a v4 Swap's token
  delta includes the tax.

The curve factory (chain.VibeContracts gen "curve", the deployment vibe/vibe's own config
names; fixtures/vibe_curve_testnet.json, every trade of one curve replayed in tests.py):

- LaunchCreated(id, token, curve indexed; data: creator, its payout address, a per-launch
  word, the launch-time buy's quote and tokens, 1, a bytes32, the metadata uri). No name,
  symbol, quote or target: the curve's views give the target, the target gives the quote
  (chain.VibeContracts.quotes).
- Buy(caller indexed, recipient indexed, quote gross, quote net, tokens out, creator fee,
  protocol fee): gross - net = the two fees (1.25%); no tax. The caller is the buyer, or
  the factory for the creator's buy in the launch transaction.
- Sell(seller indexed, tokens in, quote gross, quote net, creator fee, protocol fee): the
  seller gets quote net.
- The curve is constant product over virtual reserves it reports itself: the quote starts
  at INITIAL_VIRTUAL_ETH_RESERVE (target * 279.9M / 793.1M) and the tokens at 1.073B, of
  which 793.1M are on the curve (206.9M go to the pool). The buy that reaches the target
  emits Completed (0x654e1d49: real quote, time) first; Graduated(poolId indexed, quote to
  the pool, tokens to the pool) comes in a LATER transaction (a keeper's, hours later on
  testnet). Until then the curve is full but there is no pool, so for these curves
  State.complete means graduated(). The pool is (token, quote), fee 0, tickSpacing 60,
  this factory's hook.
"""

from dataclasses import dataclass

from . import abi, chain, pools

# LaunchCreated(uint256 indexed id, address indexed launcher, address indexed token, ...):
# the signature of its config tuple is not known; the hash is from the factory's logs.
TOPIC_LAUNCH_CREATED = "0xa7e8032bfd07a9fbcde50eabe91eb2901faee6dbddd9cced579491d9b07ef5c8"
# InitialPurchase(address indexed launcher, uint256 tokensGross, uint256 tokensOut,
#                 uint256 quoteIn, uint256 fee)
TOPIC_INITIAL_PURCHASE = "0x5b18697b3628e2e1fc787629f624c31f0e98e273ced4f25efbb83d3865368dda"
# CurveBuy(address indexed caller, address indexed recipient, uint256 tokensGross,
#          uint256 tokensOut, uint256 taxTokens, uint256 quoteIn, uint256 fee, uint256 bps)
TOPIC_CURVE_BUY = "0x55867fe9a84ea30ad247ef1bb703eba47fd9af476149002512a2814b1e3d8fb5"
# CurveSell(address indexed seller, address indexed recipient, uint256 tokensIn,
#           uint256 tokensToCurve, uint256 taxTokens, uint256 quoteOut, uint256 fee)
TOPIC_CURVE_SELL = "0x3899b2b9ba4dfe77b54ff5e6f921672b2a7563ade468f2a106b25a4b808d60de"
# Graduated(bytes32 indexed poolId, uint256 quoteToPool, uint256 tokensToPool, uint256)
TOPIC_GRADUATED = "0xab164bba6f9a29b6722f18be5ea821368fedba444c4f8733118f3557d77ab2ae"

# The curve factory's events (names unknown: hashes from its logs, layouts from Transfers).
# LaunchCreated(uint256 indexed id, address indexed token, address indexed curve, ...)
TOPIC_C_LAUNCH_CREATED = "0x56eef5c350583250e71791d6d659f4d181796c9deb08b977252efc3995aa9279"
# (address indexed caller, address indexed recipient, quoteGross, quoteNet, tokensOut,
#  creatorFee, protocolFee)
TOPIC_C_BUY = "0x8a5254432535d4192429d2cc163283a57784eac274295fcda17cc659c1ee414c"
# (address indexed seller, tokensIn, quoteGross, quoteNet, creatorFee, protocolFee)
TOPIC_C_SELL = "0x917d0fe1b6c3328f12a0177d25bf1b7d9e963116addad0bfc06b0cdcb6427603"
# (bytes32 indexed poolId, quoteToPool, tokensToPool)
TOPIC_C_GRADUATED = "0xc1eaf6441a62895ddec3612cee606bd971cf6b1b38dd700f103e7b6b94167d0c"

TRADE_TOPICS = (TOPIC_INITIAL_PURCHASE, TOPIC_CURVE_BUY, TOPIC_CURVE_SELL, TOPIC_C_BUY, TOPIC_C_SELL)
GRADUATED_TOPICS = (TOPIC_GRADUATED, TOPIC_C_GRADUATED)
CURVE_TOPICS = [*TRADE_TOPICS, *GRADUATED_TOPICS]
LAUNCH_TOPICS = {"v6": TOPIC_LAUNCH_CREATED, "curve": TOPIC_C_LAUNCH_CREATED}

# Curve view calls. Where the name is unknown the constant says what the call returns
# (matched against trades: see the fixture's `snapshot`).
SEL_TOKEN = "0xfc0c546a"        # token()
SEL_FACTORY = "0xc45a0155"      # factory()
SEL_COMPLETE = "0x522e1177"     # complete(): 1 once the target is reached
SEL_POOL_ID = "0x3e0dc34e"      # poolId(): 0 until graduation
SEL_QUOTE = "0x7f1a97f8"        # the quote currency, 0x0 for native ETH
SEL_TARGET = "0x7d05d553"       # the graduation target, quote units
SEL_REAL_QUOTE = "0xa49fd649"   # real quote reserve: + net on a buy, - gross on a sell
SEL_TOKENS_LEFT = "0x86ba97fd"  # tokens still on the curve
SEL_TOKENS_SOLD = "0x02c7e7af"  # tokens the curve has given out; + left = the curve supply
# The curve factory's curves (names from community code, return values checked live):
SEL_C_TARGET = "0xdfcc910f"          # NET_GRADUATION_TARGET()
SEL_C_REAL = "0x2ab04c7b"            # realEthReserve(): real quote reserve (quote units)
SEL_C_LEFT = "0x01d4da72"            # curveTokensRemaining()
SEL_C_VIRTUAL_QUOTE0 = "0x027fb5b1"  # INITIAL_VIRTUAL_ETH_RESERVE()
SEL_C_VIRTUAL_TOKENS = "0x343ee3b7"  # virtualTokenReserve(): left on the curve + 279.9M
SEL_C_GRADUATED = "0xe7c2b772"       # graduated()
C_CURVE_SUPPLY = 793_100_000 * 10 ** 18  # tokens on a curve of the curve factory
V6_CURVE_SUPPLY = 800_000_000 * 10 ** 18  # tokens on a V6 curve (its virtual tokens: a third more)
C_FEE_BPS = 125                          # its fee on every trade, in the quote

TICK_SPACING = 60
POOL_FEE = 0


@dataclass(frozen=True)
class Launch:
    launch_id: int
    launcher: str
    token: str
    curve: str
    quote: str          # abi.ZERO_ADDRESS for native ETH
    bps: int            # the charge per trade, tax and fee together
    treasury: str
    target: int         # graduation target, quote base units
    first_buy: int      # tokens the creator asked for at launch
    name: str | None
    symbol: str | None
    block: int
    tx: str
    log_index: int


@dataclass(frozen=True)
class Buy:
    curve: str
    caller: str         # the router, or the buyer itself
    recipient: str      # the buyer we show
    tokens_gross: int   # tokens out of the curve
    tokens: int         # tokens the buyer received
    tax_tokens: int     # tokens taken as tax (to the token contract)
    quote_net: int      # quote into the curve's reserve
    fee: int            # quote fee, on top of quote_net
    bps: int | None     # None for the creator's first buy (the event does not say)
    first: bool         # the creator's first buy, in the launch transaction
    block: int
    tx: str
    log_index: int

    @property
    def quote_paid(self) -> int:
        return self.quote_net + self.fee


@dataclass(frozen=True)
class Sell:
    curve: str
    seller: str         # the wallet that sold: the one we show
    recipient: str      # who got the quote (often the router)
    tokens: int         # tokens the seller gave
    tokens_net: int     # tokens into the curve's reserve
    tax_tokens: int
    quote_gross: int    # quote out of the curve's reserve
    fee: int            # quote fee, out of quote_gross

    block: int
    tx: str
    log_index: int

    @property
    def quote_received(self) -> int:
        return self.quote_gross - self.fee


@dataclass(frozen=True)
class Graduated:
    curve: str
    pool_id: str
    quote_to_pool: int
    tokens_to_pool: int
    block: int
    tx: str
    log_index: int


@dataclass(frozen=True)
class State:
    token: str
    quote: str
    target: int
    real_quote: int
    tokens_left: int
    tokens_sold: int
    complete: bool
    pool_id: str | None
    # The curve factory's curves report their virtual reserves; a V6 curve's follow from
    # its target and supply (target/3 and supply/3).
    virtual_quote0: int | None = None
    virtual_tokens: int | None = None   # beyond the tokens still on the curve

    @property
    def supply(self) -> int:
        return self.tokens_left + self.tokens_sold

    @property
    def phantom(self) -> int:
        return self.virtual_quote0 if self.virtual_quote0 is not None else virtual_quote(self.target)

    @property
    def extra_tokens(self) -> int:
        return self.virtual_tokens if self.virtual_tokens is not None else self.supply // 3

    @property
    def k(self) -> int:
        return self.phantom * (self.supply + self.extra_tokens)

    @property
    def progress(self) -> float:
        """The curve % as vibe/vibe's site shows it, which differs by generation: on a
        curve-factory curve the tokens sold of its tokens (the API's `curve.progressBps`),
        on a V6 curve the quote raised of the target (its API has no progress; the site
        divides `pairReserveUnits` by `targetPairUnits`). Both reach 100% at graduation;
        the tokens' share runs ahead (75.1% of the ETH is 92.0% of the tokens)."""
        if self.virtual_tokens is not None:  # a curve-factory curve
            return min(self.tokens_sold / self.supply, 1.0) if self.supply else 0.0
        return min(self.real_quote / self.target, 1.0) if self.target else 0.0

    def price(self, token_decimals: int = 18, quote_decimals: int = 18) -> float | None:
        """Spot price on the curve, whole quote per whole token."""
        y = self.tokens_left + self.extra_tokens
        if y <= 0:
            return None
        x = self.real_quote + self.phantom
        return x / y * 10 ** (token_decimals - quote_decimals)


def virtual_quote(target: int) -> int:
    return target // 3


def curve_k(target: int, supply: int) -> int:
    """quote * tokens of a fresh curve, which every trade keeps."""
    return virtual_quote(target) * (supply + supply // 3)


def _pos(lg: dict):
    try:
        return int(lg["blockNumber"], 16), lg["transactionHash"].lower(), int(lg["logIndex"], 16)
    except (KeyError, TypeError, ValueError):
        return None


def _topics(lg: dict) -> list[str]:
    return [t.lower() for t in lg.get("topics") or []]


def _string_at(data: bytes, word_index: int) -> str | None:
    offset = abi.word_uint(data, word_index)
    if offset is None or offset + 32 > len(data):
        return None
    length = int.from_bytes(data[offset:offset + 32], "big")
    raw = data[offset + 32:offset + 32 + length]
    if len(raw) != length:
        return None
    return "".join(c for c in raw.decode("utf-8", "replace") if c.isprintable()) or None


def decode_launch(lg: dict, factory: str) -> Launch | None:
    """A factory's LaunchCreated. Only the factory's own logs count: anyone can emit the
    same topics."""
    t = _topics(lg)
    pos = _pos(lg)
    if lg.get("address", "").lower() != factory.lower() or len(t) != 4 or pos is None:
        return None
    if t[0] == TOPIC_C_LAUNCH_CREATED:
        return _decode_c_launch(lg, t, pos)
    if t[0] != TOPIC_LAUNCH_CREATED:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    launcher, token = abi.topic_address(t[2]), abi.topic_address(t[3])
    curve, quote, treasury = (abi.word_address(data, i) for i in (0, 4, 7))
    bps, target, first_buy = (abi.word_uint(data, i) for i in (5, 8, 9))
    if None in (launcher, token, curve, quote, treasury, bps, target, first_buy):
        return None
    return Launch(int(t[1], 16), launcher, token, curve, quote, bps, treasury, target, first_buy,
                  _string_at(data, 1), _string_at(data, 2), *pos)


def _decode_c_launch(lg: dict, t: list[str], pos) -> Launch | None:
    """The curve factory's LaunchCreated. The quote and target are not in it (quote and
    target stay unknown here: abi.ZERO_ADDRESS and 0; state() reads them), nor are the
    name and symbol."""
    data = abi.to_bytes(lg.get("data", "0x"))
    token, curve = abi.topic_address(t[2]), abi.topic_address(t[3])
    creator, payout = abi.word_address(data, 0), abi.word_address(data, 1)
    first_buy = abi.word_uint(data, 4)
    if None in (token, curve, creator, payout, first_buy):
        return None
    return Launch(int(t[1], 16), creator, token, curve, abi.ZERO_ADDRESS, C_FEE_BPS, payout, 0, first_buy,
                  None, None, *pos)


def decode_buy(lg: dict) -> Buy | None:
    t = _topics(lg)
    pos = _pos(lg)
    if len(t) != 3 or t[0] != TOPIC_CURVE_BUY or pos is None:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    caller, recipient = abi.topic_address(t[1]), abi.topic_address(t[2])
    vals = [abi.word_uint(data, i) for i in range(6)]
    if caller is None or recipient is None or None in vals:
        return None
    gross, out, tax, net, fee, bps = vals
    if out + tax != gross:
        return None  # not the layout we know
    return Buy(lg["address"].lower(), caller, recipient, gross, out, tax, net, fee, bps, False, *pos)


def decode_first_buy(lg: dict) -> Buy | None:
    """InitialPurchase: the creator's first buy, as a Buy with the creator as recipient."""
    t = _topics(lg)
    pos = _pos(lg)
    if len(t) != 2 or t[0] != TOPIC_INITIAL_PURCHASE or pos is None:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    launcher = abi.topic_address(t[1])
    vals = [abi.word_uint(data, i) for i in range(4)]
    if launcher is None or None in vals:
        return None
    gross, out, net, fee = vals
    if out > gross:
        return None
    return Buy(lg["address"].lower(), launcher, launcher, gross, out, gross - out, net, fee, None, True, *pos)


def decode_sell(lg: dict) -> Sell | None:
    t = _topics(lg)
    pos = _pos(lg)
    if len(t) != 3 or t[0] != TOPIC_CURVE_SELL or pos is None:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    seller, recipient = abi.topic_address(t[1]), abi.topic_address(t[2])
    vals = [abi.word_uint(data, i) for i in range(5)]
    if seller is None or recipient is None or None in vals:
        return None
    tokens, net, tax, gross, fee = vals
    if net + tax != tokens or fee > gross:
        return None
    return Sell(lg["address"].lower(), seller, recipient, tokens, net, tax, gross, fee, *pos)


def decode_graduated(lg: dict) -> Graduated | None:
    t = _topics(lg)
    pos = _pos(lg)
    if len(t) != 2 or t[0] not in GRADUATED_TOPICS or pos is None:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    quote, tokens = abi.word_uint(data, 0), abi.word_uint(data, 1)
    if quote is None or tokens is None:
        return None
    return Graduated(lg["address"].lower(), t[1], quote, tokens, *pos)


def decode_c_buy(lg: dict) -> Buy | None:
    """A curve-factory Buy. The factory as caller marks the creator's buy at launch."""
    t = _topics(lg)
    pos = _pos(lg)
    if len(t) != 3 or t[0] != TOPIC_C_BUY or pos is None:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    caller, recipient = abi.topic_address(t[1]), abi.topic_address(t[2])
    vals = [abi.word_uint(data, i) for i in range(5)]
    if caller is None or recipient is None or None in vals:
        return None
    gross, net, out, creator_fee, protocol_fee = vals
    if gross - net != creator_fee + protocol_fee or net > gross:
        return None  # not the layout we know
    first = chain.vibe_factory(caller) is not None
    return Buy(lg["address"].lower(), caller, recipient, out, out, 0, net, gross - net, C_FEE_BPS, first, *pos)


def decode_c_sell(lg: dict) -> Sell | None:
    t = _topics(lg)
    pos = _pos(lg)
    if len(t) != 2 or t[0] != TOPIC_C_SELL or pos is None:
        return None
    data = abi.to_bytes(lg.get("data", "0x"))
    seller = abi.topic_address(t[1])
    vals = [abi.word_uint(data, i) for i in range(5)]
    if seller is None or None in vals:
        return None
    tokens, gross, net, creator_fee, protocol_fee = vals
    if gross - net != creator_fee + protocol_fee or net > gross:
        return None
    return Sell(lg["address"].lower(), seller, seller, tokens, tokens, 0, gross, gross - net, *pos)


def decode_trade(lg: dict) -> Buy | Sell | None:
    t0 = (_topics(lg) or [""])[0]
    if t0 == TOPIC_CURVE_BUY:
        return decode_buy(lg)
    if t0 == TOPIC_INITIAL_PURCHASE:
        return decode_first_buy(lg)
    if t0 == TOPIC_CURVE_SELL:
        return decode_sell(lg)
    if t0 == TOPIC_C_BUY:
        return decode_c_buy(lg)
    if t0 == TOPIC_C_SELL:
        return decode_c_sell(lg)
    return None


def pool_id(token: str, quote: str, hook: str) -> str:
    """The v4 pool a curve graduates into."""
    return chain.pool_id(token, quote, POOL_FEE, TICK_SPACING, hook)


def _as_pons_buy(b: Buy) -> chain.CurveBuy:
    # chain.curve_after_buy moves the curve by quote_in - fee - tax and tokens_out
    return chain.CurveBuy(b.curve, b.caller, b.recipient, b.quote_paid, b.tokens_gross, b.fee, 0,
                          b.block, b.tx, b.log_index)


def _as_pons_sell(s: Sell) -> chain.CurveSell:
    # chain.curve_after_sell moves the curve by quote_out + fee + tax and tokens_in
    return chain.CurveSell(s.curve, s.seller, s.recipient, s.tokens_net, s.quote_received, s.fee, 0,
                           s.block, s.tx, s.log_index)


def curve_supply(k: int, phantom: int) -> int | None:
    """The tokens a curve sells in all, from its constants: k / phantom is those tokens
    plus the curve's virtual ones, a third more on a V6 curve; a curve-factory curve
    sells C_CURVE_SUPPLY. None for a shape that is neither."""
    if not k or not phantom:
        return None
    y0 = k // phantom
    if abs(y0 * 3 // 4 - V6_CURVE_SUPPLY) <= 10 ** 18:
        return V6_CURVE_SUPPLY
    return C_CURVE_SUPPLY if y0 > C_CURVE_SUPPLY else None


def sold_share(k: int, phantom: int, quote_reserve: float) -> float | None:
    """Tokens sold of the curve's tokens once its quote reserve (virtual + real) is
    `quote_reserve`: tokens on the curve = k / reserve - virtual tokens (State.progress)."""
    supply = curve_supply(k, phantom)
    if supply is None or quote_reserve <= 0:
        return None
    return min(max((k / phantom - k / quote_reserve) / supply, 0.0), 1.0)


def after_trade(tr: Buy | Sell, k: int | None, phantom: int | None, target: int,
                token_decimals: int, quote_decimals: int) -> tuple[float | None, float | None]:
    """(spot price after the trade, progress 0..1) from the event alone, as for Pons: the
    same constant-product model, with the virtual quote at target/3. The progress is what
    vibe/vibe's site shows (State.progress): the quote raised on a V6 curve, the tokens
    sold on a curve-factory curve."""
    if isinstance(tr, Buy):
        price, raised = chain.curve_after_buy(_as_pons_buy(tr), k, phantom, target, token_decimals, quote_decimals)
    else:
        price, raised = chain.curve_after_sell(_as_pons_sell(tr), k, phantom, target, token_decimals, quote_decimals)
    if raised is None or not k or not phantom or curve_supply(k, phantom) != C_CURVE_SUPPLY:
        return price, raised
    sold = sold_share(k, phantom, phantom + raised * target)
    return price, sold if sold is not None else raised


def decode_state(token: str, quote: str, target: int, real: int, left: int, sold: int,
                 complete: int, pid: str) -> State:
    return State(token, quote, target, real, left, sold, bool(complete),
                 pid if pid and int(pid, 16) else None)


V6_SELS = (SEL_TOKEN, SEL_FACTORY, SEL_QUOTE, SEL_TARGET, SEL_REAL_QUOTE, SEL_TOKENS_LEFT,
           SEL_TOKENS_SOLD, SEL_COMPLETE, SEL_POOL_ID)
C_SELS = (SEL_C_TARGET, SEL_C_REAL, SEL_C_LEFT, SEL_C_VIRTUAL_QUOTE0, SEL_C_VIRTUAL_TOKENS, SEL_C_GRADUATED)


async def _calls(rpc, curve: str, sels) -> list:
    """Each call's answer, or the exception it raised: the selectors of one generation
    revert on a curve of the other."""
    if hasattr(rpc, "batch"):
        return await rpc.batch([("eth_call", [{"to": curve, "data": sel}, "latest"]) for sel in sels])
    out = []
    for sel in sels:
        try:
            out.append(await rpc.eth_call(curve, sel))
        except Exception as e:
            out.append(e)
    return out


async def state(rpc, curve: str, factory: str | None = None) -> State | None:
    """The curve's state, or None when it does not answer like a vibe/vibe curve (or, with
    `factory`, belongs to another factory). Both generations' views go out as one JSON-RPC
    batch when the client has one (the public RPC's Cloudflare counts requests, not
    calls); the curve's factory() says which answers count."""
    try:
        rets = await _calls(rpc, curve, V6_SELS + C_SELS)
    except Exception:
        return None
    if any(isinstance(r, Exception) for r in rets[:2]):
        return None
    try:
        token, got = (abi.word_address(abi.to_bytes(r or "0x"), 0) for r in rets[:2])
    except ValueError:
        return None
    if token is None or got is None or (factory is not None and got != factory.lower()):
        return None
    contracts = chain.vibe_factory(got)
    if contracts is not None and contracts.gen == "curve":
        return _c_state(token, contracts, rets[len(V6_SELS):])
    v6 = rets[:len(V6_SELS)]
    if any(isinstance(r, Exception) for r in v6):
        return None
    v6 = [r or "0x" for r in v6]
    try:
        quote = abi.word_address(abi.to_bytes(v6[2]), 0)
        nums = [abi.decode_uint(r) for r in v6[3:8]]
    except ValueError:
        return None
    pid = v6[8]
    if quote is None or None in nums or len(abi.to_bytes(pid)) != 32 or not nums[0]:
        return None
    return decode_state(token, quote, *nums, pid.lower())


def _c_state(token: str, contracts: chain.VibeContracts, rets: list) -> State | None:
    """A curve-factory curve's state: its quote from its target, its virtual reserves
    as it reports them, complete once it graduated (a full curve waits for its pool).
    The PoolId is not a view here: None (the caller builds it)."""
    if any(isinstance(r, Exception) for r in rets):
        return None
    try:
        target, real, left, vq0, vt, graduated = (abi.decode_uint(r or "0x") for r in rets)
    except ValueError:
        return None
    if None in (target, real, left, vq0, vt, graduated) or not target or vt < left:
        return None
    quote = dict(contracts.quotes).get(target)
    if quote is None:
        return None  # a quote asset we do not know: no honest numbers for it
    return State(token, quote, target, real, left, C_CURVE_SUPPLY - left, bool(graduated), None,
                 virtual_quote0=vq0, virtual_tokens=vt - left)


async def eth_price(rpc, curve: str, pool: str, decimals: int = 18) -> float | None:
    """ETH per whole token of a vibe/vibe token quoted in ETH: its curve's price, or once
    it graduated, its v4 pool's. Other launches quote their curve in such a token, so this
    gives their trades a dollar value (quotes.Rates, kind "vibe:<curve>:<pool>")."""
    st = await state(rpc, curve)
    if st is None or st.quote != abi.ZERO_ADDRESS:
        return None
    if not st.complete:
        return st.price(decimals, 18)
    got = await pools.v4_state(rpc, pool)
    if not got or not got[0]:
        return None
    return chain.v4_token_price(got[0], st.token, abi.ZERO_ADDRESS, decimals, 18)
