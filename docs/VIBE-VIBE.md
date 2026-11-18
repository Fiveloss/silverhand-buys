# vibe/vibe on Robinhood Chain testnet: what the chain says

Read-only probes of the public testnet RPC (`https://rpc.testnet.chain.robinhood.com`, chain id 46630)
on 2026-10-12. The contracts are not verified and the team has not published an ABI. Event names
come from their signature hashes (the four below match `CurveBuy`, `CurveSell`, `InitialPurchase` and
`Graduated` exactly); field meanings were read off the token Transfers of the same transactions and
then replayed over a whole curve life (`fixtures/vibe_v6_testnet.json`, tests `VibeDecoding`). What
is still a guess is marked as such.

Mainnet: not live as of 2026-10-12 (vibevibe.fun redirects to testnet.vibevibe.fun).

## Contracts

| What | Address |
|---|---|
| V6 factory ("taxed", since Oct 2026) | `0xe794217880011f9ca6961340ed5c16ec9559fea0` |
| Curve factory (V2/V3 launches) | `0x40f1be6faf8dab9c143cce1a0a04c2075fb2df59` |
| Older curve factories | `0xb5b7a2f6c4eafa2d73918fca32d50e2126339eb9`, `0x4febc267e0c24440bcdef72b5dbc5fe7bed091df` |
| V6 pool hook (taxed) | `0xa5e27e3e54739162fe52c2c48e55b6dd6572e8ec` |
| Router (CurveBuy caller, CurveSell recipient) | `0x2784448c519d01d3ae257c0aac0b7cdad8acceb1` |
| Graduation locker (named by curve and token) | `0x7771e60708060c43c8562c4544fbd5c365e549c0` |
| Protocol treasury (gets the 0.0004 ETH launch fee) | `0x463e27522efa028f59e1c153d065b97cdef02495` |
| Uniswap v4 PoolManager (same address as mainnet) | `0x8366a39cc670b4001a1121b8f6a443a643e40951` |

Older factories and their graduation adapter, fee hook and lockers are listed in the MIT-licensed
community project https://github.com/blessedunit/exitliquid (`src/lib/chain.ts`, `state.ts`, `service.ts`).

## V6 events

| Event | topic0 | Indexed | Data |
|---|---|---|---|
| `LaunchCreated` (factory) | `0xa7e8032b…` | id, launcher, token | curve; name, symbol, metadata uri (strings); quote (0x0 = ETH); bps; a packed word (`0x1f41d4c07d0`, unknown); project treasury; graduation target; creator's first-buy tokens; then three words seen only on some launches (an amount, a bytes32, a count: probably an airdrop) |
| `InitialPurchase(address,uint256,uint256,uint256,uint256)` (curve) | `0x5b18697b…` | launcher | tokens gross, tokens to the launcher, quote net, fee |
| `CurveBuy(address,address,uint256×6)` (curve) | `0x55867fe9…` | caller (router), recipient (buyer) | tokens gross, tokens to the recipient, tax tokens, quote net, fee, bps |
| `CurveSell(address,address,uint256×5)` (curve) | `0x3899b2b9…` | seller (the wallet), recipient (often the router) | tokens in, tokens into the curve, tax tokens, quote out of the curve, fee |
| `Graduated(bytes32,uint256,uint256,uint256)` (curve) | `0xab164bba…` | poolId | quote to the pool, tokens to the pool (200M), 0 |
| `ProtocolFlushed`, `ProjectTreasuryPaid` (curve) | `0x3984ccae…`, `0x467a3335…` | | fee plumbing, not trades |

Checked against Transfers: a buyer pays quote net + fee (to the wei); the curve sends `tokens to the
recipient` to the buyer and `tax tokens` to the token contract (which sends 1/16 of it to `0xdead` at
once; what it does with the rest is not known); a seller sends `tokens in`, the curve sends `tax tokens` to the token contract and
`quote out - fee` to the recipient. The creator's first buy: the launch transaction's value is quote
net + fee + a 0.0004 ETH launch fee.

**The charge.** `bps` (100, 200 or 300 seen) is the whole charge, split per curve between a tax in
tokens and a fee in the quote: tax/gross + fee/net = bps/10000 for every buy (200 bps: 1.28% + 0.72%
on most curves; other splits seen, e.g. 0.16% + 1.84%). The split is a curve setting (two views
return it in 1e12 units). Why the event calls the total "bps" is a guess from the numbers.

## The curve

Constant product over virtual reserves, fitted exactly over a whole curve life:

- quote reserve = real + target/3; token reserve = left on the curve + 800M/3
  (1B minted, 800M on the curve, 200M kept for the pool);
- at the target the curve price is (4/3 · target) / (800M/3) = target / 200M, the price the pool opens at;
- a buy adds `quote net` to the real reserve and takes `tokens gross` off the curve; a sell takes
  `quote out` off the reserve and adds `tokens into the curve`. Replaying every event of one curve
  from launch ends at exactly 4 ETH and 0 tokens left, and `Graduated` follows in the buy that fills it.

Curve views (names unknown where marked; matched against the reserves around a live trade):

