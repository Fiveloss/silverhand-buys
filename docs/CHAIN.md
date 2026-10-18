# Chain facts and house rules

What the code relies on about Robinhood Chain, Pons v2, Uniswap v2/v3/v4 and the stock
token feeds, and the
rules every change keeps. How to run the bot is in [README.md](../README.md).

## Chain facts (verified on mainnet, see [`fixtures/`](../fixtures/))

Robinhood Chain: chain id 4663, ~0.1 s blocks, public RPC
`https://rpc.mainnet.chain.robinhood.com`. Its limits:

- `eth_getLogs` returns at most 10,000 logs;
- it answers 429 when polled too fast;
- it keeps state for only ~6–8k blocks (10–13 minutes); logs go back to genesis.

| What | Value |
|---|---|
| Pons v2 factory | `0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e` |
| Pons hook on v4 pools | `0xe5e702641ea86f4ae6cc3cdaed2b886f976be044` |
| Uniswap v4 PoolManager | `0x8366a39cc670b4001a1121b8f6a443a643e40951` |
| `TokenLaunched` (factory) | `0x8d4aad49…`: token, curve, deployer indexed; pair, config, threshold in data |
| `CurveBuy` (curve) | `0xec36bf57…`: buyer, recipient indexed; quoteIn (gross), tokensOut, fee, tax |
| `CurveSell` (curve) | `0x8113d738…`: seller, recipient indexed; tokensIn, quoteOut (net: the curve pays out quoteOut + fee + tax), fee, tax |
| graduation (factory) | `0xcdb72f15…`: token indexed; quoteRaised, tokensToFactory. The name is unpublished |
| v4 `Swap` | `0x40e9cecb…`: deltas from the swapper's side, negative = paid into the pool |
| Uniswap v3 factory | `0x1f7d7550b1b028f7571e69a784071f0205fd2efa` (Uniswap's own) |
| Ramses v3 factory | `0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8` (same events as Uniswap v3) |
| Uniswap v2 factory | `0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f` (Uniswap's own) |
| v3 `Swap` | `0xc42079f9…`: deltas from the pool's side, positive = paid into the pool |
| v2 `Swap` / `Sync` | `0xd78ad95f…` in/out amounts / `0x1c411e9a…` the reserves, logged right before the swap |
| v4 `Initialize` | `0xdd466e67…`: id, currency0, currency1 indexed; fee, tickSpacing, hooks in data |
| Flap.sh Portal | `0x26605f322f7ff986f381bb9a6e3f5dab0beaeb09`: every Flap curve; events have no indexed fields |
| Flap `TokenBought` / `TokenSold` | `0xa800a203…` / `0x03a4693e…`: ts, token, trader, amount, eth (paid with the fee / received), fee, postPrice (quote per token, 18 decimals) |
| Flap progress / graduation | `0x4c35e20d…` token, progress (1e18 = full) / `0x6e4f4763…` `LaunchedToDEX` token, Uniswap v2 pair, amount, eth |

- **PoolId** of a graduate = keccak(currency0, currency1, fee 0, tickSpacing 200,
  hook), with native ETH as `0x0`. It matches DexScreener's pool id.
- **The curve** is constant product over (virtual 1.68 ETH + real quote, 1B
  tokens). Fee and tax come off quoteIn first. It graduates when the real quote
  reaches the threshold (4.2 ETH for config 0). Price and progress are
  computed from the event (`chain.curve_after_buy`), with no RPC call per trade.
- About a quarter of launches pair with an ERC-20 instead of ETH, often a stock token.
- **Any token's pools** are found by their creation events with the token as a topic
  filter over the whole chain (the RPC answers in seconds). Only pools created by the
  factories above, or initialized in the PoolManager, count: any contract can emit a
  fake `Swap`. A token can have hundreds of pools (bots open one per fee tier), so the
  busiest of the last hour (else day) are read first, at most twelve.
- **v4 pool state** lives in the PoolManager: slot0 at keccak256(poolId . 6), the active
  liquidity three slots later, read with `extsload`. A pool's depth is the virtual quote
  reserve of its active liquidity (v3 the same from `slot0()` and `liquidity()`), which
  also measures pools launched one-sided.
- **Flap.sh**: `getTokenV7(token)` (17 words) gives status (1 curve, 4 DEX), price, the
  pool after graduation and the quote (word 9): ETH, or a token. Flap's docs say ETH only on
  this chain, but mainnet has curves quoted in stock tokens (GME). A Flap buy's `eth` equals
  the value of a direct buy's transaction, so it includes the fee.
- **The buyer** of a pool trade: the pool's Transfer of the token, followed through the
  contracts (trading bots, routers) that pass the same amount on in the transaction.
- **Dollars** only for quotes listed in `buybot/quotes.py`, by address: ETH, WETH, USDG,
  and Robinhood stock tokens with a Chainlink feed on the chain (addresses from
  Robinhood's asset registry, `api.robinhood.com/rhj/assets`; feeds from Chainlink's
  list). A feed's price is per token (the share multiplier is included); stock feeds
  pause while the market is closed, so a price up to four days old still counts.
  Several fake "USDC" tokens trade on the chain: symbols are never trusted.

`tools/preflight.py` re-checks all of this on the live chain.

## Rules

- Never make up a number: a value that could not be read is left out, not shown as zero.
- Never ask for, store or use keys or seed phrases. The bot does not trade.
- Every post carries "NFA · DYOR"; nothing suggests an affiliation with Robinhood or Pons.
- The bot shows no ads and sells nothing: posts end with "NFA · DYOR" only, the same in every
  chat. `/terms` keeps the source link (AGPL).
- Settings commands are admin-only; everyone else is ignored silently.
- Bot text is English.

## Checks

`python3 tests.py` (standard library only) and `python3 tests_bot.py` (needs the
requirements, no network). Before deploying: `python3 tools/preflight.py`.
