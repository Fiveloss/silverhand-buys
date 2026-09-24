# Chain facts and house rules

What the code relies on about Robinhood Chain, Pons v2 and Uniswap v4, and the
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

- **PoolId** of a graduate = keccak(currency0, currency1, fee 0, tickSpacing 200,
  hook), with native ETH as `0x0`. It matches DexScreener's pool id.
- **The curve** is constant product over (virtual 1.68 ETH + real quote, 1B
  tokens). Fee and tax come off quoteIn first. It graduates when the real quote
  reaches the threshold (4.2 ETH for config 0). Price and progress are
  computed from the event (`chain.curve_after_buy`), with no RPC call per trade.
- About a quarter of launches pair with an ERC-20 instead of ETH. Dollar values
  are shown only for ETH, WETH and USDG.

`tools/preflight.py` re-checks all of this on the live chain.

## Rules

- Never make up a number: a value that could not be read is left out, not shown as zero.
- Never ask for, store or use keys or seed phrases. The bot does not trade.
- Every post carries "NFA · DYOR"; nothing suggests an affiliation with Robinhood or Pons.
- Digital goods are sold in Telegram Stars only. `/terms` and `/paysupport` must keep working.
- Ads go live only after an owner approves them.
- Settings commands are admin-only; everyone else is ignored silently.
- Bot text is English.

## Checks

`python3 tests.py` (standard library only) and `python3 tests_bot.py` (needs the
requirements, no network). Before deploying: `python3 tools/preflight.py`.
