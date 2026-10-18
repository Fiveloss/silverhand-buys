"""Live check against Robinhood Chain before starting the bot on a server.

    venv/bin/python tools/preflight.py          # the deploy's check: about ten requests
    venv/bin/python tools/preflight.py --full   # everything below, ~150 requests

The deploy's check covers what differs per machine: the RPC answers for this chain, the
quote tokens and stock feeds read (in batches), an ETH price source and the bot token. The
public RPC's Cloudflare shuts out an IP that asks much more than that, and on the server
that IP is the running bot's; the decoding is covered by the offline tests on fixtures,
which the deploy runs first. With --full (from another machine, before a release) it
re-verifies on the live chain every fact the bot relies on: chain id,
the Pons v2 factory and its events, the curve reads, the constant-product
model used for price and progress, a graduated token's Uniswap v4 pool id and
its swaps, the Uniswap v2/v3/v4 pools of any token (creation events, swaps,
the v4 state read from the PoolManager), the known quote tokens and the stock
tokens' Chainlink feeds, the ETH price sources and the bot token.
Exit code 0 = safe to start; 1 = something is off (read the ❌ lines).
"""

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402

from buybot import abi, chain, flap, pools, quotes  # noqa: E402
from buybot.config import load_dotenv  # noqa: E402
from buybot.prices import EthUsd  # noqa: E402
from buybot.rpc import Rpc, RpcError  # noqa: E402

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
    for addr, (sym, dec, kind) in quotes.QUOTES.items():
        if addr == quotes.NATIVE or kind.startswith("feed:"):
            continue
        try:
            s = abi.decode_string(await rpc.eth_call(addr, chain.SEL_SYMBOL))
            d = abi.decode_uint(await rpc.eth_call(addr, chain.SEL_DECIMALS))
        except Exception as e:
            fail(f"{sym} at {addr} unreadable: {e}")
            continue
        if (s, d) != (sym, dec):
            fail(f"{addr} is {s}/{d}, expected {sym}/{dec}: remove or fix it in quotes.QUOTES")
        else:
            ok(f"quote token {sym} ({dec} decimals) at {addr}")


async def check_stock_feeds(rpc):
    """Each stock token answers with its symbol and 18 decimals, and its Chainlink feed
    with 8 decimals and a price. A feed older than FEED_MAX_AGE only warns: the bot then
    shows that quote without dollars."""
    stale, bad, unanswered = [], 0, []
    items = list(quotes.STOCKS.items())
    calls = []
    for addr, (sym, feed) in items:  # four reads each, all in a few batches
        for to, sel in ((addr, chain.SEL_SYMBOL), (addr, chain.SEL_DECIMALS),
                        (feed, chain.SEL_DECIMALS), (feed, quotes.SEL_LATEST_ROUND)):
            calls.append(("eth_call", [{"to": to, "data": sel}, "latest"]))
    answers = []
    for i in range(0, len(calls), 40):
        answers += await rpc.batch(calls[i:i + 40])
    for n, (addr, (sym, feed)) in enumerate(items):
        got4 = answers[n * 4:n * 4 + 4]
        if any(isinstance(a, Exception) or a is None for a in got4):
            unanswered.append(sym)
            continue
        s = abi.decode_string(got4[0])
        d = abi.decode_uint(got4[1])
        fd = abi.decode_uint(got4[2])
        got = quotes.decode_round(got4[3])
        if (s, d, fd) != (sym, 18, quotes.FEED_DECIMALS) or got is None:
            fail(f"stock {sym}: token {s}/{d}, feed decimals {fd}, round {got}: fix quotes.STOCKS")
            bad += 1
        elif time.time() - got[1] > quotes.FEED_MAX_AGE:
            stale.append(sym)
    if stale:
        warn(f"feeds older than {quotes.FEED_MAX_AGE // 86400} days (no dollars for them): {', '.join(stale)}")
    if unanswered:
        warn(f"the RPC did not answer for {', '.join(unanswered)}: not checked")
    if not bad:
        ok(f"{len(quotes.STOCKS)} stock tokens match their Chainlink feeds")


async def _busiest(rpc, head, topic, span=3000, **kw):
    logs = await logs_back(rpc, head, span, topics=[topic], **kw)
    by = {}
    for lg in logs:
        key = lg["topics"][1].lower() if kw.get("address") == chain.POOL_MANAGER else lg["address"].lower()
        by.setdefault(key, []).append(lg)
    return sorted(by.items(), key=lambda kv: -len(kv[1]))


