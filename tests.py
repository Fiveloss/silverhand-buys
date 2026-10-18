"""Invariant checks. Standard library only, no network: python3 tests.py

Chain decoding is checked against real Robinhood Chain mainnet logs in
fixtures/pons_v2_mainnet.json; the Telegram layer is smoke-tested separately
in tests_bot.py (needs the requirements installed).
"""

import asyncio
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from buybot import abi, chain, emoji, flap, pools, quotes, render
from buybot.config import load_dotenv
from buybot.db import Chat, Db, Token
from buybot.keccak import keccak256
from buybot.watcher import Watcher


def needs(*modules):
    """Skip a test whose module imports a requirement (aiohttp, aiogram, Pillow) that is not
    installed: this file stays standard library only. CI runs it again with the requirements."""
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    return unittest.skipIf(missing, f"needs {', '.join(missing)}")

FIX = json.loads((Path(__file__).parent / "fixtures" / "pons_v2_mainnet.json").read_text())
# One mainnet curve sell with the curve's reserves right before and after it.
SELL = json.loads((Path(__file__).parent / "fixtures" / "curve_sell_mainnet.json").read_text())
# A buy and a sell in a Uniswap v2 pair, a v3 pool and a v4 pool, with their creation
# logs and the token Transfers of the same transactions; one Chainlink stock round.
POOLS = json.loads((Path(__file__).parent / "fixtures" / "pools_mainnet.json").read_text())
# Flap.sh: a curve buy with its progress change, a sell, a graduation into a v2 pair,
# getTokenV7, and direct buys' transaction values next to the event's `eth`.
FLAP = json.loads((Path(__file__).parent / "fixtures" / "flap_mainnet.json").read_text())
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
             media_kind=None, media_file_id=None, active=True, created_at=0)
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
        self.assertIn("🟢 <b>BUY</b> · <b>$TST</b>", text)
        self.assertIn("💵 <b>$1,050</b> · 0.35 ETH", text)
        self.assertIn("├ 🪙 Got <b>2.1M</b> TST", text)
        self.assertIn("└ 🎓 Curve", text)  # the last branch closes the tree
        self.assertIn("43%", text)
        self.assertIn('href="https://x/address/0x3f92', text)
        self.assertIn(f"CA <code>{TOKEN}</code>", text)  # a tap copies the contract

    def test_unknown_numbers_are_left_out_not_zero(self):
        text = render.buy_text(self.payload(usd=None, mc_usd=None, mc_quote=None, progress=None,
                                            buyer=None), chat())
        self.assertNotRegex(text, r"\$[\d,.]")  # no dollar amount (the ticker is $TST)
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
        self.assertIn("💼 +33% · holds 0.12%", text)
        self.assertIn("🚪 Продал всё", render.buy_text(self.payload(holder={"all": True}), chat(lang="ru"),
                                                      sell=True))
        self.assertNotIn("💼", render.buy_text(self.payload(), c))

    def test_every_post_is_html_telegram_accepts(self):
        """A stray "<" (as in "<0.01%") makes Telegram refuse the whole post."""
        import re
        tag = re.compile(r"</?(b|i|u|s|a|code|pre|blockquote)\b[^<>]*>")

        def check(text):
            for m in re.finditer("<", text):
                self.assertTrue(tag.match(text, m.start()), text[m.start():m.start() + 30])

        c = chat()
        tiny = self.payload(holder={"change": 0.5, "share": 0.00001})
        check(render.buy_text(tiny, c))
        self.assertIn("holds &lt;0.01%", render.buy_text(tiny, c))
        check(render.buy_text(self.payload(holder={"all": True}), c, sell=True))
        check(render.buy_text(self.payload(), c, more=[self.payload(), self.payload()]))
        check(render.buy_text(render.sample_payload("<X>", "ETH", None, venue="v3", dex="Ramses v3"), c))
        check(render.milestone_text({"symbol": "<X>", "what": "mc", "level": 50_000}))
        check(render.graduation_text({"symbol": "<X>", "raised": 4.2, "quote_symbol": "ETH", "usd": 1.0}))
        check(render.footer())

    def test_sell_post(self):
        text = render.buy_text(self.payload(), chat(), sell=True)
        self.assertIn("🔴 <b>SELL</b> · <b>$TST</b>", text)
        self.assertIn("🔴", text)
        self.assertNotIn("🟢", text)
        self.assertIn("🪙 Sold <b>2.1M</b> TST", text)
        self.assertIn("💰 <b>$1,050</b>", text)
        self.assertIn("├ 👤 0x3f92", text)

    def test_merged_buys(self):
        items = [self.payload(quote=0.1, usd=300), self.payload(quote=2.0, usd=6000),
                 self.payload(quote=0.5, usd=1500)]
        main, more = render.pick_main(items)
        self.assertEqual(main["quote"], 2.0)
        text = render.buy_text(main, chat(), more)
        self.assertIn("2 more buys <b>$1,800</b> · 0.6 ETH", text)

    def test_footer_is_the_same_clean_line_for_every_chat(self):
        self.assertEqual(render.footer(), "\n\n<i>NFA · DYOR</i>")
        for promo in ("via", "startgroup", "start=ads", "Ad", "Advertise", "Free"):
            self.assertNotIn(promo, render.footer())

    def test_buttons(self):
        pairs = render.buttons(self.payload(), "https://gmgn.ai/robinhood/token/REF_{token}", "https://e")
        self.assertEqual(pairs[0][1], f"https://gmgn.ai/robinhood/token/REF_{TOKEN}")
        self.assertTrue(pairs[1][1].startswith("https://e/tx/0x"))


class CustomEmoji(unittest.TestCase):
    IDS = {name: str(5000 + i) for i, name in enumerate(emoji.PACK)}

    def test_the_pack_is_drawn_and_small_enough(self):
        for name in emoji.PACK:
            f = emoji.DIR / f"{name}.webm"
            self.assertTrue(f.exists(), name)
            self.assertLess(f.stat().st_size, 64 * 1024, name)

    def test_brand_swaps_the_emoji_the_pack_draws(self):
        html = "🟢 <b>BUY</b> · 👨‍💻 <b>DEV</b>\n🦄 Uniswap v3"
        out = emoji.brand(html, self.IDS)
        self.assertIn(f'<tg-emoji emoji-id="{self.IDS["buy"]}">🟢</tg-emoji>', out)
        self.assertIn(f'<tg-emoji emoji-id="{self.IDS["dev"]}">👨‍💻</tg-emoji>', out)  # whole, not 💻 alone
        self.assertIn("🦄 Uniswap v3", out)  # not in the pack: as it was
        self.assertEqual(emoji.brand(html, {}), html)
        self.assertEqual(emoji.brand(html, {"buy": "1"}).count("tg-emoji emoji-id"), 1)

    def test_a_branded_post_keeps_its_text(self):
        chat = Chat(1, "g", TOKEN, 0, "🟢", 50, None, None, True, 0, kind="supergroup")
        text = render.buy_text(Rendering.payload(Rendering()), chat, explorer="https://x")
        out = emoji.brand(text, self.IDS)
        self.assertEqual(re_strip_emoji(out), text)

    def test_only_groups_and_private_chats_get_them(self):
        def chat(kind):
            return Chat(1, "g", None, 0, "🟢", 50, None, None, True, 0, kind=kind)
        self.assertTrue(emoji.allowed(chat("supergroup")))
        self.assertTrue(emoji.allowed(chat("group")))
        self.assertFalse(emoji.allowed(chat("channel")))
        self.assertFalse(emoji.allowed(chat(None)))

    @needs("aiogram")
    def test_buttons_get_colours_and_badge_icons(self):
        from buybot.sender import keyboard
        pairs = render.buttons({"token": TOKEN, "tx": "0x" + "ab" * 32}, "https://c/{token}", "https://e")
        plain = keyboard(pairs).inline_keyboard[0]
        self.assertEqual([b.text for b in plain], ["📈 Chart", "🔍 Tx", "🕐 24h", "🛡 Scan"])
        self.assertEqual([b.style for b in plain], ["success", None, "primary", None])
        self.assertTrue(all(b.icon_custom_emoji_id is None for b in plain))
        icons = keyboard(pairs, self.IDS).inline_keyboard[0]
        self.assertEqual([b.text for b in icons], ["Chart", "Tx", "24h", "🛡 Scan"])
        self.assertEqual(icons[3].callback_data, f"x:{TOKEN}")
        self.assertEqual(icons[0].icon_custom_emoji_id, self.IDS["chart"])
        self.assertEqual(icons[2].callback_data, f"s:{TOKEN}")

    def test_ids_come_from_the_meta_table(self):
        class Meta:
            raw = None

            def get_meta(self, key):
                return self.raw
        m = Meta()
        self.assertEqual(emoji.load(m), {})
        m.raw = json.dumps({"buy": 7, "nope": 8, "sell": ""})
        self.assertEqual(emoji.load(m), {"buy": "7"})
        m.raw = "{broken"
        self.assertEqual(emoji.load(m), {})


