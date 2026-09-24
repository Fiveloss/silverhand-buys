"""Invariant checks. Standard library only, no network: python3 tests.py

Chain decoding is checked against real Robinhood Chain mainnet logs in
fixtures/pons_v2_mainnet.json; the Telegram layer is smoke-tested separately
in tests_bot.py (needs the requirements installed).
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from buybot import abi, chain, render
from buybot.config import _plans, load_dotenv
from buybot.db import Chat, Db, Token
from buybot.keccak import keccak256
from buybot.watcher import Watcher

FIX = json.loads((Path(__file__).parent / "fixtures" / "pons_v2_mainnet.json").read_text())
# One mainnet curve sell with the curve's reserves right before and after it.
SELL = json.loads((Path(__file__).parent / "fixtures" / "curve_sell_mainnet.json").read_text())
TOKEN = FIX["launch"]["token"]
CURVE = FIX["launch"]["curve"]
DEPLOYER = FIX["launch"]["deployer"]
ETH = 10 ** 18


def fixture_token(**over) -> Token:
    t = Token(address=TOKEN, curve=CURVE, deployer=DEPLOYER, pair=chain.NATIVE,
              quote_symbol="ETH", quote_decimals=18, quote_kind="eth", name="Test Token",
              symbol="TST", decimals=18, total_supply=10 ** 27, threshold=42 * ETH // 10,
              pool_id=chain.pool_id(TOKEN), curve_k=168 * ETH // 100 * 10 ** 27,
              phantom=168 * ETH // 100, graduated=False, added_at=0)
    for k, v in over.items():
        setattr(t, k, v)
    return t


def chat(**over) -> Chat:
    c = Chat(chat_id=-100, title="t", token=TOKEN, min_usd=0, emoji="🟢", step_usd=10,
             media_kind=None, media_file_id=None, pro_until=0, active=True, created_at=0)
    for k, v in over.items():
        setattr(c, k, v)
    return c


def v4_swap_log(pool: str, amount0: int, amount1: int, sqrt_price: int, block: int = 100,
                log_index: int = 1) -> dict:
    def word(v: int) -> str:
        return format(v % (1 << 256), "064x")
    data = "0x" + word(amount0) + word(amount1) + word(sqrt_price) + word(10 ** 20) + word(-200) + word(0)
    return {"address": chain.POOL_MANAGER, "topics": [chain.TOPIC_V4_SWAP, pool,
                                                       abi.address_topic("0x" + "ab" * 20)],
            "data": data, "blockNumber": hex(block), "transactionHash": "0x" + "cd" * 32,
            "logIndex": hex(log_index)}


class Keccak(unittest.TestCase):
    def test_known_vectors(self):
        self.assertEqual(keccak256(b"").hex(),
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(abi.event_topic("Transfer(address,address,uint256)"),
                         "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
        self.assertEqual(abi.selector("balanceOf(address)"), "0x70a08231")

    def test_block_boundaries_do_not_crash(self):
        for n in (135, 136, 137, 272, 1000):
            self.assertEqual(len(keccak256(b"x" * n)), 32)

    def test_topics_and_selectors_match_their_signatures(self):
        self.assertEqual(abi.event_topic(
            "TokenLaunched(address,address,address,address,uint256,uint256)"), chain.TOPIC_TOKEN_LAUNCHED)
        self.assertEqual(abi.event_topic(
            "CurveBuy(address,address,uint256,uint256,uint256,uint256)"), chain.TOPIC_CURVE_BUY)
        self.assertEqual(abi.event_topic(
            "CurveSell(address,address,uint256,uint256,uint256,uint256)"), chain.TOPIC_CURVE_SELL)
        self.assertEqual(abi.event_topic(
            "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"), chain.TOPIC_V4_SWAP)
        for sig, sel in (("getLaunchedToken(address)", chain.SEL_GET_LAUNCHED_TOKEN),
                         ("realQuoteReserve()", chain.SEL_REAL_QUOTE_RESERVE),
                         ("phantomQuote()", chain.SEL_PHANTOM_QUOTE),
                         ("getReserves()", chain.SEL_GET_RESERVES), ("name()", chain.SEL_NAME),
                         ("symbol()", chain.SEL_SYMBOL), ("decimals()", chain.SEL_DECIMALS),
                         ("totalSupply()", chain.SEL_TOTAL_SUPPLY),
                         ("balanceOf(address)", chain.SEL_BALANCE_OF)):
            self.assertEqual(abi.selector(sig), sel, sig)


class Decoding(unittest.TestCase):
    def test_token_launched_from_mainnet(self):
        launch = chain.decode_token_launched(FIX["launch"]["log"])
        self.assertEqual((launch.token, launch.curve, launch.deployer), (TOKEN, CURVE, DEPLOYER))
        self.assertEqual(launch.pair, chain.NATIVE)
        self.assertEqual(launch.threshold, 42 * ETH // 10)

    def test_launched_token_record(self):
        rec = chain.decode_launched_token(FIX["launch"]["get_launched_token_return"])
        self.assertTrue(rec.exists)
        self.assertFalse(rec.graduated)
        self.assertEqual((rec.token, rec.curve, rec.pool_fee, rec.tick_spacing), (TOKEN, CURVE, 0, 200))
        self.assertEqual(rec.creator_tax_bps, 100)
        self.assertIsNone(chain.decode_launched_token("0x1234"))

    def test_not_a_pons_token_reads_as_missing(self):
        rec = chain.decode_launched_token("0x" + "00" * 32 * 15)
        self.assertFalse(rec.exists)

    def test_curve_buys_from_mainnet(self):
        buys = [chain.decode_curve_buy(lg) for lg in FIX["curve_buys_first_curve"]]
        first = buys[0]
        self.assertEqual(first.recipient, DEPLOYER)  # the launcher's own buy, through the router
        self.assertEqual(first.caller, "0xe33e9e479df8802cb0866d5d05258bec4cf62948")
        self.assertEqual(first.quote_in, 5 * ETH // 100)
        self.assertEqual(first.tokens_out // ETH, 28_340_080)
        self.assertEqual(first.fee, first.tax)

    def test_only_buys_decode_as_buys(self):
        logs = FIX["curve_logs_second_curve"]["logs"]
        decoded = [chain.decode_curve_buy(lg) for lg in logs]
        buys = [lg for lg in logs if lg["topics"][0] == chain.TOPIC_CURVE_BUY]
        self.assertEqual(sum(d is not None for d in decoded), len(buys))
        self.assertGreater(len(buys), 0)
        self.assertLess(len(buys), len(logs))  # sells and a fee sweep are in there too

    def test_curve_sells_from_mainnet(self):
        logs = FIX["curve_logs_second_curve"]["logs"]
        sells = [lg for lg in logs if lg["topics"][0] == chain.TOPIC_CURVE_SELL]
        self.assertGreater(len(sells), 0)
        self.assertTrue(all(chain.decode_curve_sell(lg) for lg in sells))
        self.assertTrue(all(chain.decode_curve_sell(lg) is None for lg in logs if lg not in sells))
        s = chain.decode_curve_sell(SELL["log"])
        self.assertEqual(s.recipient, "0x81c34be30586b2d55637b0afd40243a0d189bd31")
        self.assertGreater(s.fee, 0)

    def test_graduation_from_mainnet(self):
        g = chain.decode_graduated(FIX["graduation"]["log"])
        self.assertEqual(g.token, FIX["graduation"]["token"])
        self.assertEqual(g.quote_raised, 42 * ETH // 10 + 263)
        wrong = dict(FIX["graduation"]["log"], address="0x" + "11" * 20)
        self.assertIsNone(chain.decode_graduated(wrong))

    def test_v4_pool_id_matches_the_live_pool(self):
        v = FIX["v4_pool_id"]
        self.assertEqual(chain.pool_id(v["token"], chain.NATIVE, v["fee"], v["tick_spacing"], v["hook"]),
                         v["pool_id"])

    def test_strings(self):
        enc = "0x" + format(32, "064x") + format(3, "064x") + b"TST".hex().ljust(64, "0")
        self.assertEqual(abi.decode_string(enc), "TST")
        self.assertEqual(abi.decode_string("0x" + b"OLD".hex().ljust(64, "0")), "OLD")
        self.assertIsNone(abi.decode_string("0x" + format(32, "064x")))


class CurveMath(unittest.TestCase):
    def test_model_reproduces_the_real_first_buy(self):
        t = fixture_token()
        b = chain.decode_curve_buy(FIX["curve_buys_first_curve"][0])
        net = b.quote_in - b.fee - b.tax
        # constant product from the phantom 1.68 ETH and 1e9 tokens gives exactly the tokens out
        expected = 10 ** 27 * net // (t.phantom + net)
        self.assertLess(abs(expected - b.tokens_out), 10 ** 12)

    def test_price_and_progress_rise_with_each_buy(self):
        t = fixture_token()
        results = [chain.curve_after_buy(chain.decode_curve_buy(lg), t.curve_k, t.phantom,
                                         t.threshold, 18, 18) for lg in FIX["curve_buys_first_curve"]]
        prices = [r[0] for r in results]
        progress = [r[1] for r in results]
        self.assertEqual(prices, sorted(prices))
        self.assertEqual(progress, sorted(progress))
        self.assertAlmostEqual(progress[0], 0.049 / 4.2, places=6)
        self.assertAlmostEqual(prices[0], 1.729 ** 2 / 1.68 / 1e9, delta=1e-12)

    def test_a_curve_that_does_not_fit_the_model_gives_nothing(self):
        t = fixture_token()
        b = chain.decode_curve_buy(FIX["curve_buys_first_curve"][0])
        self.assertEqual(chain.curve_after_buy(b, t.curve_k // 100, t.phantom, t.threshold, 18, 18),
                         (None, None))
        self.assertEqual(chain.curve_after_buy(b, None, t.phantom, t.threshold, 18, 18), (None, None))

    def test_v4_buy_and_sell(self):
        pool = chain.pool_id(TOKEN)
        # native ETH is currency0; a buy pays ETH (negative) and takes tokens (positive)
        buy = chain.decode_v4_swap(v4_swap_log(pool, -ETH // 2, 10 ** 24, 2 ** 96))
        self.assertEqual(chain.v4_buy_amounts(buy, TOKEN, chain.NATIVE), (ETH // 2, 10 ** 24))
        sell = chain.decode_v4_swap(v4_swap_log(pool, ETH // 2, -10 ** 24, 2 ** 96))
        self.assertIsNone(chain.v4_buy_amounts(sell, TOKEN, chain.NATIVE))
        self.assertEqual(chain.v4_sell_amounts(sell, TOKEN, chain.NATIVE), (ETH // 2, 10 ** 24))
        self.assertIsNone(chain.v4_sell_amounts(buy, TOKEN, chain.NATIVE))

    def test_sell_model_reproduces_the_real_reserves(self):
        s = chain.decode_curve_sell(SELL["log"])
        qb, tb = (int(x) for x in SELL["reserves_before"])
        qa, ta = (int(x) for x in SELL["reserves_after"])
        # what the sell did to the curve: tokens in, quoteOut + fee + tax out
        self.assertEqual(ta - tb, s.tokens_in)
        self.assertEqual(qb - qa, s.quote_out + s.fee + s.tax)
        phantom, threshold = 168 * ETH // 100, 42 * ETH // 10
        price, progress = chain.curve_after_sell(s, qb * tb, phantom, threshold, 18, 18)
        self.assertAlmostEqual(price / (qa / ta), 1, places=9)
        self.assertAlmostEqual(progress, (qa - phantom) / threshold, places=9)
        self.assertEqual(chain.curve_after_sell(s, None, phantom, threshold, 18, 18), (None, None))

    def test_v4_price(self):
        # 1 ETH = 1,000,000 tokens -> sqrtPrice of token1/token0 = sqrt(1e6) * 2**96
        sqrt_p = 1000 * 2 ** 96
        price = chain.v4_token_price(sqrt_p, TOKEN, chain.NATIVE, 18, 18)
        self.assertAlmostEqual(price, 1e-6, places=12)
        # with the token as currency0 the same number means the inverse
        low = "0x" + "00" * 19 + "01"
        self.assertAlmostEqual(chain.v4_token_price(sqrt_p, low, "0x" + "ff" * 20, 18, 18), 1e6, places=3)


class Rendering(unittest.TestCase):
    def payload(self, **over):
        p = render.sample_payload("TST", "ETH", 3000.0)
        p.update(token=TOKEN, tx="0x" + "ab" * 32)
        p.update(over)
        return p

    def test_numbers(self):
        self.assertEqual(render.fmt_num(1234.5), "1,234")
        self.assertEqual(render.fmt_num(12.345), "12.35")
        self.assertEqual(render.fmt_num(0.004567), "0.004567")
        self.assertEqual(render.fmt_compact(2_100_000), "2.1M")
        self.assertEqual(render.fmt_usd(84_200), "$84,200")
        self.assertEqual(render.fmt_usd(1_250_000), "$1.25M")
        self.assertEqual(render.fmt_usd(12.5), "$12.50")

    def test_one_mark_per_post(self):
        c = chat(step_usd=10)
        for usd in (5, 55, 10 ** 9, None):
            text = render.buy_text(render.sample_payload("TST", "ETH", 3000.0) | {"usd": usd}, c)
            self.assertEqual(text.count("🟢"), 1)  # one mark, whatever the size
        self.assertEqual(render.buy_text(render.sample_payload("TST", "ETH", 3000.0), c,
                                         sell=True).count("🔴"), 1)

    def test_buy_post(self):
        text = render.buy_text(self.payload(), chat(), explorer="https://x")
        self.assertIn("TST Buy!", text)
        self.assertIn("💵 Spent <b>$1,050</b> · 0.35 ETH", text)
        self.assertIn("🪙 Got <b>2.1M</b> TST", text)
        self.assertIn("<blockquote>", text)
        self.assertIn("43%", text)
        self.assertIn('href="https://x/address/0x3f92', text)

    def test_unknown_numbers_are_left_out_not_zero(self):
        text = render.buy_text(self.payload(usd=None, mc_usd=None, mc_quote=None, progress=None,
                                            buyer=None), chat())
        self.assertNotIn("$", text)
        self.assertNotIn("MC", text)
        self.assertNotIn("Curve", text)

    def test_names_are_escaped(self):
        text = render.buy_text(self.payload(symbol="<b>X</b>"), chat())
        self.assertNotIn("<b>X</b>", text)
        self.assertIn("&lt;b&gt;X", text)

    def test_holder_lines(self):
        from buybot.watcher import holder_note
        supply = 1e9
        self.assertEqual(holder_note("buy", 1000, 1000, supply), {"new": True, "share": 1e-6})
        pos = holder_note("buy", 4000, 1000, supply)  # held 3000, bought 1000: +33%
        self.assertAlmostEqual(pos["change"], 1 / 3)
        self.assertIsNone(holder_note("buy", 500, 1000, supply))  # tokens went elsewhere
        self.assertEqual(holder_note("sell", 0, 1000, supply), {"all": True})
        self.assertAlmostEqual(holder_note("sell", 3000, 1000, supply)["change"], -0.25)

        c = chat()
        self.assertIn("🆕 New holder", render.buy_text(self.payload(holder={"new": True}), c))
        text = render.buy_text(self.payload(holder={"change": 1 / 3, "share": 0.0012}), c)
        self.assertIn("💼 Position +33% · holds 0.12%", text)
        self.assertIn("🚪 Продал всё", render.buy_text(self.payload(holder={"all": True}), chat(lang="ru"),
                                                      sell=True))
        self.assertNotIn("💼", render.buy_text(self.payload(), c))

    def test_sell_post(self):
        text = render.buy_text(self.payload(), chat(), sell=True)
        self.assertIn("<b>TST Sell!</b>", text)
        self.assertIn("🔴", text)
        self.assertNotIn("🟢", text)
        self.assertIn("🪙 Sold <b>2.1M</b> TST", text)
        self.assertIn("💰 Got <b>$1,050</b>", text)
        self.assertIn("👤 Seller", text)

    def test_merged_buys(self):
        items = [self.payload(quote=0.1, usd=300), self.payload(quote=2.0, usd=6000),
                 self.payload(quote=0.5, usd=1500)]
        main, more = render.pick_main(items)
        self.assertEqual(main["quote"], 2.0)
        text = render.buy_text(main, chat(), more)
        self.assertIn("2 more buys <b>$1,800</b> · 0.6 ETH", text)

    def test_footer(self):
        self.assertNotIn("via", render.footer(True, None, "bot"))
        self.assertIn("NFA", render.footer(True, None, "bot"))
        free = render.footer(False, None, "bot")
        self.assertIn("via @bot", free)
        self.assertIn("startgroup", free)
        from buybot.db import Ad
        ad = Ad(1, 1, "Buy <now>", 'https://a.b/"x', 1, 1, "paid", 0, 1)
        text = render.footer(False, ad, "bot")
        self.assertIn("Buy &lt;now&gt;", text)
        self.assertIn("&quot;", text)

    def test_buttons(self):
        pairs = render.buttons(self.payload(), "https://gmgn.ai/robinhood/token/REF_{token}", "https://e")
        self.assertEqual(pairs[0][1], f"https://gmgn.ai/robinhood/token/REF_{TOKEN}")
        self.assertTrue(pairs[1][1].startswith("https://e/tx/0x"))


class Storage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Db(os.path.join(self.dir.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def test_token_round_trip_keeps_big_integers(self):
        t = fixture_token()
        self.db.upsert_token(t)
        back = self.db.get_token(TOKEN)
        self.assertEqual(back.total_supply, 10 ** 27)
        self.assertEqual(back.curve_k, t.curve_k)
        self.db.upsert_token(fixture_token(graduated=True))
        self.db.upsert_token(fixture_token(graduated=False))
        self.assertTrue(self.db.get_token(TOKEN).graduated)  # never un-graduates

    def test_outbox_is_deduplicated_and_atomic(self):
        self.db.ensure_chat(-1)
        rows = [(-1, "buy", "buy:0x1:1", {"a": 1}), (-1, "buy", "buy:0x1:1", {"a": 1})]
        self.assertEqual(self.db.commit_tick(rows, 10), 1)
        self.assertEqual(self.db.commit_tick(rows, 11), 0)
        self.assertEqual(self.db.last_block(), 11)
        self.assertEqual(self.db.outbox_size(), 1)

    def test_pro_extends_from_now_or_from_the_current_end(self):
        self.db.ensure_chat(-1)
        now = 1_000_000
        self.assertEqual(self.db.extend_pro(-1, 100, now), now + 100)
        self.assertEqual(self.db.extend_pro(-1, 100, now), now + 200)

    def test_ads_never_overlap(self):
        a = self.db.create_ad(1, "a", "https://a", 2, 10)
        b = self.db.create_ad(2, "b", "https://b", 1, 5)
        s1, e1 = self.db.schedule_paid_ad(a, "c1", now=1000)
        s2, e2 = self.db.schedule_paid_ad(b, "c2", now=1000)
        self.assertEqual((s1, e1), (1000, 1000 + 2 * 86400))
        self.assertEqual(s2, e1)
        self.assertEqual(self.db.current_ad(now=1001).id, a)
        self.assertEqual(self.db.current_ad(now=e1 + 1).id, b)
        self.assertIsNone(self.db.current_ad(now=e2 + 1))

    def test_payments_are_recorded_once(self):
        self.assertTrue(self.db.record_payment("x", 1, 500, "pro:-1:1"))
        self.assertFalse(self.db.record_payment("x", 1, 500, "pro:-1:1"))

    def test_migration_and_removal(self):
        self.db.ensure_chat(-1)
        self.db.update_chat(-1, token=TOKEN, emoji="🚀")
        self.db.add_outbox(-1, "buy", "d", {})
        self.db.migrate_chat(-1, -1001)
        self.assertIsNone(self.db.get_chat(-1))
        self.assertEqual(self.db.get_chat(-1001).emoji, "🚀")
        self.assertEqual(self.db.pending_outbox()[0].chat_id, -1001)
        self.db.deactivate_chat(-1001)
        self.assertEqual(self.db.outbox_size(), 0)
        self.assertEqual(self.db.tracked_tokens(), [])

    def test_a_database_from_before_sells_gets_the_column(self):
        with self.db.conn:
            self.db.conn.execute("ALTER TABLE chats DROP COLUMN sells")
            self.db.conn.execute("INSERT INTO chats(chat_id, created_at) VALUES (-1, 0)")
        self.db.close()
        self.db = Db(os.path.join(self.dir.name, "t.db"))
        self.assertFalse(self.db.get_chat(-1).sells)
        self.db.update_chat(-1, sells=1)
        self.assertTrue(self.db.get_chat(-1).sells)

    def test_a_trade_seen_again_learns_its_wallet(self):
        self.db.add_trades([(TOKEN, "tx:1", 100, 1e-8, 0.5, 1)])
        self.db.add_trades([(TOKEN, "tx:1", 100, 1e-8, 0.5, 1, "0x" + "aa" * 20)])
        self.db.add_trades([(TOKEN, "tx:1", 100, 1e-8, 0.5, 1, None)])  # never forgets it again
        rows, wallets = self.db.top_buyers(TOKEN, 0)
        self.assertEqual((rows, wallets), ([("0x" + "aa" * 20, 0.5, 1)], 1))
        self.assertEqual(len(self.db.trades_since(TOKEN, 0)), 1)

    def test_daily_backups_keep_the_last_seven(self):
        import sqlite3
        folder = os.path.join(self.dir.name, "backups")
        self.db.ensure_chat(-1, "Kept")
        day = 86400
        made = [self.db.backup(folder, now=1_700_000_000 + k * day) for k in range(9)]
        self.assertTrue(all(made))
        self.assertIsNone(self.db.backup(folder, now=1_700_000_000 + 8 * day))  # once a day
        copies = sorted(os.listdir(folder))
        self.assertEqual(len(copies), 7)
        self.assertEqual(copies[-1], os.path.basename(made[-1]))
        con = sqlite3.connect(made[-1])
        self.assertEqual(con.execute("SELECT title FROM chats").fetchone()[0], "Kept")
        con.close()

    def test_unknown_setting_is_refused(self):
        self.db.ensure_chat(-1)
        with self.assertRaises(ValueError):
            self.db.update_chat(-1, pro_until=10 ** 10)


class FakeRpc:
    def __init__(self, head: int, logs: list[dict], graduated: bool = False):
        self.head = head
        self.logs = logs
        self.graduated = graduated
        self.get_logs_calls = []

    async def block_number(self):
        return self.head

    async def get_logs(self, frm, to, address=None, topics=None):
        self.get_logs_calls.append((frm, to, address, topics))
        addrs = {a.lower() for a in ([address] if isinstance(address, str) else address or [])}
        first = topics[0] if topics else None
        first = set(first) if isinstance(first, list) else {first} if first else None
        second = set(topics[1]) if topics and len(topics) > 1 else None
        out = []
        for lg in self.logs:
            b = int(lg["blockNumber"], 16)
            if not frm <= b <= to or (addrs and lg["address"].lower() not in addrs):
                continue
            if first and lg["topics"][0] not in first:
                continue
            if second and lg["topics"][1] not in second:
                continue
            out.append(lg)
        return out

    async def eth_call(self, to, data, block="latest"):
        words = ["0"] * 15
        words[10] = "2" if self.graduated else "0"
        words[14] = "1"
        return "0x" + "".join(w.rjust(64, "0") for w in words)


class FakeEthUsd:
    current = 3000.0

    async def get(self):
        return 3000.0


def run(coro):
    return asyncio.run(coro)


class WatcherTicks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Db(os.path.join(self.dir.name, "t.db"))
        self.db.upsert_token(fixture_token())
        self.db.ensure_chat(-1)
        self.db.update_chat(-1, token=TOKEN)
        self.buys = FIX["curve_buys_first_curve"]
        self.first_block = int(self.buys[0]["blockNumber"], 16)
        self.last_block = int(self.buys[-1]["blockNumber"], 16)

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def watcher(self, rpc, **kw):
        w = Watcher(rpc, self.db, FakeEthUsd(), **kw)
        w._phase_checked = time.time()  # phase refresh tested separately
        return w

    def test_first_start_begins_at_the_tip(self):
        rpc = FakeRpc(self.last_block, self.buys)
        self.assertEqual(run(self.watcher(rpc).tick()), 0)
        self.assertEqual(self.db.last_block(), self.last_block)
        self.assertEqual(self.db.outbox_size(), 0)

    def test_buys_reach_the_outbox_once(self):
        self.db.set_last_block(self.first_block - 1)
        rpc = FakeRpc(self.last_block, self.buys)
        w = self.watcher(rpc)
        self.assertEqual(run(w.tick()), 3)
        rows = self.db.pending_outbox()
        self.assertTrue(rows[0].payload["dev"])
        self.assertAlmostEqual(rows[0].payload["usd"], 150.0)
        self.assertIsNotNone(rows[0].payload["progress"])
        self.assertEqual(self.db.last_block(), self.last_block)
        # a restart that replays the same blocks adds nothing
        self.db.set_last_block(self.first_block - 1)
        self.assertEqual(run(w.tick()), 0)
        self.assertEqual(self.db.outbox_size(), 3)

    def test_min_buy_filter(self):
        self.db.update_chat(-1, min_usd=120)
        self.db.set_last_block(self.first_block - 1)
        run(self.watcher(FakeRpc(self.last_block, self.buys)).tick())
        self.assertEqual(self.db.outbox_size(), 1)  # only the 0.05 ETH ($150) buy

    def test_far_behind_skips_ahead_instead_of_flooding(self):
        self.db.set_last_block(self.first_block - 100_000)
        w = self.watcher(FakeRpc(self.last_block, self.buys), max_catchup=5)
        run(w.tick())
        # only the two buys inside the last 5 blocks are posted; the old one is skipped
        self.assertEqual(self.db.outbox_size(), 2)
        self.assertEqual(w.skipped_blocks, self.last_block - 5 - (self.first_block - 100_000))

    def test_graduation_then_v4_buys_in_the_same_range(self):
        token = fixture_token()
        grad = dict(FIX["graduation"]["log"],
                    topics=[chain.TOPIC_GRADUATED, abi.address_topic(TOKEN)],
                    blockNumber=hex(self.last_block + 1), logIndex="0x1")
        swap = v4_swap_log(token.pool_id, -ETH, 10 ** 24, 1000 * 2 ** 96, block=self.last_block + 2)
        self.db.set_last_block(self.last_block)
        rpc = FakeRpc(self.last_block + 2, [grad, swap])
        self.assertEqual(run(self.watcher(rpc).tick()), 2)
        kinds = [r.kind for r in self.db.pending_outbox()]
        self.assertEqual(kinds, ["grad", "buy"])
        v4 = self.db.pending_outbox()[1].payload
        self.assertEqual(v4["venue"], "v4")
        self.assertAlmostEqual(v4["mc_usd"], 1e-6 * 1e9 * 3000, places=3)
        self.assertTrue(self.db.get_token(TOKEN).graduated)

    def test_no_pool_query_while_on_the_curve(self):
        self.db.set_last_block(self.first_block - 1)
        rpc = FakeRpc(self.last_block, self.buys)
        run(self.watcher(rpc).tick())
        self.assertEqual(len(rpc.get_logs_calls), 1)

    def test_graduated_tokens_skip_the_curve_query(self):
        self.db.mark_graduated(TOKEN)
        self.db.set_last_block(self.first_block - 1)
        rpc = FakeRpc(self.last_block, self.buys)
        run(self.watcher(rpc).tick())
        # the pool's swaps, then who the pool paid the token to; never the curve
        self.assertEqual([c[2] for c in rpc.get_logs_calls], [chain.POOL_MANAGER, [TOKEN]])

    def test_v4_buyers_named_by_the_pools_transfer(self):
        from buybot.watcher import name_v4_buyers
        buyer = "0x" + "77" * 20
        tx = "0x" + "cd" * 32
        transfer = {"address": TOKEN, "transactionHash": tx, "data": "0x" + "00" * 32,
                    "topics": [chain.TOPIC_TRANSFER, abi.address_topic(chain.POOL_MANAGER), abi.address_topic(buyer)]}
        other = dict(transfer, topics=[chain.TOPIC_TRANSFER, abi.address_topic(buyer),
                                       abi.address_topic(chain.POOL_MANAGER)])  # a sell into the pool
        payouts = chain.pool_payouts([transfer, other])
        self.assertEqual(payouts, {(TOKEN, tx): buyer})
        t = fixture_token()
        events = [(1, 1, "buy", t, "b", {"venue": "v4", "tx": tx, "buyer": None}),
                  (1, 2, "buy", t, "c", {"venue": "curve", "tx": tx, "buyer": None})]
        name_v4_buyers(events, payouts)
        self.assertEqual([e[5]["buyer"] for e in events], [buyer, None])

    def test_phase_refresh_catches_a_missed_graduation(self):
        self.db.set_last_block(self.last_block)
        rpc = FakeRpc(self.last_block + 1, [], graduated=True)
        w = Watcher(rpc, self.db, FakeEthUsd())
        run(w.tick())
        self.assertTrue(self.db.get_token(TOKEN).graduated)

    def test_trades_are_kept_for_charts(self):
        self.db.set_last_block(self.first_block - 1)
        run(self.watcher(FakeRpc(self.last_block, self.buys)).tick())
        points = self.db.trades_since(TOKEN, 0)
        self.assertEqual(len(points), 3)
        self.assertTrue(all(price > 0 and side == 1 for _, price, _, side in points))
        # the fixture logs carry no block time: it is estimated, never zero
        self.assertTrue(all(ts > time.time() - 7 * 86400 for ts, *_ in points))

    def test_backfill_loads_history(self):
        self.db.update_chat(-1, token=None)
        rpc = FakeRpc(self.last_block, self.buys)  # no block_time: times are estimated
        hours = (self.last_block - self.first_block + 10) / 36000
        n = run(self.watcher(rpc).backfill(fixture_token(), hours=hours))
        self.assertEqual(n, 3)
        self.assertEqual(run(self.watcher(rpc).backfill(fixture_token(), hours=hours)), 3)
        self.assertEqual(len(self.db.trades_since(TOKEN, 0)), 3)  # repeats are ignored
        self.assertEqual(self.db.outbox_size(), 0)  # history is never posted

    def test_posted_trades_get_the_wallet_position(self):
        class BalanceRpc(FakeRpc):
            balance_calls = 0

            async def eth_call(self, to, data, block="latest"):
                if data.startswith(chain.SEL_BALANCE_OF):
                    BalanceRpc.balance_calls += 1
                    return "0x" + format(40_000_000 * ETH, "064x")  # 40M tokens after the buy
                return await super().eth_call(to, data, block)

        self.db.set_last_block(self.first_block - 1)
        rpc = BalanceRpc(self.last_block, self.buys)
        run(self.watcher(rpc).tick())
        rows = self.db.pending_outbox()
        first = rows[0].payload  # the launcher's 28.3M-token buy
        self.assertIn("change", first["holder"])
        self.assertGreater(first["holder"]["change"], 0)
        self.assertAlmostEqual(first["holder"]["share"], 0.04)
        self.assertEqual(BalanceRpc.balance_calls, 3)

        self.db.update_chat(-1, min_usd=10 ** 9)  # nothing will be posted: nothing looked up
        BalanceRpc.balance_calls = 0
        self.db.set_last_block(self.first_block - 1)
        run(self.watcher(BalanceRpc(self.last_block, self.buys)).tick())
        self.assertEqual(BalanceRpc.balance_calls, 0)

    def test_sells_go_only_where_turned_on(self):
        self.db.ensure_chat(-2)
        self.db.update_chat(-2, token=TOKEN, sells=1)
        start = self.last_block
        curve_sell = dict(SELL["log"], address=CURVE, blockNumber=hex(start + 1), logIndex="0x1")
        self.db.set_last_block(start)
        run(self.watcher(FakeRpc(start + 1, [curve_sell])).tick())
        rows = self.db.pending_outbox()
        self.assertEqual([(r.chat_id, r.kind) for r in rows], [(-2, "sell")])
        p = rows[0].payload
        self.assertAlmostEqual(p["quote"], int(SELL["log"]["data"][66:130], 16) / ETH)
        self.assertIsNotNone(p["progress"])

        # after graduation: a v4 buy goes to both chats, the v4 sell only to -2
        self.db.delete_outbox([r.id for r in rows])
        self.db.mark_graduated(TOKEN)
        pool = fixture_token().pool_id
        swaps = [v4_swap_log(pool, -ETH, 10 ** 24, 2 ** 96, block=start + 2),
                 v4_swap_log(pool, ETH // 2, -10 ** 24, 2 ** 96, block=start + 3)]
        run(self.watcher(FakeRpc(start + 3, swaps)).tick())
        got = sorted((r.chat_id, r.kind) for r in self.db.pending_outbox() if r.kind != "milestone")
        self.assertEqual(got, [(-2, "buy"), (-2, "sell"), (-1, "buy")])

    def test_milestones_only_on_new_highs(self):
        w = self.watcher(FakeRpc(0, []))
        t = fixture_token()

        def ev(kind, mc, progress=None):
            return (1, 1, kind, t, f"{kind}:{mc}", {"mc_usd": mc, "progress": progress, "usd": 10.0,
                                                   "quote": 0.01, "tx": "0x" + "ab" * 32})

        def levels(*events):
            return [(e[5]["what"], e[5]["level"]) for e in w._milestones(list(events))]

        self.assertEqual(levels(ev("buy", 12_000, 0.52)), [])  # the first trade only sets the base
        self.assertEqual(levels(ev("buy", 27_000, 0.6)), [("mc", 25_000)])
        self.assertEqual(levels(ev("sell", 9_000), ev("buy", 26_000)), [])  # a swing, no new high
        self.assertEqual(levels(ev("buy", 60_000, 0.91)), [("mc", 50_000), ("curve", 0.9)])
        self.assertEqual(levels(ev("sell", 120_000)), [])  # sells never post one
        self.assertEqual(levels(ev("buy", 110_000)), [])   # but the sell set the new high

    def test_nothing_tracked_just_advances(self):
        self.db.update_chat(-1, token=None)
        self.db.set_last_block(self.first_block - 1)
        rpc = FakeRpc(self.last_block, self.buys)
        run(self.watcher(rpc).tick())
        self.assertEqual(rpc.get_logs_calls, [])
        self.assertEqual(self.db.last_block(), self.last_block)


class Texts(unittest.TestCase):
    def test_every_text_has_both_languages_with_the_same_blanks(self):
        import re
        from buybot import i18n
        for key, (en, ru) in i18n.TEXTS.items():
            with self.subTest(key=key):
                self.assertTrue(en and ru)
                self.assertEqual(sorted(re.findall(r"{(\w+)}", en)), sorted(re.findall(r"{(\w+)}", ru)))
                self.assertEqual(en.count("<b>"), en.count("</b>"))
                self.assertEqual(ru.count("<b>"), ru.count("</b>"))

    def test_russian_plurals(self):
        from buybot import i18n
        forms = ("buy", "buys"), ("покупка", "покупки", "покупок")
        self.assertEqual([i18n.plural(n, "ru", *forms) for n in (1, 2, 5, 11, 21, 22, 25)],
                         ["покупка", "покупки", "покупок", "покупок", "покупка", "покупки", "покупок"])
        self.assertEqual(i18n.plural(1, "en", *forms), "buy")
        self.assertEqual(i18n.weeks(4, "ru"), "4 недели")

    def test_posts_in_russian(self):
        text = render.buy_text(render.sample_payload("TST", "ETH", 3000.0), chat(lang="ru"), sell=True)
        self.assertIn("TST Продажа!", text)
        self.assertIn("🪙 Продано", text)


class TokenDay(unittest.TestCase):
    def test_a_day_of_trades(self):
        from buybot.stats import token_day
        now = 100_000
        pts = [(now - 3000, 1.0, 2.0, 1), (now - 2000, 3.0, 0.5, -1), (now - 1000, 2.0, 4.0, 1),
               (now - 90_000, 9.0, 9.0, 1)]  # older than a day: left out
        d = token_day(pts, supply=10, now=now)
        self.assertEqual((d.buys, d.sells), (2, 1))
        self.assertEqual((d.buy_volume, d.sell_volume, d.biggest_buy), (6.0, 0.5, 4.0))
        self.assertEqual((d.mc_now, d.mc_high, d.mc_low), (20.0, 30.0, 10.0))
        self.assertAlmostEqual(d.change, 1.0)
        self.assertLess(d.hours, 1)
        self.assertIsNone(token_day([], supply=10, now=now))


class Config(unittest.TestCase):
    def test_plans_and_dotenv(self):
        self.assertEqual(_plans("1:500, 4:1500"), {1: 500, 4: 1500})
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# comment\nBUYBOT_TEST_X='quoted'\nBUYBOT_TEST_Y=2\n")
        try:
            os.environ.pop("BUYBOT_TEST_X", None)
            os.environ["BUYBOT_TEST_Y"] = "kept"
            load_dotenv(f.name)
            self.assertEqual(os.environ["BUYBOT_TEST_X"], "quoted")
            self.assertEqual(os.environ["BUYBOT_TEST_Y"], "kept")
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