async def check_v3_v2_pools(rpc, head):
    """A busy pool of each known factory: its factory() answers, its creation event is
    found by the token filter the bot uses, its swaps decode, and its state reads."""
    seen = set()
    for pool, swaps in (await _busiest(rpc, head, pools.TOPIC_V3_SWAP))[:40]:
        fac = "0x" + (await rpc.eth_call(pool, "0xc45a0155"))[-40:]
        if fac not in pools.V3_FACTORIES or fac in seen:
            continue
        seen.add(fac)
        t0 = "0x" + (await rpc.eth_call(pool, "0x0dfe1681"))[-40:]
        t1 = "0x" + (await rpc.eth_call(pool, "0xd21220a7"))[-40:]
        created = await rpc.get_logs(0, head, address=fac, topics=[pools.TOPIC_V3_POOL_CREATED,
                                                                   abi.address_topic(t0), abi.address_topic(t1)])
        if not any(abi.word_address(abi.to_bytes(lg["data"]), 1) == pool for lg in created):
            fail(f"{pools.V3_FACTORIES[fac]}: pool {pool} not found by its PoolCreated event")
            continue
        decoded = [pools.decode_v3_swap(lg) for lg in swaps]
        depth = await pools.quote_depth(rpc, pools.Candidate("v3", pool, t1, ""), t0)
        if None in decoded or depth is None:
            fail(f"{pools.V3_FACTORIES[fac]}: swaps or state of {pool} unreadable")
            continue
        ok(f"{pools.V3_FACTORIES[fac]}: pool {pool[:10]}… found by PoolCreated, {len(swaps)} swaps decode")
    for name in set(pools.V3_FACTORIES.values()) - {pools.V3_FACTORIES[f] for f in seen}:
        warn(f"{name}: no busy pool in ~5 min, not checked live this run")

    for pair, swaps in (await _busiest(rpc, head, pools.TOPIC_V2_SWAP, span=20_000))[:20]:
        fac = "0x" + (await rpc.eth_call(pair, "0xc45a0155"))[-40:]
        if fac not in pools.V2_FACTORIES:
            continue
        t0 = "0x" + (await rpc.eth_call(pair, "0x0dfe1681"))[-40:]
        t1 = "0x" + (await rpc.eth_call(pair, "0xd21220a7"))[-40:]
        created = await rpc.get_logs(0, head, address=fac, topics=[pools.TOPIC_V2_PAIR_CREATED,
                                                                   abi.address_topic(t0), abi.address_topic(t1)])
        if not any(abi.word_address(abi.to_bytes(lg["data"]), 0) == pair for lg in created):
            fail(f"Uniswap v2: pair {pair} not found by its PairCreated event")
            return
        swap = pools.decode_v2_swap(swaps[-1])
        block = swap.block
        around = await rpc.get_logs(block, block, address=pair, topics=[pools.TOPIC_V2_SYNC])
        if pools.sync_before(pools.v2_syncs(around), swap) is None:
            fail(f"Uniswap v2: no Sync before the swap in {swap.tx}: v2 prices would be averages")
            return
        ok(f"Uniswap v2: pair {pair[:10]}… found by PairCreated, swap and Sync decode")
        return
    warn("Uniswap v2: no busy pair in ~30 min, not checked live this run")


async def check_v4_pools(rpc, head):
    """A busy v4 pool: its Initialize log gives the key that hashes to its id, and the
    price read from the PoolManager's storage (slot 6) matches its last swap."""
    # ten seconds of v4 swaps: the whole chain's, and the public RPC chokes on a wider window
    # (Oct 2026). A busy RPC here only means the fact is not re-checked this run.
    try:
        busy = (await _busiest(rpc, head, chain.TOPIC_V4_SWAP, span=100, address=chain.POOL_MANAGER))[:20]
    except Exception as e:
        warn(f"Uniswap v4: the RPC did not answer the swap sample ({e}); not checked live this run")
        return
    keys = {}
    for pid, _ in busy[:3]:  # one id per query: the RPC takes far shorter ranges for a list
        for lg in await rpc.get_logs(0, head, address=chain.POOL_MANAGER, topics=[pools.TOPIC_V4_INITIALIZE, pid]):
            keys[lg["topics"][1].lower()] = lg
    for pid, swaps in busy:
        init = keys.get(pid)
        if init is None:
            continue
        data = abi.to_bytes(init["data"])
        c0, c1 = abi.topic_address(init["topics"][2]), abi.topic_address(init["topics"][3])
        if chain.pool_id(c1, c0, abi.word_uint(data, 0), abi.word_int(data, 1), abi.word_address(data, 2)) != pid:
            fail(f"v4 pool {pid}: its Initialize key does not hash to its id")
            return
        state = await pools.v4_state(rpc, pid)
        last = chain.decode_v4_swap(swaps[-1]).sqrt_price_x96
        if not state or not last or not 0.8 < state[0] / last < 1.25:
            fail(f"v4 pool {pid}: state read from the PoolManager ({state}) does not match its last swap")
            return
        ok(f"Uniswap v4: pool {pid[:10]}… key matches its id, price read from the PoolManager")
        return
    warn("Uniswap v4: no busy pool with an Initialize log found, not checked live this run")