def re_strip_emoji(html: str) -> str:
    import re
    return re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', r"\1", html)


@needs("aiohttp")
class RpcBatch(unittest.TestCase):
    def test_a_batch_is_one_request_and_errors_stay_per_item(self):
        from buybot.rpc import Rpc, RpcError

        class Resp:
            status = 200
            headers = {}

            def __init__(self, body):
                self.body = body

            async def text(self):
                return self.body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Session:
            posts = []

            def post(self, url, json=None, timeout=None):
                Session.posts.append(json)
                ans = [{"jsonrpc": "2.0", "id": json[1]["id"], "error": {"code": -32000, "message": "boom"}},
                       {"jsonrpc": "2.0", "id": json[0]["id"], "result": "0x10"}]
                return Resp(__import__("json").dumps(ans))

        rpc = Rpc("https://x", Session(), min_interval=0)
        out = run(rpc.batch([("eth_blockNumber", []), ("eth_chainId", [])]))
        self.assertEqual(len(Session.posts), 1)
        self.assertEqual(out[0], "0x10")  # matched by id, not by order
        self.assertIsInstance(out[1], RpcError)

    def test_a_cloudflare_challenge_pauses_instead_of_retrying(self):
        from buybot import rpc as rpc_mod

        class Resp:
            status = 403
            headers = {}

            async def text(self):
                return "<html><title>Just a moment...</title></html>"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Session:
            n = 0

            def post(self, *a, **k):
                Session.n += 1
                return Resp()

        rpc = rpc_mod.Rpc("https://x", Session(), min_interval=0)
        with self.assertRaises(rpc_mod.RpcError):
            run(rpc.call("eth_blockNumber", []))
        self.assertEqual(Session.n, 1)
        self.assertEqual(rpc.challenges, 1)
        self.assertGreater(rpc._next_at - time.monotonic(), rpc_mod.CHALLENGE_PAUSE - 5)


class Memory(unittest.TestCase):
    """The bot runs under a 300 MB cap: cards are drawn one at a time and keep no 2x canvas."""

    @needs("aiogram", "PIL")
    def test_cards_are_drawn_one_at_a_time(self):
        from buybot import card, sender
        db = Db(":memory:")
        db.upsert_token(fixture_token())
        inside, most = [0], [0]
        real = card.render_still

        def slow(spec):
            inside[0] += 1
            most[0] = max(most[0], inside[0])
            time.sleep(0.05)
            inside[0] -= 1
            return b"png"
        card.render_still = slow
        try:
            p = {"token": TOKEN, "usd": 10.0, "quote": 0.01, "price": 1e-9}

            async def three():
                return await asyncio.gather(*[sender.make_chart(db, p, 1, mode=sender.CHART_STILL)
                                              for _ in range(3)])
            self.assertEqual(run(three()), [(b"png", "photo")] * 3)
        finally:
            card.render_still = real
        self.assertEqual(most[0], 1)

    @needs("PIL")
    def test_a_card_keeps_only_its_final_size_layers(self):
        from buybot import card
        now = 1_760_000_000
        pts = [[now - 3600 + i * 60, 1e-7 * (1 + i / 100), 0.1, 1] for i in range(60)]
        c = card.Card({"side": 1, "symbol": "T", "points": pts, "supply": 1e9, "usd_per_quote": 3000.0,
                       "now": now, "progress": 0.5}, size=card.SIZE, fps=30)
        self.assertEqual(c.c2.img.size, (1, 1))  # measures text at 2x, holds no 2x canvas
        self.assertEqual((c.base.size, c.full.size), (card.SIZE, card.SIZE))

    def test_factories_are_asked_one_by_one_from_their_first_pool(self):
        calls = []

        class Rpc:
            async def get_logs(self, frm, to, address=None, topics=None):
                calls.append((frm, address))
                return []
        run(pools._logs_both_ways(Rpc(), 90_000_000, list(pools.V3_FACTORIES), pools.TOPIC_V3_POOL_CREATED,
                                  TOKEN, None))
        self.assertTrue(all(isinstance(a, str) for _, a in calls))  # a list would cut the RPC's span to 100k
        self.assertEqual(sorted(set(calls)), sorted({(pools.FACTORY_FROM.get(a, 0), a) for a in pools.V3_FACTORIES}))
        self.assertEqual(len(calls), 2 * len(pools.V3_FACTORIES))


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

    def test_the_paid_plan_leaves_a_database_from_before_it(self):
        """A 0.3 database: empty ads and payments tables go, chats.pro_until stays (older
        code reads it), cached animations of the removed menu screens go. A self-hosted
        copy whose payments table holds rows keeps it."""
        import sqlite3
        path = os.path.join(self.dir.name, "old.db")
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE ads (id INTEGER PRIMARY KEY, text TEXT);"
                          "CREATE TABLE payments (charge_id TEXT PRIMARY KEY, amount INTEGER);"
                          "INSERT INTO payments VALUES ('c1', 500);"
                          "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                          "INSERT INTO meta VALUES ('anim:en:pro:1234', 'f1'), ('anim:en:ads:99', 'f2'),"
                          " ('anim:en:start:77', 'f3');")
        con.commit()
        con.close()
        db = Db(path)
        tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("ads", tables)
        self.assertIn("payments", tables)  # it had a row
        self.assertEqual(db.get_meta("anim:en:start:77"), "f3")
        self.assertIsNone(db.get_meta("anim:en:pro:1234"))
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(chats)")}
        self.assertIn("pro_until", cols)
        db.ensure_chat(-5)
        self.assertEqual(db.get_chat(-5).chat_id, -5)
        self.assertNotIn("stars_total", db.stats())
        db.close()

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
            self.db.update_chat(-1, bogus=1)


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
        # the curve's trades, and only the Transfers its creator sent (for a dev sell)
        self.assertEqual(len(rpc.get_logs_calls), 2)
        self.assertEqual(rpc.get_logs_calls[1][2:],
                         ([TOKEN], [chain.TOPIC_TRANSFER, [abi.address_topic(DEPLOYER)]]))

    def test_graduated_tokens_skip_the_curve_query(self):
        self.db.mark_graduated(TOKEN)
        self.db.set_last_block(self.first_block - 1)
        rpc = FakeRpc(self.last_block, self.buys)
        run(self.watcher(rpc).tick())
        # the pool's swaps, then who the pool paid the token to; never the curve
        self.assertEqual([c[2] for c in rpc.get_logs_calls], [chain.POOL_MANAGER, [TOKEN]])

    def test_v4_buyers_named_by_the_pools_transfer(self):
        from buybot.watcher import name_buyers
        buyer = "0x" + "77" * 20
        tx = "0x" + "cd" * 32
        transfer = {"address": TOKEN, "transactionHash": tx, "data": "0x" + format(5, "064x"),
                    "blockNumber": "0x1", "logIndex": "0x1",
                    "topics": [chain.TOPIC_TRANSFER, abi.address_topic(chain.POOL_MANAGER), abi.address_topic(buyer)]}
        other = dict(transfer, logIndex="0x0", topics=[chain.TOPIC_TRANSFER, abi.address_topic(buyer),
                                                       abi.address_topic(chain.POOL_MANAGER)])  # a sell into the pool
        payouts = pools.payouts([transfer, other], {chain.POOL_MANAGER})
        self.assertEqual(payouts, {(TOKEN, tx): buyer})
        t = fixture_token()
        events = [(1, 1, "buy", t, "b", {"venue": "v4", "tx": tx, "buyer": None}),
                  (1, 2, "buy", t, "c", {"venue": "curve", "tx": tx, "buyer": None})]
        name_buyers(events, payouts)
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

    def test_backfills_run_one_at_a_time(self):
        self.db.update_chat(-1, token=None)
        hours = (self.last_block - self.first_block + 10) / 36000
        inside, most = [0], [0]

        class SlowRpc(FakeRpc):
            async def get_logs(self, *a, **k):
                inside[0] += 1
                most[0] = max(most[0], inside[0])
                await asyncio.sleep(0.01)
                inside[0] -= 1
                return await super().get_logs(*a, **k)

        async def both():
            w = self.watcher(SlowRpc(self.last_block, self.buys))
            return await asyncio.gather(w.backfill(fixture_token(), hours=hours),
                                        w.backfill(fixture_token(), hours=hours))
        self.assertEqual(run(both()), [3, 3])
        self.assertEqual(most[0], 1)

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