| Selector | Returns |
|---|---|
| `0xfc0c546a` `token()`, `0xc45a0155` `factory()` | the token, the V6 factory |
| `0x522e1177` `complete()`, `0x3e0dc34e` `poolId()` | 1 once the target is reached; the v4 PoolId after graduation |
| `0x7f1a97f8` (unknown) | the quote (0x0 = ETH) |
| `0x7d05d553` (unknown) | the graduation target |
| `0xa49fd649` (unknown) | the real quote reserve (0 after graduation) |
| `0x86ba97fd`, `0x02c7e7af` (unknown) | tokens left on the curve, tokens it gave out (sum: 800M) |
| `0xcf09e0d0` `createdAt()` | the launch's block time |

The token itself answers `curve()` (`0x7165485d`), `factory()`, `launcher()`, `hook()` and
`transfersUnlocked()`; the bot uses `curve()` to find the curve, then the factory's `LaunchCreated`
to confirm it.

## Quotes

A curve's quote is not always ETH. Busy testnet curves are quoted in other vibe tokens (LABIBI,
VIBEVIBE) or in testnet stock tokens (OPENAI, NVDA). The router then swaps ETH into the quote in that
token's v4 pool first, in the same transaction. The target is set in the quote's units (about the
worth of 4 ETH at launch). The bot values a quote that is itself a vibe token quoted in ETH by that
token's curve price, or by its pool once it graduated.

## How a graduation looks (tx `0xe38eee4a44d476203f6460b26d2cfeacd897cb2caea2903cd29c5eb794f4a5c9`)

One transaction: the last `CurveBuy` fills the curve, the curve sends the remaining tokens and the
target to the PoolManager, `PoolManager.Initialize` opens the pool (fee 0, **tickSpacing 60**,
hook = the taxed V6 hook), liquidity is added through the locker, the token emits
`TransfersUnlocked()`, and the curve emits `Graduated(poolId, ...)`.

The PoolId is the usual keccak256 of the PoolKey (token and quote sorted by address, fee 0,
tickSpacing 60, the V6 hook): checked on 11 graduations, ETH- and token-quoted. For an ETH curve
currency0 is native ETH; for a token-quoted curve the pool pairs the token with its quote.

## The graduated pool

The hook taxes pool trades in tokens: in the same transaction the PoolManager sends the tax to the
token contract. A Swap's token delta includes it: a buyer receives the delta less the tax; a seller
gives the delta plus the tax. On a buy the hook also takes about 0.72% of the ETH in as a fee, on
top of the Swap's ETH delta (the router's transaction value is the sum).

## The curve factory (`0x40f1be6f…df59`, read since 2026-10-19)

The deployment vibe/vibe's own config names (`/api/v1/chains/46630/config`: 5 ETH target, 1.25% fee
split 75/25 creator/protocol, 2% wallet cap, SPCX as a second quote asset). Hook `0x2779651f…d0cc`.
Field meanings from Transfers; one curve's whole life (878 buys, 806 sells) replays to the wei.

| Event | topic0 | Indexed | Data |
|---|---|---|---|
| LaunchCreated (factory) | `0x56eef5c3…` | id, token, curve | creator, its payout address, a per-launch word, launch-buy quote, launch-buy tokens, 1, bytes32, metadata uri |
| Buy (curve) | `0x8a525443…` | caller, recipient | quote gross, quote net, tokens out, creator fee, protocol fee (caller = buyer; the factory for the launch buy) |
| Sell (curve) | `0x917d0fe1…` | seller | tokens in, quote gross, quote net (to the seller), creator fee, protocol fee |
| Completed (curve) | `0x654e1d49…` | | real quote, time: emitted in the buy that reaches the target |
| Graduated (curve) | `0xc1eaf644…` | poolId | quote to the pool, tokens to the pool (206.9M), in a later keeper transaction |

No tax in tokens. The launch event names no quote, target, name or symbol: the curve's views do.

| View | Returns |
|---|---|
| `0xdfcc910f` `NET_GRADUATION_TARGET()` | the target (5 ETH; 82.74 for SPCX curves: the target tells the quote) |
| `0x2ab04c7b` `realEthReserve()` | real quote reserve |
| `0x01d4da72` `curveTokensRemaining()` | tokens left on the curve (793.1M at start) |
| `0x027fb5b1` `INITIAL_VIRTUAL_ETH_RESERVE()` | virtual quote at start: target × 279.9M / 793.1M |
| `0x343ee3b7` `virtualTokenReserve()` | tokens left + 279.9M |
| `0x522e1177` `complete()`, `0xe7c2b772` `graduated()` | full; graduated into its pool |

The pool: (ETH or the quote, token), fee 0, tickSpacing 60, this hook. Buyers call the curve
themselves (no router in between).

## The public testnet RPC

- `eth_getLogs` without an address: at most 30,000 blocks; with addresses and a list of topic
  values: 100,000 blocks; with one value per topic position: 10,000,000 blocks.
- No historical state: `eth_call` at an older block fails ("historical state ... is not available").
- About 6.7 blocks a second (mainnet: about 10).
- It sometimes stalls for the full 20 s timeout; the client retries.

## Still open (asked in the Discord ticket)

- the official names of the fields and views above, and of the packed launch-config word;
- whether the tax/fee split can change after launch;
- the mainnet date and mainnet addresses.
