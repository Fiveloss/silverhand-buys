# Test fixtures

`pons_v2_mainnet.json` holds raw JSON-RPC responses from Robinhood Chain mainnet
(chain id 4663): a Pons v2 launch, its curve buys, a curve with buys and sells,
a factory graduation event, a `getLaunchedToken` record, curve reserves, and a
Uniswap v4 PoolId with the pool it names.

They were captured through the public RPC in September 2026 by the
[realorrug](https://github.com/1xmint/realorrug) project and published under the
Apache License 2.0 (`docs/research/data/0036-pons-v2-launch.json`,
`0040-graduation-tx-receipt.json`, `0040-curve-getter-probe.json`, and research
note 0050 §3). They are used here, unchanged, as test vectors. The file's
`source` field says the same.

`curve_sell_mainnet.json` is one `CurveSell` log with the curve's `getReserves()`
right before and after its block (the only trade on that curve in the block),
captured by this project through the public RPC on 2026-09-24. It pins down that
`quoteOut` is what the seller received: the curve pays out `quoteOut + fee + tax`.