async def check_flap(rpc, head):
    """Flap's Portal: its trades decode, a direct buy's `eth` is the transaction's value,
    and getTokenV7 answers with the last trade's price."""
    logs = await logs_back(rpc, head, 60_000, address=flap.PORTAL, topics=[[flap.TOPIC_BOUGHT, flap.TOPIC_SOLD]])
    trades = [flap.decode_trade(lg) for lg in logs]
    if not logs:
        warn("Flap: no trade in ~100 min, not checked live this run")
        return
    if None in trades:
        fail(f"Flap: {trades.count(None)} of {len(logs)} Portal trades did not decode")
        return
    direct = None
    for tr in reversed(trades):
        if tr.kind != "buy":
            continue
        tx = await rpc.call("eth_getTransactionByHash", [tr.tx])
        if tx and (tx.get("to") or "").lower() == flap.PORTAL:
            direct = (tr, int(tx["value"], 16))
            break
    if direct and direct[0].eth != direct[1]:
        fail(f"Flap: a direct buy's eth {direct[0].eth} is not its value {direct[1]}: amounts would be off")
        return
    last = trades[-1]
    st = await flap.state(rpc, last.token)
    if st is None or st.status not in (flap.STATUS_CURVE, flap.STATUS_DEX):
        fail(f"Flap: getTokenV7 of {last.token} unreadable ({st})")
        return
    ok(f"Flap: {len(trades)} Portal trades in ~100 min decode"
       + (", a direct buy's eth is its value" if direct else "") + ", getTokenV7 answers")


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


async def live_checks(rpc, url, chain_id, head):
    if chain_id != chain.CHAIN_ID:
        fail(f"RPC is chain {chain_id}, not Robinhood Chain {chain.CHAIN_ID}")
        return
    ok(f"RPC {url}: chain {chain_id}, block {head}")
    unanswered = []
    steps = ((check_quotes, (rpc,)), (check_stock_feeds, (rpc,)))
    if "--full" in sys.argv:
        steps = ((check_launch_and_curve, (rpc, head)), (check_curve_model, (rpc, head)),
                 (check_sell_model, (rpc, head)), (check_graduation_and_pool, (rpc, head)),
                 (check_v3_v2_pools, (rpc, head)), (check_v4_pools, (rpc, head)),
                 (check_flap, (rpc, head))) + steps
    else:
        print("   (the chain's events and pools: offline tests; --full checks them live)")
    for fn, args in steps:
        for attempt in (1, 2):
            try:
                await fn(*args)
                break
            except RpcError as e:
                if e.code != -1:
                    fail(f"{fn.__name__}: {e}")
                    break
                if attempt == 1 and "Cloudflare" in e.message:
                    continue  # the client has paused a minute: once more
                # the RPC kept failing (overloaded, an error page): nothing was disproven
                warn(f"{fn.__name__}: the RPC did not answer ({e.message}); not checked this run")
                unanswered.append(fn.__name__)
                break
            except Exception as e:
                fail(f"{fn.__name__}: {type(e).__name__}: {e}")
                break
    if len(unanswered) > (3 if len(steps) > 2 else 0):
        fail(f"the RPC failed {len(unanswered)} checks; too little was verified to start")


async def main():
    load_dotenv(ROOT / ".env")
    url = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
    async with aiohttp.ClientSession(trust_env=True) as session:
        rpc = Rpc(url, session, min_interval=1.2)
        try:
            chain_id = int(await rpc.call("eth_chainId", []), 16)
            head = await rpc.block_number()
        except RpcError as e:
            if "Cloudflare" not in e.message:
                fail(f"RPC {url} unreachable: {e}")
                return
            # The public RPC's Cloudflare shuts out an IP that asked too much: on the server,
            # usually the running older bot. Nothing is disproven, and the bot about to start
            # asks far less; the offline tests above have passed.
            warn("the RPC is rate-limiting this IP (Cloudflare challenge): live checks skipped "
                 "this run; run tools/preflight.py from another machine to cover them")
            chain_id = None
        except Exception as e:
            fail(f"RPC {url} unreachable: {e}")
            return
        if chain_id is not None:
            await live_checks(rpc, url, chain_id, head)
        price = await EthUsd(session).get()
        (ok if price else fail)(f"ETH/USD price: {price}" if price else "no ETH/USD source reachable")
        await check_telegram(session)
        print(f"\nRPC calls: {rpc.calls}, retried errors: {rpc.errors}")


if __name__ == "__main__":
    asyncio.run(main())
    print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed.")
    sys.exit(1 if failures else 0)
