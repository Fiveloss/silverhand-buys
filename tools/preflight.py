"""Live check against Robinhood Chain before starting the bot on a server.

    venv/bin/python tools/preflight.py

It re-verifies on the live chain every fact the bot relies on: chain id,
the Pons v2 factory and its events, the curve reads, the constant-product
model used for price and progress, a graduated token's Uniswap v4 pool id and
its swaps, the known quote tokens, the ETH price sources and the bot token.
Exit code 0 = safe to start; 1 = something is off (read the ❌ lines).
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402

from buybot import abi, chain  # noqa: E402
from buybot.config import load_dotenv  # noqa: E402
from buybot.prices import EthUsd  # noqa: E402
from buybot.rpc import Rpc  # noqa: E402

failures = 0


def ok(msg):
    print("✅", msg)


def warn(msg):
    print("⚠️ ", msg)


def fail(msg):
    global failures
    failures += 1
    print("❌", msg)


async def logs_back(rpc, head, span, **kw):
    """Logs over the last `span` blocks, in windows the public RPC accepts."""
    out, step = [], 5000
    for start in range(head - span, head, step):
        out += await rpc.get_logs(start + 1, min(start + step, head), **kw)
    return out


async def check_launch_and_curve(rpc, head):
    launches = await logs_back(rpc, head, 3000, address=chain.PONS_FACTORY,
                               topics=[chain.TOPIC_TOKEN_LAUNCHED])
    decoded = [chain.decode_token_launched(lg) for lg in launches]
    if not launches:
        fail("no TokenLaunched from the factory in the last 3000 blocks (~5 min): "
             "wrong factory address or topic?")
        return None
    if None in decoded:
        fail(f"{decoded.count(None)} of {len(launches)} TokenLaunched logs did not decode")
    else:
        ok(f"factory emits TokenLaunched: {len(launches)} launches in ~5 min, all decode")
    native = [d for d in decoded if d and d.pair == chain.NATIVE]
    launch = (native or [d for d in decoded if d])[-1]

    rec = chain.decode_launched_token(await rpc.eth_call(
        chain.PONS_FACTORY, abi.call_data(chain.SEL_GET_LAUNCHED_TOKEN, launch.token)))
    if not rec or not rec.exists or rec.curve != launch.curve:
        fail("getLaunchedToken does not match the TokenLaunched event")
    else:
        ok(f"getLaunchedToken record matches (fee {rec.pool_fee}, tickSpacing {rec.tick_spacing}, "
           f"phase {rec.phase})")

    data = abi.to_bytes(await rpc.eth_call(launch.curve, chain.SEL_GET_RESERVES))
    q, t = abi.word_uint(data, 0), abi.word_uint(data, 1)
    if len(data) != 64 or not q or not t:
        fail(f"getReserves on curve {launch.curve} returned {len(data)} bytes")
        return launch
    real = abi.decode_uint(await rpc.eth_call(launch.curve, chain.SEL_REAL_QUOTE_RESERVE))
    phantom = abi.decode_uint(await rpc.eth_call(launch.curve, chain.SEL_PHANTOM_QUOTE))
    if real is None or real > q:
        fail(f"realQuoteReserve unreadable or above quoteReserve ({real} vs {q})")
    elif phantom is not None and phantom != q - real:
        warn(f"phantomQuote {phantom} != quoteReserve - realQuoteReserve {q - real}: "
             "progress uses the second")
    else:
        ok(f"curve reads: quote {q / 1e18:.4f}, real {real / 1e18:.4f}, phantom {(q - real) / 1e18:.4f}")
    return launch


async def check_curve_model(rpc, head):
    trades = await logs_back(rpc, head, 2000, topics=[[chain.TOPIC_CURVE_BUY, chain.TOPIC_CURVE_SELL]])
    buys = [lg for lg in trades if lg["topics"][0].lower() == chain.TOPIC_CURVE_BUY]
    decoded = [b for b in (chain.decode_curve_buy(lg) for lg in buys) if b]
    if not decoded:
        warn("no CurveBuy in the last ~3 min; the price model was not checked live")
        return
    ok(f"CurveBuy decodes: {len(decoded)} of {len(buys)} buys in ~3 min")
    # only a block where the curve saw this one trade (no other buy, no sell) shows
    # the buy's own effect in the reserves before and after it
    per_block = {}
    for lg in trades:
        key = (lg["address"].lower(), int(lg["blockNumber"], 16))
        per_block[key] = per_block.get(key, 0) + 1
    checked = bad = 0
    for b in decoded[-40:]:
        curve, block = b.curve, b.block
        if per_block[(curve, block)] != 1 or head - block > 5000 or checked >= 15:
            continue
        try:
            after = abi.to_bytes(await rpc.eth_call(curve, chain.SEL_GET_RESERVES, block))
            before = abi.to_bytes(await rpc.eth_call(curve, chain.SEL_GET_RESERVES, block - 1))
        except Exception as e:
            warn(f"historical getReserves failed ({e}); model check skipped for one buy")
            continue
        q1, t1 = abi.word_uint(after, 0), abi.word_uint(after, 1)
        q0, t0 = abi.word_uint(before, 0), abi.word_uint(before, 1)
        if not all((q0, t0, q1, t1)) or t1 == 0:
            continue
        real = abi.decode_uint(await rpc.eth_call(curve, chain.SEL_REAL_QUOTE_RESERVE, block))
        phantom = q1 - real if real is not None else None
        price, _ = chain.curve_after_buy(b, q0 * t0, phantom, 10 ** 30, 18, 18)
        truth = q1 / t1
        checked += 1
        if price is None or abs(price - truth) / truth > 0.01:
            bad += 1
            bad_tx = b.tx
    if not checked:
        warn("could not isolate a single-buy block to check the price model")
    elif bad:
        fail(f"curve price model off by >1% on {bad} of {checked} buys (e.g. {bad_tx}): "
             "MC/progress would be wrong")
    else:
        ok(f"curve price model matches on-chain reserves on {checked} buys (within 1%)")


async def check_sell_model(rpc, head):
    trades = await logs_back(rpc, head, 2000, topics=[[chain.TOPIC_CURVE_BUY, chain.TOPIC_CURVE_SELL]])
    per_block = {}
    for lg in trades:
        key = (lg["address"].lower(), int(lg["blockNumber"], 16))
        per_block[key] = per_block.get(key, 0) + 1
    sells = [s for s in (chain.decode_curve_sell(lg) for lg in trades) if s]
    n_sell_logs = sum(lg["topics"][0].lower() == chain.TOPIC_CURVE_SELL for lg in trades)
    if not sells:
        warn("no CurveSell in the last ~3 min; the sell model was not checked live")
        return
    ok(f"CurveSell decodes: {len(sells)} of {n_sell_logs} sells in ~3 min")
    checked = bad = 0
    for s in sells[-30:]:
        if per_block[(s.curve, s.block)] != 1 or head - s.block > 5000 or checked >= 10:
            continue
        try:
            after = abi.to_bytes(await rpc.eth_call(s.curve, chain.SEL_GET_RESERVES, s.block))
            before = abi.to_bytes(await rpc.eth_call(s.curve, chain.SEL_GET_RESERVES, s.block - 1))
        except Exception as e:
            warn(f"historical getReserves failed ({e}); sell check skipped for one sell")
            continue
        q1, t1 = abi.word_uint(after, 0), abi.word_uint(after, 1)
        q0, t0 = abi.word_uint(before, 0), abi.word_uint(before, 1)
        if not all((q0, t0, q1, t1)):
            continue
        price, _ = chain.curve_after_sell(s, q0 * t0, None, 0, 18, 18)
        truth = q1 / t1
        checked += 1
        if price is None or abs(price - truth) / truth > 0.01 or q0 - q1 != s.quote_out + s.fee + s.tax:
            bad += 1
    if not checked:
        warn("could not isolate a single-sell block to check the sell model")
    elif bad:
        fail(f"curve sell model off on {bad} of {checked} sells: sell posts would be wrong")
    else:
        ok(f"curve sell model matches on-chain reserves on {checked} sells")


async def check_graduation_and_pool(rpc, head):
    grads = await logs_back(rpc, head, 60000, address=chain.PONS_FACTORY,
                            topics=[chain.TOPIC_GRADUATED])
    decoded = [chain.decode_graduated(lg) for lg in grads]
    if not grads:
        warn("no graduation in the last ~100 min; graduation topic not checked live")
        return
    if None in decoded:
        fail(f"{decoded.count(None)} of {len(grads)} graduation logs did not decode")
        return
    ok(f"graduation event decodes: {len(grads)} graduations in ~100 min")
    for g in reversed(decoded[-5:]):
        rec = chain.decode_launched_token(await rpc.eth_call(
            chain.PONS_FACTORY, abi.call_data(chain.SEL_GET_LAUNCHED_TOKEN, g.token)))
        if not rec or not rec.graduated:
            fail(f"{g.token} graduated by event but phase is {rec.phase if rec else '?'}")
            return
        pid = chain.pool_id(g.token, rec.pair, rec.pool_fee, rec.tick_spacing, chain.PONS_HOOK)
        swaps = await logs_back(rpc, head, 3000, address=chain.POOL_MANAGER,
                                topics=[chain.TOPIC_V4_SWAP, pid])
        if not swaps:
            continue
        ds = [chain.decode_v4_swap(lg) for lg in swaps]
        buys = sum(1 for s in ds if s and chain.v4_buy_amounts(s, g.token, rec.pair))
        ok(f"v4 pool id derived for {g.token[:10]}… has {len(swaps)} swaps in ~5 min "
           f"({buys} buys, {len(swaps) - buys} sells)")
        return
    warn("recent graduates had no v4 swaps in ~5 min; pool id not confirmed live this run")


async def check_quotes(rpc):
    for addr, (sym, dec, _) in chain.KNOWN_QUOTES.items():
        if addr == chain.NATIVE:
            continue
        try:
            s = abi.decode_string(await rpc.eth_call(addr, chain.SEL_SYMBOL))
            d = abi.decode_uint(await rpc.eth_call(addr, chain.SEL_DECIMALS))
        except Exception as e:
            fail(f"{sym} at {addr} unreadable: {e}")
            continue
        if (s, d) != (sym, dec):
            fail(f"{addr} is {s}/{d}, expected {sym}/{dec}: remove or fix it in chain.KNOWN_QUOTES")
        else:
            ok(f"quote token {sym} ({dec} decimals) at {addr}")


async def check_telegram(session):
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        warn("BOT_TOKEN not set, Telegram not checked")
        return
    try:
        async with session.get(f"https://api.telegram.org/bot{token}/getMe") as r:
            body = await r.json(content_type=None)
    except Exception as e:
        fail(f"Telegram API unreachable: {e}")
        return
    if body.get("ok"):
        me = body["result"]
        ok(f"bot token works: @{me.get('username')}")
        if not me.get("can_join_groups"):
            fail("adding to groups is disabled: BotFather → /setjoingroups → Enable")
    else:
        fail(f"bot token rejected: {body.get('description')}")


async def main():
    load_dotenv(ROOT / ".env")
    url = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
    async with aiohttp.ClientSession(trust_env=True) as session:
        rpc = Rpc(url, session, min_interval=0.4)
        try:
            chain_id = int(await rpc.call("eth_chainId", []), 16)
            head = await rpc.block_number()
        except Exception as e:
            fail(f"RPC {url} unreachable: {e}")
            return
        if chain_id != chain.CHAIN_ID:
            fail(f"RPC is chain {chain_id}, not Robinhood Chain {chain.CHAIN_ID}")
            return
        ok(f"RPC {url}: chain {chain_id}, block {head}")
        for step in (check_launch_and_curve(rpc, head), check_curve_model(rpc, head),
                     check_sell_model(rpc, head), check_graduation_and_pool(rpc, head),
                     check_quotes(rpc)):
            try:
                await step
            except Exception as e:
                fail(f"{step.__name__}: {type(e).__name__}: {e}")
        price = await EthUsd(session).get()
        (ok if price else fail)(f"ETH/USD price: {price}" if price else "no ETH/USD source reachable")
        await check_telegram(session)
        print(f"\nRPC calls: {rpc.calls}, retried errors: {rpc.errors}")


if __name__ == "__main__":
    asyncio.run(main())
    print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed.")
    sys.exit(1 if failures else 0)