class PoolFacts(unittest.TestCase):
    """Uniswap v2/v3/v4 decoding against real mainnet logs (fixtures/pools_mainnet.json)."""

    def test_topics_and_selectors_match_their_signatures(self):
        for topic, sig in [(pools.TOPIC_V3_SWAP, "Swap(address,address,int256,int256,uint160,uint128,int24)"),
                           (pools.TOPIC_V2_SWAP, "Swap(address,uint256,uint256,uint256,uint256,address)"),
                           (pools.TOPIC_V2_SYNC, "Sync(uint112,uint112)"),
                           (pools.TOPIC_V3_POOL_CREATED, "PoolCreated(address,address,uint24,int24,address)"),
                           (pools.TOPIC_V2_PAIR_CREATED, "PairCreated(address,address,address,uint256)"),
                           (pools.TOPIC_V4_INITIALIZE,
                            "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")]:
            self.assertEqual(topic, abi.event_topic(sig))
        self.assertEqual(pools.SEL_EXTSLOAD, abi.selector("extsload(bytes32)"))
        self.assertEqual(quotes.SEL_LATEST_ROUND, abi.selector("latestRoundData()"))

    def transferred(self, venue, side):
        """What moved of the token in the trade's transaction, from/to the pool."""
        f = POOLS[venue]
        pool = {"v2": f.get("pair"), "v3": f.get("pool"), "v4": chain.POOL_MANAGER}[venue]
        tx = f[side]["transactionHash"]
        for lg in f["transfers"]:
            frm, to = abi.topic_address(lg["topics"][1]), abi.topic_address(lg["topics"][2])
            if lg["transactionHash"] == tx and pool in (frm, to):
                return int(lg["data"], 16)
        return None

    def test_v3_buy_and_sell_match_the_token_transfers(self):
        f = POOLS["v3"]
        for side in ("buy", "sell"):
            swap = pools.decode_v3_swap(f[side])
            kind, quote, tokens = pools.v3_trade(swap, f["token"], f["quote"])
            self.assertEqual(kind, side)
            self.assertEqual(tokens, self.transferred("v3", side))
            self.assertGreater(quote, 0)
        # the pool's creation log names it, from Uniswap's own factory
        created = f["created"]
        self.assertEqual(created["address"], f["factory"])
        self.assertIn(f["factory"], pools.V3_FACTORIES)
        self.assertEqual(abi.word_address(abi.to_bytes(created["data"]), 1), f["pool"])

    def test_v3_price_after_the_trade_sits_near_the_trade(self):
        f = POOLS["v3"]
        swap = pools.decode_v3_swap(f["buy"])
        _, quote, tokens = pools.v3_trade(swap, f["token"], f["quote"])
        spot = chain.v4_token_price(swap.sqrt_price_x96, f["token"], f["quote"], 18, 18)
        avg = chain.trade_price(quote, tokens, 18, 18)
        self.assertTrue(0.5 < spot / avg < 2, (spot, avg))  # a buy leaves the price above ~its average

    def test_v2_buy_and_sell_with_their_syncs(self):
        f = POOLS["v2"]
        syncs = pools.v2_syncs(f["syncs"])
        for side in ("buy", "sell"):
            swap = pools.decode_v2_swap(f[side])
            kind, quote, tokens = pools.v2_trade(swap, f["token"], f["quote"])
            self.assertEqual(kind, side)
            self.assertEqual(tokens, self.transferred("v2", side))
            reserves = pools.sync_before(syncs, swap)
            self.assertIsNotNone(reserves)
            spot = pools.v2_price(*reserves, f["token"], f["quote"], 18, 18)
            avg = chain.trade_price(quote, tokens, 18, 18)
            self.assertTrue(0.8 < spot / avg < 1.25, (side, spot, avg))
        self.assertIn(POOLS["v2"]["created"]["address"], pools.V2_FACTORIES)

    def test_v4_pool_key_and_trades(self):
        f = POOLS["v4"]
        data = abi.to_bytes(f["initialize"]["data"])
        fee, spacing, hooks = abi.word_uint(data, 0), abi.word_int(data, 1), abi.word_address(data, 2)
        self.assertEqual(chain.pool_id(f["token"], f["quote"], fee, spacing, hooks), f["pool_id"])
        buy = chain.v4_buy_amounts(chain.decode_v4_swap(f["buy"]), f["token"], f["quote"])
        sell = chain.v4_sell_amounts(chain.decode_v4_swap(f["sell"]), f["token"], f["quote"])
        self.assertEqual(buy[1], self.transferred("v4", "buy"))
        self.assertEqual(sell[1], self.transferred("v4", "sell"))

    def test_the_wallet_is_where_the_tokens_stop(self):
        # mainnet: PoolManager -> a trading bot's contract -> two more hops -> the wallet
        f = POOLS["v4"]
        got = pools.payouts(f["transfers"], {chain.POOL_MANAGER})
        tx = f["buy"]["transactionHash"]
        chain_of = [abi.topic_address(lg["topics"][2]) for lg in f["transfers"] if lg["transactionHash"] == tx]
        self.assertGreater(len(chain_of), 1)
        self.assertEqual(got[(f["token"], tx)], chain_of[-1])
        # a v3 buy paid straight to the wallet; a sell names nobody
        v3 = POOLS["v3"]
        got = pools.payouts(v3["transfers"], {v3["pool"]})
        self.assertEqual(list(got), [(v3["token"], v3["buy"]["transactionHash"])])

    def test_a_router_at_the_end_is_not_a_wallet(self):
        router = next(iter(pools.ROUTERS - {chain.POOL_MANAGER}))
        lg = {"address": TOKEN, "transactionHash": "0x" + "ab" * 32, "blockNumber": "0x1", "logIndex": "0x1",
              "data": "0x" + format(10, "064x"),
              "topics": [chain.TOPIC_TRANSFER, abi.address_topic(chain.POOL_MANAGER), abi.address_topic(router)]}
        self.assertEqual(pools.payouts([lg], {chain.POOL_MANAGER}), {})

    def test_v4_depth_of_the_quote(self):
        token, weth = "0x" + "ff" * 20, quotes.WETH  # WETH sorts first: currency0
        # at price 1 (sqrtP = 2^96) a pool with liquidity L holds L of each side around the price
        self.assertEqual(pools.v4_quote_depth(2 ** 96, 10 ** 18, token, weth), 10 ** 18)
        # a price of 4 quote per token (sqrtP = 2 * 2^96 with the token as currency1):
        # the quote (currency0) side is L / sqrtP
        self.assertEqual(pools.v4_quote_depth(2 * 2 ** 96, 10 ** 18, token, weth), 10 ** 18 // 2)
        self.assertEqual(pools.v4_quote_depth(0, 10 ** 18, token, weth), 0)

    def test_pools_worth_watching(self):
        c = [pools.Candidate("v3", f"0x{i}", quotes.WETH, "Uniswap v3") for i in range(9)]
        picked = pools.choose([(c[0], 500_000), (c[1], 20_000), (c[2], 9_000), (c[3], 800), (c[4], 0)])
        self.assertEqual([p[0] for p in picked], [c[0], c[1]])  # 9K is under 2% of 500K; 800 under $1K
        self.assertEqual([p[0] for p in pools.choose([(c[0], 400), (c[1], 300)])], [c[0]])  # a new launch
        self.assertEqual(pools.choose([(c[0], 60)]), [])  # dust
        many = pools.choose([(c[i], 100_000 - i) for i in range(9)])
        self.assertEqual(len(many), pools.MAX_POOLS)
        self.assertEqual(pools.choose([]), [])

    def test_only_the_busiest_pools_are_read(self):
        cands = [pools.Candidate("v4", "0x%064x" % i, quotes.USDG, "Uniswap v4", block=i) for i in range(30)]

        class Rpc:
            windows = []

            async def get_logs(self, frm, to, address=None, topics=None):
                Rpc.windows.append(to - frm)
                if to - frm < 100_000:
                    return []  # a quiet last hour
                return [{"topics": [chain.TOPIC_V4_SWAP, cands[i].id]} for i in (5, 5, 5, 7, 9, 9)]

        got = run(pools.worth_reading(Rpc(), cands, 10 ** 6))
        self.assertEqual([c.block for c in got], [5, 9, 7])  # busiest of the day first
        self.assertEqual(Rpc.windows, [36_000, 864_000])
        self.assertEqual(len(run(pools.worth_reading(Rpc(), cands[:12], 10 ** 6))), 12)  # few: all

        class Quiet(Rpc):
            async def get_logs(self, frm, to, address=None, topics=None):
                return []

        self.assertEqual([c.block for c in run(pools.worth_reading(Quiet(), cands, 10 ** 6))][:2], [29, 28])

    def test_chainlink_round(self):
        f = POOLS["feed"]
        price, updated = quotes.decode_round(f["latest_round_return"])
        self.assertTrue(10 < price < 10_000)
        self.assertGreater(updated, 1_700_000_000)
        self.assertEqual(quotes.QUOTES[f["token"]][2], f"feed:{f['feed']}")
        self.assertIsNone(quotes.decode_round("0x" + "00" * 160))

    def test_only_listed_quotes_get_dollars(self):
        self.assertEqual(quotes.quote_info(quotes.WETH.upper().replace("0X", "0x")), ("WETH", 18, "eth"))
        self.assertIsNone(quotes.quote_info("0x3ae0689f64b8a7683d06a9e358d7346dc5e71e18"))  # a fake "USDC"
        for addr, (sym, feed) in quotes.STOCKS.items():
            self.assertTrue(abi.is_address(addr) and abi.is_address(feed) and addr == addr.lower(), sym)


class FeedRates(unittest.TestCase):
    def test_rates_by_kind_and_staleness(self):
        feed = "0x" + "fe" * 20
        answer = {"updated": int(time.time()) - 3600}

        class Rpc:
            calls = 0

            async def eth_call(self, to, data, block="latest"):
                Rpc.calls += 1
                w = lambda v: format(v % (1 << 256), "064x")  # noqa: E731
                return "0x" + w(7) + w(22_557_000_000) + w(0) + w(answer["updated"]) + w(7)

        rates = quotes.Rates(Rpc(), FakeEthUsd())
        got = run(rates.table(["usd", "eth", f"feed:{feed}", "other"]))
        self.assertEqual(got, {"usd": 1.0, "eth": 3000.0, f"feed:{feed}": 225.57, "other": None})
        run(rates.get(f"feed:{feed}"))
        self.assertEqual(Rpc.calls, 1)  # cached
        # a price older than four days (a feed that stopped) is not used
        answer["updated"] = int(time.time()) - 5 * 86400
        rates = quotes.Rates(Rpc(), FakeEthUsd())
        self.assertIsNone(run(rates.get(f"feed:{feed}")))
        self.assertEqual(quotes.usd_value(2.0, "usd", {"usd": 1.0}), 2.0)
        self.assertIsNone(quotes.usd_value(2.0, "other", {"other": None}))


class DexTokens(unittest.TestCase):
    """A token outside Pons, followed through its pools (the real v3 pool of the fixture,
    and a made-up USDG v4 pool of the same token)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Db(os.path.join(self.dir.name, "t.db"))
        f = POOLS["v3"]
        self.f = f
        self.token = Token(address=f["token"], curve="", deployer="0x" + "de" * 20, pair=f["quote"],
                           quote_symbol="WETH", quote_decimals=18, quote_kind="eth", name="Dex Token",
                           symbol="DEX", decimals=18, total_supply=10 ** 27, threshold=0, pool_id="",
                           curve_k=None, phantom=None, graduated=True, added_at=0, kind="dex")
        self.db.upsert_token(self.token)
        self.v3 = pools.Pool(f["token"], "v3", f["pool"], f["quote"], "WETH", 18, "eth", "Uniswap v3", 50_000)
        self.usdg_pool = "0x" + "a1" * 32
        self.v4 = pools.Pool(f["token"], "v4", self.usdg_pool, quotes.USDG, "USDG", 6, "usd", "Uniswap v4", 9_000)
        self.db.set_pools(f["token"], [self.v3, self.v4])
        self.db.ensure_chat(-1)
        self.db.update_chat(-1, token=f["token"], sells=1)
        self.buy_block = int(f["buy"]["blockNumber"], 16)
        self.sell_block = int(f["sell"]["blockNumber"], 16)

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def watcher(self, rpc):
        w = Watcher(rpc, self.db, FakeEthUsd())
        w._phase_checked = time.time()
        return w

    def test_a_dex_token_round_trip_in_the_database(self):
        t = self.db.get_token(self.f["token"])
        self.assertEqual((t.kind, t.graduated, t.curve), ("dex", True, ""))
        self.assertEqual([p.id for p in self.db.pools_of(t.address)], [self.f["pool"], self.usdg_pool])
        self.assertEqual(len(self.db.tracked_pools()), 2)
        self.db.update_chat(-1, token=None)
        self.assertEqual(self.db.tracked_pools(), [])

    def test_v3_trades_reach_the_outbox_with_the_wallet(self):
        f = self.f
        lo, hi = sorted((self.buy_block, self.sell_block))
        self.db.set_last_block(lo - 1)
        rpc = FakeRpc(hi, [f["buy"], f["sell"]] + f["transfers"])
        run(self.watcher(rpc).tick())
        rows = {r.kind: r.payload for r in self.db.pending_outbox() if r.kind in ("buy", "sell")}
        self.assertEqual(set(rows), {"buy", "sell"})
        buy = rows["buy"]
        self.assertEqual((buy["venue"], buy["dex"], buy["quote_symbol"]), ("v3", "Uniswap v3", "WETH"))
        wallet = abi.topic_address(next(lg for lg in f["transfers"]
                                        if lg["transactionHash"] == f["buy"]["transactionHash"])["topics"][2])
        self.assertEqual(buy["buyer"], wallet)
        self.assertAlmostEqual(buy["usd"], buy["quote"] * 3000.0)
        self.assertAlmostEqual(buy["mc_usd"], buy["price"] * 1e9 * 3000.0)
        # kept for the chart in the main quote
        self.assertEqual(len(self.db.trades_since(f["token"], 0)), 2)
        # queries: v4 swaps of the USDG pool, v3 swaps, then the pools' Transfers of the token
        self.assertEqual([c[2] for c in rpc.get_logs_calls],
                         [chain.POOL_MANAGER, [f["pool"]], [f["token"]]])

    def test_a_trade_in_another_quote_is_converted_for_the_chart(self):
        # 3,000 USDG for 1M tokens in the USDG pool while the main quote is WETH at $3,000
        f = self.f
        raw = 3000 / 1e6 * 10 ** 6 / 10 ** 18  # USDG per token, in base units
        token_first = int(f["token"], 16) < int(quotes.USDG, 16)
        # sqrtPriceX96 is sqrt(currency1 per currency0)
        sqrt_p = int((raw if token_first else 1 / raw) ** 0.5 * 2 ** 96)
        a_token, a_quote = 10 ** 24, -3000 * 10 ** 6
        amounts = (a_token, a_quote) if token_first else (a_quote, a_token)
        swap = v4_swap_log(self.usdg_pool, *amounts, sqrt_p, block=self.buy_block)
        self.db.set_last_block(self.buy_block - 1)
        run(self.watcher(FakeRpc(self.buy_block, [swap])).tick())
        p = next(r.payload for r in self.db.pending_outbox() if r.kind == "buy")
        self.assertEqual((p["quote_symbol"], p["dex"]), ("USDG", "Uniswap v4"))
        self.assertAlmostEqual(p["usd"], 3000.0)
        self.assertAlmostEqual(p["quote_main"], 1.0)      # 3,000 USDG = 1 WETH
        self.assertAlmostEqual(p["price"], 1e-6, places=9)  # WETH per token
        self.assertAlmostEqual(p["mc_usd"], 3_000_000, delta=1)
        self.assertEqual(p["mc_symbol"], "WETH")
        ts, price_main, quote_main, side = self.db.trades_since(f["token"], 0)[0]
        self.assertAlmostEqual(quote_main, 1.0)

    def test_the_post_says_where(self):
        p = render.sample_payload("DEX", "WETH", 3000.0, venue="v3", dex="Ramses v3")
        text = render.buy_text(p, chat())
        self.assertIn("💱 Ramses v3", text)
        self.assertNotIn("Curve", text)
        text = render.buy_text(render.sample_payload("DEX", "USDG", 1.0, venue="v2", dex="Uniswap v2"), chat())
        self.assertIn("🦄 Uniswap v2", text)
        self.assertIn("$1,050</b> · 1,050 USDG", text)

    def test_merged_trades_in_different_quotes_add_up_in_dollars(self):
        main = render.sample_payload("DEX", "WETH", 3000.0, venue="v3")
        other = dict(main, quote_symbol="USDG", quote=500.0, usd=500.0)
        text = render.buy_text(main, chat(), more=[other])
        self.assertIn("1 more buy <b>$500</b>", text)
        self.assertNotIn("WETH ($", text.split("more")[1])

    def test_pools_are_looked_for_again_every_hour(self):
        class PoolRpc(FakeRpc):
            finds = 0

        async def fake_find(rpc, rates, token, head):
            PoolRpc.finds += 1
            return [pools.Pool(token, "v2", "0x" + "b2" * 20, quotes.USDG, "USDG", 6, "usd", "Uniswap v2", 90_000)]

        from buybot import watcher as wmod
        real = wmod.amm.find_pools
        wmod.amm.find_pools = fake_find
        try:
            w = self.watcher(PoolRpc(10, []))
            tokens = self.db.tracked_tokens()
            run(w.refresh_pools(tokens, 10))
            self.assertEqual(PoolRpc.finds, 0)  # just seen: not yet
            w._pools_checked[self.f["token"]] -= 2 * 3600
            self.db.add_trades([(self.f["token"], "x:1", 1, 1.0, 1.0, 1, None)])
            run(w.refresh_pools(tokens, 10))
            self.assertEqual(PoolRpc.finds, 1)
            t = self.db.get_token(self.f["token"])
            self.assertEqual((t.pair, t.quote_symbol, t.quote_kind), (quotes.USDG, "USDG", "usd"))
            self.assertEqual(self.db.trades_since(self.f["token"], 0), [])  # old units dropped
            self.assertEqual([p.venue for p in self.db.pools_of(t.address)], ["v2"])
        finally:
            wmod.amm.find_pools = real

    def test_quotes_that_became_known_get_their_kind(self):
        nvda = "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"
        t = fixture_token(pair=nvda, quote_symbol="NVDA", quote_kind="other")
        self.db.upsert_token(t)
        self.assertEqual(self.db.reprice_quotes(quotes.QUOTES), 1)
        self.assertTrue(self.db.get_token(TOKEN).quote_kind.startswith("feed:"))


class FlapLaunches(unittest.TestCase):
    def test_topics_and_selector(self):
        for topic, sig in [(flap.TOPIC_BOUGHT, "TokenBought(uint256,address,address,uint256,uint256,uint256,uint256)"),
                           (flap.TOPIC_SOLD, "TokenSold(uint256,address,address,uint256,uint256,uint256,uint256)"),
                           (flap.TOPIC_PROGRESS, "FlapTokenProgressChanged(address,uint256)"),
                           (flap.TOPIC_LAUNCHED, "LaunchedToDEX(address,address,uint256,uint256)")]:
            self.assertEqual(topic, abi.event_topic(sig))
        self.assertEqual(flap.SEL_GET_TOKEN_V7, abi.selector("getTokenV7(address)"))

    def test_eth_is_what_the_buyer_paid(self):
        # mainnet: for buys sent straight to the Portal, `eth` equals the transaction's value
        for d in FLAP["direct_buys"]:
            self.assertEqual(d["eth"], d["tx_value"])
            self.assertLess(d["fee"], d["eth"])

    def test_trades_progress_state_and_graduation_from_mainnet(self):
        buy = flap.decode_trade(FLAP["buy"])
        self.assertEqual((buy.kind, buy.token), ("buy", FLAP["token"]))
        sell = flap.decode_trade(FLAP["sell"])
        self.assertEqual(sell.kind, "sell")
        progress = [flap.decode_progress(lg) for lg in FLAP["buy_tx_logs"]]
        self.assertIn((buy.token, buy.tx), [(p[0], p[1]) for p in progress if p])
        state = flap.decode_state(FLAP["get_token_v7_return"])
        self.assertEqual(state.status, flap.STATUS_CURVE)
        self.assertAlmostEqual(state.price, buy.price)  # the last trade's postPrice is the state's price
        g = flap.decode_launched(FLAP["launched"]["log"])
        self.assertEqual((g[0], g[1]), (FLAP["launched"]["token"], FLAP["launched"]["pair"]))
        self.assertIn(FLAP["launched"]["factory"], pools.V2_FACTORIES)
        self.assertEqual(FLAP["launched"]["status_after"], flap.STATUS_DEX)
        self.assertIsNone(flap.decode_trade(dict(FLAP["buy"], address="0x" + "11" * 20)))  # not the Portal

    def test_the_quote_is_read_from_the_state(self):
        # mainnet has Flap tokens quoted in stock tokens (GME), though Flap's docs say ETH only
        self.assertEqual(flap.decode_state(FLAP["get_token_v7_return"]).quote, abi.ZERO_ADDRESS)
        gme = next(a for a, (sym, _) in quotes.STOCKS.items() if sym == "GME")
        ret = FLAP["get_token_v7_return"]
        words = [ret[2 + 64 * i:2 + 64 * (i + 1)] for i in range(17)]
        words[9] = abi.encode_address(gme)
        st = flap.decode_state("0x" + "".join(words))
        self.assertEqual(st.quote, gme)
        self.assertTrue(quotes.quote_info(st.quote)[2].startswith("feed:"))

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Db(os.path.join(self.dir.name, "t.db"))
        self.token = Token(address=FLAP["token"], curve="", deployer="0x" + "de" * 20, pair=chain.NATIVE,
                           quote_symbol="ETH", quote_decimals=18, quote_kind="eth", name="Flap Token",
                           symbol="FLP", decimals=18, total_supply=10 ** 27, threshold=0, pool_id="",
                           curve_k=None, phantom=None, graduated=False, added_at=0, kind="flap")
        self.db.upsert_token(self.token)
        self.db.ensure_chat(-1)
        self.db.update_chat(-1, token=FLAP["token"], sells=1)

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def tick(self, logs, frm, to):
        self.db.set_last_block(frm - 1)
        w = Watcher(FakeRpc(to, logs), self.db, FakeEthUsd())
        w._phase_checked = time.time()
        run(w.tick())
        return w

    def test_curve_trades_are_posted_with_progress_and_the_real_wallet(self):
        logs = FLAP["buy_tx_logs"] + FLAP["sell_tx_logs"]
        buy = flap.decode_trade(FLAP["buy"])
        wallet = "0x" + "77" * 20  # the bot contract named by the event passes the tokens on
        hop = {"address": buy.token, "transactionHash": buy.tx, "blockNumber": hex(buy.block),
               "logIndex": hex(buy.log_index + 1), "data": "0x" + format(buy.tokens, "064x"),
               "topics": [chain.TOPIC_TRANSFER, abi.address_topic(flap.PORTAL), abi.address_topic(buy.trader)]}
        hop2 = dict(hop, logIndex=hex(buy.log_index + 2),
                    topics=[chain.TOPIC_TRANSFER, abi.address_topic(buy.trader), abi.address_topic(wallet)])
        blocks = [int(lg["blockNumber"], 16) for lg in logs]
        self.tick(logs + [hop, hop2], min(blocks), max(blocks))
        rows = {r.kind: r.payload for r in self.db.pending_outbox() if r.kind in ("buy", "sell")}
        self.assertEqual(set(rows), {"buy", "sell"})
        b = rows["buy"]
        self.assertEqual(b["buyer"], wallet)
        self.assertAlmostEqual(b["quote"], buy.eth / 1e18)
        self.assertAlmostEqual(b["usd"], buy.eth / 1e18 * 3000)
        self.assertAlmostEqual(b["mc_quote"], buy.price * 1e9)
        self.assertIsNotNone(b["progress"])
        self.assertIn("Curve", render.buy_text(b, chat()))
        self.assertEqual(len(self.db.trades_since(FLAP["token"], 0)), 2)

    def test_graduation_moves_the_token_to_its_pair(self):
        pair = "0x" + "a2" * 20
        data = abi.encode_address(FLAP["token"]) + abi.encode_address(pair) + format(2 * 10 ** 26, "064x") \
            + format(5 * 10 ** 18, "064x")
        grad = {"address": flap.PORTAL, "topics": [flap.TOPIC_LAUNCHED], "data": "0x" + data,
                "blockNumber": hex(500), "transactionHash": "0x" + "9a" * 32, "logIndex": "0x3"}
        self.tick([grad], 500, 500)
        rows = [r for r in self.db.pending_outbox() if r.kind == "grad"]
        self.assertEqual(len(rows), 1)
        text = render.graduation_text(rows[0].payload)
        self.assertIn("Uniswap v2", text)
        self.assertIn("5 ETH", text)
        t = self.db.get_token(FLAP["token"])
        self.assertEqual((t.kind, t.graduated, t.quote_symbol), ("dex", True, "WETH"))
        self.assertEqual([(p.venue, p.id) for p in self.db.pools_of(t.address)], [("v2", pair)])


def transfer_log(token: str, frm: str, to: str, value: int, block: int, tx: str, index: int = 0) -> dict:
    return {"address": token, "topics": [chain.TOPIC_TRANSFER, abi.address_topic(frm), abi.address_topic(to)],
            "data": "0x" + format(value, "064x"), "blockNumber": hex(block), "transactionHash": tx,
            "logIndex": hex(index)}


class DevSells(unittest.TestCase):
    """🚨 DEV SELL: a sale by the token's creator, told by the creator's own Transfer."""
    ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Db(os.path.join(self.dir.name, "t.db"))
        self.db.upsert_token(fixture_token())
        self.db.ensure_chat(-1)
        self.db.update_chat(-1, token=TOKEN, min_usd=10_000)  # sells off (the default), a high minimum
        self.block = 1000

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def tick(self, logs):
        self.db.set_last_block(self.block - 1)
        w = Watcher(FakeRpc(self.block, logs), self.db, FakeEthUsd())
        w._phase_checked = time.time()
        run(w.tick())
        return self.db.pending_outbox()

    def curve_sell(self, tx: str) -> dict:
        return dict(SELL["log"], address=CURVE, blockNumber=hex(self.block), transactionHash=tx)

    def test_a_dev_sell_through_a_router_is_posted_with_sells_off(self):
        dev_tx, other_tx = "0x" + "d1" * 32, "0x" + "d2" * 32
        rows = self.tick([self.curve_sell(dev_tx), self.curve_sell(other_tx),
                          transfer_log(TOKEN, DEPLOYER, self.ROUTER, 10 ** 24, self.block, dev_tx)])
        self.assertEqual(len(rows), 1)  # the other sell: sells are off
        p = rows[0].payload
        self.assertEqual((rows[0].kind, p["tx"], p["buyer"]), ("sell", dev_tx, DEPLOYER))
        self.assertTrue(p["dev"] and p["dev_sell"])
        self.assertIsNone(p.get("holder"))  # the balance was not read: no share is made up

    def test_a_dev_sell_in_the_pool(self):
        self.db.mark_graduated(TOKEN)
        tx = "0x" + "cd" * 32  # v4_swap_log's
        swap = v4_swap_log(fixture_token().pool_id, ETH // 10, -10 ** 24, 1000 * 2 ** 96, block=self.block)
        rows = self.tick([swap, transfer_log(TOKEN, DEPLOYER, chain.POOL_MANAGER, 10 ** 24, self.block, tx)])
        self.assertEqual([(r.kind, r.payload["dev_sell"], r.payload["venue"]) for r in rows], [("sell", True, "v4")])

    def test_only_the_creator_and_never_a_router(self):
        from buybot.watcher import dev_moves, mark_dev_sells
        tx = "0x" + "aa" * 32
        logs = [transfer_log(TOKEN, DEPLOYER, chain.POOL_MANAGER, 5, 1, tx)]
        self.assertEqual(dev_moves(logs, {TOKEN: DEPLOYER}), {(TOKEN, tx)})
        self.assertEqual(dev_moves(logs, {TOKEN: "0x" + "99" * 20}), set())
        router_made = [transfer_log(TOKEN, self.ROUTER, chain.POOL_MANAGER, 5, 1, tx)]
        self.assertEqual(dev_moves(router_made, {TOKEN: self.ROUTER}), set())
        t = fixture_token()
        events = [(1, 1, "sell", t, "s", {"tx": tx, "buyer": None}),
                  (1, 2, "buy", t, "b", {"tx": tx, "buyer": None}),
                  (1, 3, "sell", t, "s2", {"tx": "0x" + "bb" * 32, "buyer": DEPLOYER})]  # named by the event
        mark_dev_sells(events, {(TOKEN, tx)})
        self.assertEqual([e[5].get("dev_sell") for e in events], [True, None, True])

    def test_the_post(self):
        p = render.sample_payload("TST", "ETH", 3000.0) | {
            "token": TOKEN, "dev": True, "dev_sell": True, "buyer": DEPLOYER,
            "holder": {"change": -0.35, "share": 0.012}}
        text = render.buy_text(p, chat(), sell=True)
        self.assertTrue(text.startswith("🚨 <b>DEV SELL</b> · <b>$TST</b>"), text)
        self.assertIn("├ 👨‍💻 0x3f92…10a9 · 🚨 sold 35% of the bag · holds 1.20%", text)
        self.assertNotIn("🔴", text)
        self.assertIn("ДЕВ ПРОДАЁТ", render.buy_text(p, chat(lang="ru"), sell=True))
        self.assertIn("🚪 Sold the whole bag", render.buy_text(p | {"holder": {"all": True}}, chat(), sell=True))
        self.assertNotIn("sold", render.buy_text(p | {"holder": None}, chat(), sell=True).split("👨‍💻")[1])
        # a dev's buy keeps the plain DEV mark
        self.assertIn("🟢 <b>BUY</b> · <b>$TST</b> · 👨‍💻 <b>DEV</b>", render.buy_text(p | {"dev_sell": False}, chat()))


class FakeScanRpc:
    """A token's whole history for /scan: get_logs filtered like the RPC, eth_call for the
    token's reads and balances, the mint transaction's sender."""

    def __init__(self, token, logs, balances, symbol="TST", sender=None):
        self.token, self.logs, self.balances, self.symbol, self.sender = token, logs, balances, symbol, sender
        self.get_logs_calls = []

    async def get_logs(self, frm, to, address=None, topics=None):
        self.get_logs_calls.append((frm, to, address, topics))
        addrs = {a.lower() for a in ([address] if isinstance(address, str) else address or [])}
        out = []
        for lg in self.logs:
            if not frm <= int(lg["blockNumber"], 16) <= to or (addrs and lg["address"].lower() not in addrs):
                continue
            want = topics or []
            if len(want) > len(lg["topics"]):
                continue
            if all(w is None or (lg["topics"][i] in w if isinstance(w, list) else lg["topics"][i] == w)
                   for i, w in enumerate(want)):
                out.append(lg)
        return out

    async def eth_call(self, to, data, block="latest"):
        def word(v):
            return "0x" + format(v, "064x")

        def string(s):
            return word(32) + format(len(s), "064x") + s.encode().hex().ljust(64, "0")
        if to == chain.PONS_FACTORY:
            return "0x" + "00" * 32 * 15
        if to != self.token:
            raise RuntimeError("execution reverted")
        sel = data[:10]
        if sel == chain.SEL_BALANCE_OF:
            return word(self.balances.get("0x" + data[-40:], 0))
        return {chain.SEL_DECIMALS: word(18), chain.SEL_TOTAL_SUPPLY: word(10 ** 24),
                chain.SEL_SYMBOL: string(self.symbol), chain.SEL_NAME: string("Test Token")}[sel]

    async def tx_sender(self, tx):
        return self.sender


class TokenScan(unittest.TestCase):
    """/scan from a token's Transfers: a pool token whose dev opened a v2 pair."""
    X = "0x" + "a1" * 20
    DEV, A, B, C, D, E = ("0x" + c * 20 for c in ("de", "a0", "b1", "c2", "d3", "e4"))
    PAIR, BOT = "0x" + "9a" * 20, "0x" + "b0" * 20
    ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"
    K = 10 ** 18

    def history(self):
        k, x = self.K, self.X
        moves = [(100, abi.ZERO_ADDRESS, self.DEV, 1_000_000), (200, self.DEV, self.PAIR, 800_000),  # mint, the pair
                 (201, self.PAIR, self.A, 50_000),                                     # snipers ...
                 (210, self.PAIR, self.BOT, 40_000), (210, self.BOT, self.B, 40_000),   # ... through a bot
                 (220, self.PAIR, self.ROUTER, 30_000), (220, self.ROUTER, self.C, 30_000),  # ... and a router
                 (400, self.PAIR, self.D, 100_000),                                    # a later buyer
                 (500, self.A, self.PAIR, 20_000),                                     # A sells
                 (600, self.DEV, self.PAIR, 100_000),                                  # the dev sells
                 (700, self.DEV, self.E, 50_000),                                      # and moves some
                 (800, self.D, "0x000000000000000000000000000000000000dead", 10_000)]  # a burn
        logs = []
        for i, (block, frm, to, v) in enumerate(moves):
            tx = "0x" + format(block, "064x")
            logs.append(transfer_log(x, frm, to, v * k, block, tx, i))
        created = {"address": next(iter(pools.V2_FACTORIES)), "blockNumber": hex(150), "logIndex": "0x0",
                   "transactionHash": "0x" + "11" * 32, "data": "0x" + abi.encode_address(self.PAIR) + format(1, "064x"),
                   "topics": [pools.TOPIC_V2_PAIR_CREATED, abi.address_topic(quotes.WETH), abi.address_topic(x)]}
        balances = {self.DEV: 50_000 * k, self.A: 30_000 * k, self.B: 40_000 * k, self.C: 30_000 * k,
                    self.D: 90_000 * k, self.E: 50_000 * k}
        return logs + [created], balances

    def scan(self, symbol="TST", **patch):
        from buybot import scan as scanner
        logs, balances = self.history()
        rpc = FakeScanRpc(self.X, logs, balances, symbol, sender=self.DEV)
        old = {k: getattr(scanner, k) for k in patch}
        try:
            for k, v in patch.items():
                setattr(scanner, k, v)
            return run(scanner.scan(rpc, self.X, 1000, now=1_000_000)), rpc
        finally:
            for k, v in old.items():
                setattr(scanner, k, v)

    def test_the_whole_history(self):
        s, rpc = self.scan()
        self.assertTrue(s.complete)
        self.assertEqual((s.first_block, s.launch_block, s.dev), (100, 200, self.DEV))
        self.assertEqual(s.holders, 6)                        # the pair, 0x…dead and 0x0 are not holders
        self.assertAlmostEqual(s.top_share, 0.29)
        self.assertEqual(s.top[0], (self.D, 0.09))
        self.assertEqual(s.snipers, 3)                        # A, B behind the bot, C behind the router
        self.assertAlmostEqual(s.sniper_share, 0.10)
        self.assertAlmostEqual(s.dev_share, 0.05)
        self.assertAlmostEqual(s.dev_sold, 0.5)               # 100K of the 200K left after the pair
        self.assertAlmostEqual(s.dev_moved, 0.25)
        self.assertEqual(s.sellers, 1)
        self.assertEqual(s.made_ts, 1_000_000 - 90)           # 900 blocks ago
        # the pools were asked for only from the token's first block on
        creations = [c for c in rpc.get_logs_calls if c[3] and c[3][0] != chain.TOPIC_TRANSFER]
        self.assertTrue(creations and all(c[0] >= 100 for c in creations))

    def test_a_window_that_times_out_is_read_in_quarters(self):
        from buybot import scan as scanner
        logs, balances = self.history()

        class Slow(FakeScanRpc):
            async def get_logs(self, frm, to, address=None, topics=None):
                if topics and topics[0] == chain.TOPIC_TRANSFER and len(topics) == 1 and to - frm >= 200:
                    raise Exception("eth_getLogs failed after 6 tries (TimeoutError: )")
                return await super().get_logs(frm, to, address, topics)
        rpc = Slow(self.X, logs, balances, sender=self.DEV)
        old = scanner.MIN_WINDOW, scanner.FIRST_WINDOW
        scanner.MIN_WINDOW, scanner.FIRST_WINDOW = 50, 3200
        try:
            s = run(scanner.scan(rpc, self.X, 1000, now=1_000_000))
            self.assertTrue(s.complete)
            self.assertEqual((s.holders, s.snipers), (6, 3))
            scanner.MIN_WINDOW = 400  # still too big at its smallest: the scan fails, as before
            with self.assertRaises(Exception):
                run(scanner.scan(Slow(self.X, logs, balances, sender=self.DEV), self.X, 1000, now=1_000_000))
        finally:
            scanner.MIN_WINDOW, scanner.FIRST_WINDOW = old

    def test_a_history_too_long_shows_only_what_does_not_need_it(self):
        s, _ = self.scan(MAX_WINDOWS=1, FIRST_WINDOW=150)
        self.assertFalse(s.complete)
        self.assertIsNone(s.top_share)
        self.assertIsNone(s.holders)
        self.assertIsNone(s.dev_sold)
        self.assertEqual(s.snipers, 3)
        self.assertAlmostEqual(s.sniper_share, 0.10)          # read back from the chain
        self.assertAlmostEqual(s.dev_share, 0.05)
        text = render.scan_text(s)
        self.assertIn("Over 50,000 transfers", text)
        self.assertNotIn("Top 10", text)

    def test_not_a_token(self):
        from buybot import scan as scanner
        rpc = FakeScanRpc(self.X, [], {})
        with self.assertRaises(scanner.ScanError) as e:
            run(scanner.scan(rpc, "0x" + "77" * 20, 1000))
        self.assertEqual(e.exception.key, "scan_not_token")
        with self.assertRaises(scanner.ScanError) as e:
            run(scanner.scan(rpc, self.X, 1000))  # a token with no mint
        self.assertEqual(e.exception.key, "scan_no_history")

    def test_copy_tickers(self):
        from buybot import scan as scanner
        nvda = next(a for a, q in quotes.QUOTES.items() if q[0] == "NVDA")
        known = scanner.known_tokens(None, "$nvda")
        self.assertEqual(known, [("NVDA", nvda, None)])
        self.assertEqual(scanner.find_copies("NVDA", self.X, 1000.0, known), [("NVDA", nvda)])
        self.assertEqual(scanner.find_copies("NVDA", nvda, 1000.0, known), [])  # itself
        older, newer = ("TST", "0x" + "01" * 20, 500), ("TST", "0x" + "02" * 20, 1500)
        self.assertEqual(scanner.find_copies("tst", self.X, 1000.0, [older, newer]), [("TST", older[1])])
        self.assertEqual(scanner.find_copies("TST", self.X, None, [older]), [])  # cannot tell: not called a copy

    def test_tokens_with_a_symbol(self):
        with tempfile.TemporaryDirectory() as d:
            db = Db(os.path.join(d, "t.db"))
            db.upsert_token(fixture_token(added_at=7))
            self.assertEqual(db.tokens_with_symbol("$tst"), [("TST", TOKEN, 7)])
            self.assertEqual(db.tokens_with_symbol("TSTX"), [])
            db.close()

    def test_the_answer(self):
        import re
        s, _ = self.scan()
        s.copies = [("TST", "0x" + "01" * 20)]
        text = render.scan_text(s, explorer="https://e")
        self.assertIn("🛡 <b>Scan · $TST</b> · Test Token", text)
        self.assertIn("👑 Top 10 wallets hold <b>29.00%</b> of the supply", text)
        self.assertIn("👥 Holders: <b>6</b>", text)
        self.assertIn("🎯 Snipers: <b>3</b> wallets bought in the first 5 s · hold <b>10.00%</b> now", text)
        self.assertIn("holds <b>5.00%</b> · 🚨 sold <b>50%</b> of what they got · sent 25% to other wallets", text)
        self.assertIn("🔁 Wallets that sold: <b>1</b>", text)
        self.assertIn("⚠️ <b>Copy ticker</b>", text)
        self.assertIn("honeypot", text)
        for m in re.finditer("<", text):  # Telegram's HTML only
            self.assertRegex(text[m.start():m.start() + 12], r"^</?(b|i|a|code)\b")
        ru = render.scan_text(s, "ru")
        self.assertIn("Снайперы: <b>3</b> кошелька", ru)
        self.assertIn("Тикер-копия", ru)
        pop = render.scan_popup(s)
        self.assertLessEqual(len(pop), 200)
        self.assertIn("Top 10: 29.0%", pop)
        self.assertIn("Snipers: 3, hold 10.0%", pop)
        self.assertIn("Dev: holds 5.0%, sold 50%", pop)
        self.assertIn("Copy of an older $TST", pop)
        s.copies = []
        self.assertIn("✅ Ticker: no older $TST", render.scan_text(s))


class Texts(unittest.TestCase):
    def test_nothing_is_sold_or_advertised(self):
        """No text offers Pro, ads, Stars or 'free with ads' any more."""
        import re
        from buybot import i18n
        banned = [r"\bPro\b", r"Stars", r"/pro\b", r"/advertise", r"/paysupport", r"[Aa]dvertise",
                  r"ad line", r"Free forever", r"рекламн\w* строк", r"Бесплатно навсегда"]
        for key, pair in i18n.TEXTS.items():
            for text in pair:
                for pattern in banned:
                    self.assertIsNone(re.search(pattern, text), (key, pattern))
        for gone in ("footer_ad", "ads_info", "pro_menu", "pro_offer", "pay_invalid", "btn_pro", "set_free"):
            self.assertNotIn(gone, i18n.TEXTS)

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
        self.assertEqual(i18n.plural(4, "ru", ("week", "weeks"), ("неделя", "недели", "недель")), "недели")

    def test_posts_in_russian(self):
        text = render.buy_text(render.sample_payload("TST", "ETH", 3000.0), chat(lang="ru"), sell=True)
        self.assertIn("<b>ПРОДАЖА</b> · <b>$TST</b>", text)
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
    def test_dotenv(self):
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
