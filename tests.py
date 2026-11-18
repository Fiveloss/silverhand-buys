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

from buybot import abi, chain, emoji, flap, pools, quotes, render, vibe
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
# vibe/vibe V6 on testnet: a launch, its curve's whole life up to graduation, a curve quoted
# in another token, and a curve's views right before and after one trade.
VIBE = json.loads((Path(__file__).parent / "fixtures" / "vibe_v6_testnet.json").read_text())
# vibe/vibe's curve factory: one curve's life (every trade in `replay`) and a live launch.
VIBE_C = json.loads((Path(__file__).parent / "fixtures" / "vibe_curve_testnet.json").read_text())
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

    @needs("PIL")
    def test_failed_frames_stop_ffmpeg_and_close_its_pipes(self):
        import io
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from buybot import card, promo_video
        for module, factory, invoke in (
                (card, "Card", lambda: card.render_card({}, fps=1, seconds=1)),
                (promo_video, "video", lambda: promo_video.render({}, fps=1, seconds=1))):
            with self.subTest(renderer=module.__name__):
                proc = SimpleNamespace(stdin=io.BytesIO(), stderr=io.BytesIO(),
                                       poll=Mock(return_value=None), kill=Mock(), wait=Mock(return_value=0))
                broken = SimpleNamespace(W=2, H=2, frame=Mock(side_effect=RuntimeError("bad frame")))
                with patch.object(module, factory, return_value=broken), \
                        patch.object(module, "_ffmpeg", return_value="ffmpeg"), \
                        patch.object(module.subprocess, "Popen", return_value=proc):
                    with self.assertRaisesRegex(RuntimeError, "bad frame"):
                        invoke()
                proc.kill.assert_called_once()
                proc.wait.assert_called_once_with(timeout=5)
                self.assertTrue(proc.stdin.closed)
                self.assertTrue(proc.stderr.closed)

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

    def test_event_markers_and_posts_rollback_together(self):
        import sqlite3
        self.db.set_last_block(9)
        # Fail after the posts and trades were inserted, when persisting the marker.
        self.db.conn.execute("CREATE TEMP TRIGGER fail_marker BEFORE INSERT ON meta "
                             "WHEN NEW.key='level:mc:token' BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        rows = [(-1, "milestone", "level:1", {"level": 1})]
        trades = [(TOKEN, "tx:1", 100, 1e-8, 0.1, 1)]
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.commit_tick(rows, 10, trades=trades, meta_updates={"level:mc:token": "1"})
        self.assertEqual(self.db.last_block(), 9)
        self.assertEqual(self.db.outbox_size(), 0)
        self.assertEqual(self.db.trades_since(TOKEN, 0), [])
        self.assertIsNone(self.db.get_meta("level:mc:token"))
        self.db.conn.execute("DROP TRIGGER fail_marker")
        self.assertEqual(self.db.commit_tick(rows, 10, trades=trades,
                                             meta_updates={"level:mc:token": "1"}), 1)
        self.assertEqual(self.db.get_meta("level:mc:token"), "1")

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

    def test_database_cannot_be_reused_for_another_network(self):
        self.db.bind_chain(46630)
        self.db.bind_chain(46630)
        with self.assertRaises(ValueError):
            self.db.bind_chain(4663, legacy_chain_id=4663)
        self.assertEqual(self.db.get_meta("chain_id"), "46630")

    def test_legacy_network_confirmation_preserves_data(self):
        self.db.ensure_chat(-1, "Existing chat")
        self.db.upsert_token(fixture_token())
        self.db.set_last_block(123)
        with self.assertRaises(ValueError):
            self.db.bind_chain(4663)
        with self.assertRaises(ValueError):
            self.db.bind_chain(4663, legacy_chain_id=46630)
        self.assertIsNone(self.db.get_meta("chain_id"))
        self.db.bind_chain(4663, legacy_chain_id=4663)
        self.assertEqual(self.db.get_chat(-1).title, "Existing chat")
        self.assertEqual(self.db.get_token(TOKEN).address, TOKEN)
        self.assertEqual(self.db.last_block(), 123)


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
            updates = {}
            out = [(e[5]["what"], e[5]["level"]) for e in w._milestones(list(events), updates)]
            self.db.queue_events([], updates)
            return out

        self.assertEqual(levels(ev("buy", 12_000, 0.52)), [])  # the first trade only sets the base
        self.assertEqual(levels(ev("buy", 27_000, 0.6)), [("mc", 25_000)])
        self.assertEqual(levels(ev("sell", 9_000), ev("buy", 26_000)), [])  # a swing, no new high
        self.assertEqual(levels(ev("buy", 60_000, 0.91)), [("mc", 50_000), ("curve", 0.9)])
        self.assertEqual(levels(ev("sell", 120_000)), [])  # sells never post one
        self.assertEqual(levels(ev("buy", 110_000)), [])   # but the sell set the new high

    def test_failed_tick_does_not_consume_milestones(self):
        from unittest.mock import AsyncMock, patch
        self.db.set_last_block(self.first_block - 1)
        w = self.watcher(FakeRpc(self.last_block, self.buys))
        key = f"level:mc:{TOKEN}"
        with patch.object(w, "_about_traders", new=AsyncMock(side_effect=RuntimeError("RPC failed"))):
            with self.assertRaises(RuntimeError):
                run(w.tick())
        self.assertIsNone(self.db.get_meta(key))
        self.assertEqual(self.db.last_block(), self.first_block - 1)
        self.assertEqual(self.db.outbox_size(), 0)
        run(w.tick())
        self.assertIsNotNone(self.db.get_meta(key))
        self.assertGreater(self.db.outbox_size(), 0)

    def test_static_rpc_head_is_distinct_from_a_successful_poll(self):
        from unittest.mock import patch
        w = self.watcher(FakeRpc(self.last_block, []))
        with patch("buybot.watcher.time.time", return_value=1000):
            run(w.tick())
        with patch("buybot.watcher.time.time", return_value=1400):
            run(w.tick())
        self.assertEqual(w.last_rpc_ok, 1400)
        self.assertEqual(w.last_head_change, 1000)

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
        self.db.update_chat(-1, token=FLAP["token"], sells=1, min_usd=0)  # every trade, however small

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


def _transfers(logs: list[dict], token: str) -> list[tuple[str, str, int]]:
    return [(abi.topic_address(lg["topics"][1]), abi.topic_address(lg["topics"][2]), int(lg["data"], 16))
            for lg in logs if lg["address"] == token and lg["topics"][0] == chain.TOPIC_TRANSFER]


def vibe_state(views: dict) -> vibe.State:
    num = {k: int(v, 16) for k, v in views.items()}
    return vibe.decode_state("0x" + views["token"][-40:], "0x" + views["quote"][-40:], num["target"],
                             num["real_quote"], num["tokens_left"], num["tokens_sold"], num["complete"],
                             views["pool_id"])


class VibeDecoding(unittest.TestCase):
    """Field meanings checked against the token Transfers of the same transactions."""

    def test_topics_and_selectors(self):
        for topic, sig in [(vibe.TOPIC_CURVE_BUY, "CurveBuy(address,address,uint256,uint256,uint256,uint256,uint256,uint256)"),
                           (vibe.TOPIC_CURVE_SELL, "CurveSell(address,address,uint256,uint256,uint256,uint256,uint256)"),
                           (vibe.TOPIC_INITIAL_PURCHASE, "InitialPurchase(address,uint256,uint256,uint256,uint256)"),
                           (vibe.TOPIC_GRADUATED, "Graduated(bytes32,uint256,uint256,uint256)")]:
            self.assertEqual(topic, abi.event_topic(sig))
        for sel, sig in [(vibe.SEL_TOKEN, "token()"), (vibe.SEL_FACTORY, "factory()"),
                         (vibe.SEL_COMPLETE, "complete()"), (vibe.SEL_POOL_ID, "poolId()")]:
            self.assertEqual(sel, abi.selector(sig))

    def test_launch_and_the_creators_first_buy(self):
        launch = vibe.decode_launch(VIBE["launch"]["log"], VIBE["factory"])
        self.assertEqual((launch.token, launch.curve), (VIBE["curve"]["token"], VIBE["curve"]["address"]))
        self.assertEqual((launch.quote, launch.bps, launch.target), (abi.ZERO_ADDRESS, 200, 4 * ETH))
        self.assertIsNone(vibe.decode_launch(VIBE["launch"]["log"], "0x" + "11" * 20))  # not the factory
        tx = VIBE["launch"]["tx"]
        firsts = [b for b in map(vibe.decode_first_buy, tx["logs"]) if b]
        self.assertEqual(len(firsts), 1)
        b = firsts[0]
        self.assertTrue(b.first)
        self.assertEqual(b.recipient, launch.launcher)
        self.assertEqual(b.recipient, tx["from"])
        self.assertEqual(b.tokens, launch.first_buy)
        moved = _transfers(tx["logs"], launch.token)
        self.assertIn((launch.curve, launch.launcher, b.tokens), moved)
        self.assertIn((launch.curve, launch.token, b.tax_tokens), moved)  # the tax, to the token contract
        # the transaction's value: the buy, its fee and a 0.0004 ETH launch fee
        self.assertEqual(int(tx["value"], 16), b.quote_paid + 4 * 10 ** 14)

    def test_router_buy_and_sell_in_a_token_quoted_curve(self):
        launch = vibe.decode_launch(VIBE["quoted_launch"]["log"], VIBE["factory"])
        self.assertNotEqual(launch.quote, abi.ZERO_ADDRESS)
        buy_tx, sell_tx = VIBE["quoted_buy"], VIBE["quoted_sell"]
        b = next(x for x in map(vibe.decode_buy, buy_tx["logs"]) if x)
        self.assertEqual((b.curve, b.caller, b.recipient), (launch.curve, VIBE["router"], buy_tx["from"]))
        moved = _transfers(buy_tx["logs"], launch.token)
        self.assertIn((launch.curve, b.recipient, b.tokens), moved)
        self.assertIn((launch.curve, launch.token, b.tax_tokens), moved)
        paid = [v for _, to, v in _transfers(buy_tx["logs"], launch.quote) if to == launch.curve]
        self.assertEqual(len(paid), 1)
        self.assertLessEqual(abs(paid[0] - b.quote_paid), 1)  # quote net + fee, to the wei
        s = next(x for x in map(vibe.decode_sell, sell_tx["logs"]) if x)
        self.assertEqual((s.seller, s.recipient), (sell_tx["from"], VIBE["router"]))
        moved = _transfers(sell_tx["logs"], launch.token)
        self.assertIn((s.seller, launch.curve, s.tokens), moved)
        self.assertIn((launch.curve, launch.token, s.tax_tokens), moved)
        self.assertIn((launch.curve, s.recipient, s.quote_received), _transfers(sell_tx["logs"], launch.quote))
        # nothing else decodes as a trade
        self.assertEqual(sum(vibe.decode_trade(lg) is not None for lg in buy_tx["logs"] + sell_tx["logs"]), 2)

    def test_the_charge_is_split_between_tax_and_fee(self):
        buys = [b for b in map(vibe.decode_buy, VIBE["curve"]["logs"]) if b]
        self.assertGreater(len(buys), 10)
        for b in buys:
            self.assertAlmostEqual(b.tax_tokens / b.tokens_gross + b.fee / b.quote_net, b.bps / 10_000, places=6)

    def test_a_whole_curve_replays_to_its_target(self):
        """Buys move net quote in and gross tokens out, sells gross quote out and net tokens in;
        over the curve's life that ends at exactly the target with nothing left. At every
        trade the event-only model gives the reserves' own price and progress (the quote
        raised of the target: vibe/vibe's site shows that for V6 curves)."""
        target, supply = 4 * ETH, 8 * 10 ** 26
        k, phantom = vibe.curve_k(target, supply), vibe.virtual_quote(target)
        real, left = 0, supply
        kinds = set()
        for lg in VIBE["curve"]["logs"]:
            tr = vibe.decode_trade(lg)
            if tr is None:
                continue
            if isinstance(tr, vibe.Buy):
                real, left = real + tr.quote_net, left - tr.tokens_gross
                kinds.add("first" if tr.first else "buy")
            else:
                real, left = real - tr.quote_gross, left + tr.tokens_net
                kinds.add("sell")
            price, progress = vibe.after_trade(tr, k, phantom, target, 18, 18)
            self.assertAlmostEqual(price / ((real + phantom) / (left + supply // 3)), 1, places=6)
            self.assertAlmostEqual(progress, real / target, places=6)  # V6: the quote raised, as the site
        self.assertEqual(kinds, {"first", "buy", "sell"})
        self.assertEqual((real, left), (target, 0))
        g = next(x for x in map(vibe.decode_graduated, VIBE["curve"]["logs"]) if x)
        self.assertEqual((g.quote_to_pool, g.tokens_to_pool), (target, 2 * 10 ** 26))

    def test_the_curve_percent_is_vibe_vibes(self):
        """vibe/vibe's site shows the tokens sold of a curve-factory curve (its API's
        progressBps: VORRA 92.0% with 75.1% of the ETH raised) but the quote raised of a V6
        curve ($SHBUYS: 43.9% on the site, 75.8% of its tokens sold). The helpers give the
        tokens' share for either shape: on V6, 87.8% of the quote is 96.6% of the tokens."""
        target = 4 * ETH
        k, phantom = vibe.curve_k(target, vibe.V6_CURVE_SUPPLY), vibe.virtual_quote(target)
        self.assertEqual(vibe.curve_supply(k, phantom), vibe.V6_CURVE_SUPPLY)
        self.assertAlmostEqual(vibe.sold_share(k, phantom, phantom + 0.878 * target), 4 / 3 - 4 / (9 * 0.878 + 3))
        self.assertAlmostEqual(vibe.sold_share(k, phantom, phantom + target), 1.0)
        self.assertEqual(vibe.sold_share(k, phantom, phantom), 0.0)
        self.assertIsNone(vibe.curve_supply(k, phantom * 10))  # a shape we do not know

    def test_graduation_opens_the_pool_it_names(self):
        token, curve = VIBE["curve"]["token"], VIBE["curve"]["address"]
        logs = VIBE["graduation"]["logs"]
        g = next(x for x in map(vibe.decode_graduated, logs) if x)
        self.assertEqual(g.curve, curve)
        self.assertEqual(vibe.pool_id(token, abi.ZERO_ADDRESS, VIBE["hook"]), g.pool_id)
        self.assertEqual(VIBE["graduated_views"]["pool_id"], g.pool_id)
        init = next(lg for lg in logs if lg["topics"][0] == pools.TOPIC_V4_INITIALIZE)
        self.assertEqual(init["topics"][1], g.pool_id)
        data = abi.to_bytes(init["data"])
        self.assertEqual((abi.word_uint(data, 0), abi.word_uint(data, 1), abi.word_address(data, 2)),
                         (0, 60, VIBE["hook"]))
        b = next(x for x in map(vibe.decode_buy, logs) if x)  # the buy that filled the curve
        self.assertEqual(b.recipient, VIBE["graduation"]["from"])
        self.assertLess(b.log_index, g.log_index)

    def test_views_around_a_live_trade(self):
        snap = VIBE["snapshot"]
        before, after = vibe_state(snap["before"]), vibe_state(snap["after"])
        tr = vibe.decode_trade(snap["trade"])
        self.assertIsInstance(tr, vibe.Buy)
        self.assertEqual(after.real_quote - before.real_quote, tr.quote_net)
        self.assertEqual(before.tokens_left - after.tokens_left, tr.tokens_gross)
        self.assertEqual(after.k, before.k)
        price, progress = vibe.after_trade(tr, before.k, before.phantom, before.target, 18, 18)
        self.assertAlmostEqual(price / after.price(), 1, places=9)
        self.assertAlmostEqual(progress, after.progress, places=9)
        self.assertFalse(after.complete)
        self.assertIsNone(after.pool_id)
        done = vibe_state(VIBE["graduated_views"])
        self.assertTrue(done.complete)
        self.assertEqual(done.pool_id, VIBE["graduated_views"]["pool_id"])

    def test_contracts_per_chain(self):
        v6, curve = chain.vibe_contracts(chain.TESTNET_CHAIN_ID)
        self.assertEqual((v6.factory, v6.hook, v6.router, v6.gen), (VIBE["factory"], VIBE["hook"], VIBE["router"], "v6"))
        self.assertEqual((curve.factory, curve.gen), (VIBE_C["factory"], "curve"))
        self.assertEqual(chain.vibe_contracts(chain.CHAIN_ID), ())  # not on mainnet yet
        self.assertEqual(chain.vibe_contracts(None), ())
        self.assertIs(chain.vibe_factory(VIBE_C["factory"].upper().replace("0X", "0x")), curve)
        self.assertIsNone(chain.vibe_factory("0x" + "11" * 20))
        self.assertEqual(chain.network(chain.TESTNET_CHAIN_ID), "Robinhood Chain testnet")
        self.assertIn(VIBE["router"], pools.ROUTERS)

    def test_a_taxed_tokens_own_contract_is_never_the_buyer(self):
        # the pool hook sends the tax to the token contract before the buyer's share
        tx = VIBE["quoted_buy"]
        quote = vibe.decode_launch(VIBE["quoted_launch"]["log"], VIBE["factory"]).quote
        moved = _transfers(tx["logs"], quote)
        self.assertEqual(moved[0][:2], (chain.POOL_MANAGER, quote))
        named = pools.payouts(tx["logs"], {chain.POOL_MANAGER}).get((quote, tx["tx"]))
        self.assertNotEqual(named, quote)
        self.assertIsNotNone(named)

    def test_state_is_read_only_from_the_factorys_curves(self):
        snap = VIBE["snapshot"]
        names = {vibe.SEL_TOKEN: "token", vibe.SEL_FACTORY: "factory", vibe.SEL_QUOTE: "quote",
                 vibe.SEL_TARGET: "target", vibe.SEL_REAL_QUOTE: "real_quote",
                 vibe.SEL_TOKENS_LEFT: "tokens_left", vibe.SEL_TOKENS_SOLD: "tokens_sold",
                 vibe.SEL_COMPLETE: "complete", vibe.SEL_POOL_ID: "pool_id"}

        class Rpc:
            async def eth_call(self, to, data, block="latest"):
                return snap["after"][names[data]]

        self.assertEqual(run(vibe.state(Rpc(), snap["curve"], VIBE["factory"])), vibe_state(snap["after"]))
        self.assertIsNone(run(vibe.state(Rpc(), snap["curve"], "0x" + "11" * 20)))


class VibeRpc:
    """A testnet RPC that knows one graduated vibe/vibe launch (from the fixture)."""

    def __init__(self, chain_id=chain.TESTNET_CHAIN_ID, launch_log=None, curve_views=None):
        self.launch = launch_log or VIBE["launch"]["log"]
        self.views = curve_views or VIBE["graduated_views"]
        self.token, self.curve = VIBE["curve"]["token"], VIBE["curve"]["address"]
        self._chain_id = chain_id
        self.block = int(self.launch["blockNumber"], 16)
        self.created = int(self.views["created_at"], 16)
        self.head = self.block + 50_000

    async def chain_id(self):
        return self._chain_id

    async def block_number(self):
        return self.head

    async def block_time(self, block):
        return self.created + (block - self.block) // 7  # about 7 blocks a second

    async def get_logs(self, frm, to, address=None, topics=None):
        lg = self.launch
        if (address == lg["address"] and frm <= self.block <= to and topics[0] == lg["topics"][0]
                and topics[3] == lg["topics"][3]):
            return [lg]
        return []

    async def eth_call(self, to, data, block="latest"):
        word = {
            (self.token, "0x7165485d"): abi.address_topic(self.curve),
            (self.token, chain.SEL_DECIMALS): "0x" + abi.encode_uint(18),
            (self.token, chain.SEL_TOTAL_SUPPLY): "0x" + abi.encode_uint(10 ** 27),
        }.get((to, data))
        if word:
            return word
        if to == self.token and data in (chain.SEL_NAME, chain.SEL_SYMBOL):
            return "0x" + abi.encode_uint(32) + abi.encode_uint(2) + b"UG".hex().ljust(64, "0")
        if to == self.curve:
            by_sel = {vibe.SEL_TOKEN: "token", vibe.SEL_FACTORY: "factory", vibe.SEL_QUOTE: "quote",
                      vibe.SEL_TARGET: "target", vibe.SEL_REAL_QUOTE: "real_quote",
                      vibe.SEL_TOKENS_LEFT: "tokens_left", vibe.SEL_TOKENS_SOLD: "tokens_sold",
                      vibe.SEL_COMPLETE: "complete", vibe.SEL_POOL_ID: "pool_id", "0xcf09e0d0": "created_at"}
            if data in by_sel:
                return self.views[by_sel[data]]
        return "0x"  # no contract there (the Pons factory and Flap's Portal on testnet)


class VibeTracking(unittest.TestCase):
    def test_a_vibe_launch_is_recognised_by_the_factorys_log(self):
        from buybot import tokens
        t, found = run(tokens.load_token(VibeRpc(), VIBE["curve"]["token"]))
        launch = vibe.decode_launch(VIBE["launch"]["log"], VIBE["factory"])
        self.assertEqual(found, [])
        self.assertEqual((t.kind, t.curve, t.deployer, t.pair), ("vibe", launch.curve, launch.launcher, chain.NATIVE))
        self.assertEqual((t.threshold, t.quote_symbol, t.quote_kind), (4 * ETH, "ETH", "eth"))
        self.assertTrue(t.graduated)
        self.assertEqual(t.pool_id, VIBE["graduated_views"]["pool_id"])

    def test_a_curve_on_the_curve_keeps_its_constants(self):
        from buybot import tokens
        live = dict(VIBE["snapshot"]["after"], token=VIBE["graduated_views"]["token"],
                    created_at=VIBE["graduated_views"]["created_at"])
        t, _ = run(tokens.load_token(VibeRpc(curve_views=live), VIBE["curve"]["token"]))
        st = vibe_state(live)
        self.assertFalse(t.graduated)
        self.assertEqual((t.curve_k, t.phantom), (st.k, st.phantom))
        self.assertEqual(t.pool_id, vibe.pool_id(t.address, chain.NATIVE, VIBE["hook"]))

    def test_a_vibe_token_as_quote_is_valued_by_its_curve(self):
        live = dict(VIBE["snapshot"]["after"], token=VIBE["graduated_views"]["token"])
        rpc = VibeRpc(curve_views=live)
        kind = f"vibe:{VIBE['curve']['address']}:{'0x' + '00' * 32}"
        rate = run(quotes.Rates(rpc, FakeEthUsd()).get(kind))
        self.assertAlmostEqual(rate, vibe_state(live).price() * 3000)
        self.assertIsNone(run(quotes.Rates(rpc, FakeEthUsd()).get("vibe:" + "0x" + "11" * 20 + ":0x00")))

    def test_only_the_factorys_own_log_counts(self):
        from buybot import tokens
        fake = dict(VIBE["launch"]["log"], address="0x" + "11" * 20)
        with self.assertRaises(tokens.TokenError):
            run(tokens.load_token(VibeRpc(launch_log=fake), VIBE["curve"]["token"]))
        with self.assertRaises(tokens.TokenError):  # no vibe/vibe on mainnet yet
            run(tokens.load_token(VibeRpc(chain_id=chain.CHAIN_ID), VIBE["curve"]["token"]))

    def test_a_revert_means_not_vibe_but_a_failing_rpc_is_raised(self):
        from buybot import tokens

        class Reverts(VibeRpc):
            async def eth_call(self, to, data, block="latest"):
                if data == "0x7165485d":
                    raise Exception("RPC error 3: execution reverted")
                return await super().eth_call(to, data, block)

        class Down(VibeRpc):
            async def get_logs(self, frm, to, address=None, topics=None):
                raise Exception("getLogs failed after 6 tries (HTTP 503)")

        contracts = chain.vibe_contracts(chain.TESTNET_CHAIN_ID)[0]
        self.assertIsNone(run(tokens.find_vibe_launch(Reverts(), VIBE["curve"]["token"], contracts)))
        with self.assertRaises(Exception) as caught:
            run(tokens.load_token(Down(), VIBE["curve"]["token"]))
        self.assertNotIsInstance(caught.exception, tokens.TokenError)  # "chain down", not "not a token"


class VibeFakeRpc(FakeRpc):
    async def chain_id(self):
        return chain.TESTNET_CHAIN_ID


def vibe_token(**over) -> Token:
    launch = vibe.decode_launch(VIBE["launch"]["log"], VIBE["factory"])
    t = Token(address=launch.token, curve=launch.curve, deployer=launch.launcher, pair=chain.NATIVE,
              quote_symbol="ETH", quote_decimals=18, quote_kind="eth", name="Vibe Token", symbol="UG",
              decimals=18, total_supply=10 ** 27, threshold=launch.target,
              pool_id=vibe.pool_id(launch.token, chain.NATIVE, VIBE["hook"]),
              curve_k=vibe.curve_k(launch.target, 8 * 10 ** 26), phantom=vibe.virtual_quote(launch.target),
              graduated=False, added_at=0, kind="vibe")
    for k, v in over.items():
        setattr(t, k, v)
    return t


# vibe/vibe's test AAPL and its pool against ETH, as on testnet on 2026-10-19 (the swap
# in 0xfb26c737…: 0.0409 ETH in through the router): 1 AAPL = 1.3116 ETH
TEST_AAPL = "0x438820dcfe62a21e306614a4b54383cd8a36acf2"
AAPL_SQRT_PRICE, AAPL_LIQUIDITY = 69180935715363039438302251340, 7071067811865475244


class EthPoolRpc(VibeFakeRpc):
    """Answers the PoolManager's extsload for one pool: its sqrtPriceX96 and liquidity."""
    pool_liquidity = AAPL_LIQUIDITY

    async def eth_call(self, to, data, block="latest"):
        if to == chain.POOL_MANAGER and data.startswith(pools.SEL_EXTSLOAD):
            slot = int(data[len(pools.SEL_EXTSLOAD):], 16)
            base = pools.v4_state_slot(pools.eth_pool_id(TEST_AAPL))
            if slot == base:
                return "0x" + abi.encode_uint(AAPL_SQRT_PRICE)
            if slot == base + pools.V4_LIQUIDITY_OFFSET:
                return "0x" + abi.encode_uint(self.pool_liquidity)
            return "0x" + abi.encode_uint(0)
        return await super().eth_call(to, data, block)


class EthPoolQuotes(unittest.TestCase):
    """A testnet stock token has no feed: its v4 pool against ETH values it, the pool
    vibe/vibe's router swaps through and the vibe/vibe site prices it by."""

    def test_the_pool_id_is_the_routers(self):
        self.assertEqual(pools.eth_pool_id(TEST_AAPL),
                         "0x60e44167df8938068cdb05cbeb03b66223261778112681a0484bcf337aea870a")

    def test_share_on_x(self):
        """Buys, levels and graduations get an X Share button in a row of its own, with
        the post's line and the token's page; sells get none."""
        from urllib.parse import parse_qs, urlparse
        from buybot.sender import keyboard
        buy = {"symbol": "SHBUYS", "token": "0x" + "7c" * 20, "tx": "0x" + "ab" * 32, "usd": 327.0,
               "usd_approx": True, "quote": 0.0324, "quote_symbol": "AAPL", "mc_usd": 57917.0}
        self.assertEqual(render.share_text(buy, "buy"), "🟢 $SHBUYS buy: ≈$327 · MC ≈$57,917")
        self.assertIsNone(render.share_text(buy, "sell"))
        self.assertEqual(render.share_text({"symbol": "SHBUYS", "what": "mc", "level": 25000.0}, "milestone"),
                         "🚀 $SHBUYS hit $25K market cap")
        self.assertEqual(render.share_text({"symbol": "SHBUYS", "dex": "Uniswap v4"}, "grad"),
                         "🎓 $SHBUYS graduated to Uniswap v4")
        pairs = render.buttons(buy, "https://testnet.vibevibe.fun/token/{token}", "https://ex",
                               render.share_text(buy, "buy"))
        rows = keyboard(pairs).inline_keyboard
        self.assertEqual([len(r) for r in rows], [4, 1])
        text = parse_qs(urlparse(rows[1][0].url).query)["text"][0]
        self.assertIn("🟢 $SHBUYS buy", text)
        self.assertIn("https://testnet.vibevibe.fun/token/" + buy["token"], text)
        self.assertEqual(len(keyboard(render.buttons(buy, "", "")).inline_keyboard), 1)

    def test_pool_dollars_are_shown_as_approximate(self):
        p = {"symbol": "SHBUYS", "quote_symbol": "AAPL", "quote": 0.03128, "usd": 99.12, "usd_approx": True,
             "tokens": 17e6, "mc_usd": 9190.0, "token": "0x" + "7c" * 20}
        text = render.buy_text(p, chat())
        self.assertIn("≈$99.12", text)
        self.assertIn("MC <b>≈$9,190", text)
        self.assertNotIn("≈", render.buy_text(dict(p, usd_approx=False), chat()))

    def test_a_deep_pool_gives_dollars_and_a_thin_one_none(self):
        from buybot import tokens
        kind = run(tokens.eth_pool_kind(EthPoolRpc(0, []), TEST_AAPL))
        self.assertEqual(kind, f"ethpool:{TEST_AAPL}:{pools.eth_pool_id(TEST_AAPL)}")
        usd = run(quotes.Rates(EthPoolRpc(0, []), FakeEthUsd()).get(kind))
        self.assertAlmostEqual(usd / 3000, 1.31155, places=4)

        class Thin(EthPoolRpc):
            pool_liquidity = 10 ** 15
        self.assertIsNone(run(tokens.eth_pool_kind(Thin(0, []), TEST_AAPL)))
        self.assertIsNone(run(quotes.Rates(Thin(0, []), FakeEthUsd()).get(kind)))


class VibeWatcher(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Db(os.path.join(self.dir.name, "t.db"))
        self.token = vibe_token()
        self.db.upsert_token(self.token)
        self.db.ensure_chat(-1)
        self.db.update_chat(-1, token=self.token.address, sells=1, min_usd=0)  # every trade, however small

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def tick(self, logs: list[dict], balance: float | None = None):
        """One tick over the logs; `balance` is every wallet's token balance, in whole tokens."""
        blocks = [int(lg["blockNumber"], 16) for lg in logs]
        self.db.set_last_block(min(blocks) - 1)

        class Rpc(VibeFakeRpc):
            async def eth_call(self, to, data, block="latest"):
                if balance is not None and data.startswith(chain.SEL_BALANCE_OF):
                    return "0x" + abi.encode_uint(int(balance * 10 ** 18))
                return await super().eth_call(to, data, block)

        w = Watcher(Rpc(max(blocks), logs), self.db, FakeEthUsd(), max_range=10 ** 7, max_catchup=10 ** 7)
        w._phase_checked = time.time()
        run(w.tick())
        return [r for r in self.db.pending_outbox() if r.kind in ("buy", "sell", "grad")]

    def test_a_whole_curve_life_is_posted(self):
        logs = VIBE["curve"]["logs"]
        rows = self.tick(logs)
        trades = [vibe.decode_trade(lg) for lg in logs]
        n_buys = sum(isinstance(tr, vibe.Buy) for tr in trades)
        n_sells = sum(isinstance(tr, vibe.Sell) for tr in trades)
        kinds = [r.kind for r in rows]
        self.assertEqual((kinds.count("buy"), kinds.count("sell"), kinds.count("grad")), (n_buys, n_sells, 1))
        self.assertEqual(kinds[-1], "grad")
        buys = [r.payload for r in rows if r.kind == "buy"]
        first = buys[0]
        self.assertTrue(first["dev"] and first["first_buy"])
        self.assertEqual(first["buyer"], self.token.deployer)
        for p, tr in zip(buys, [tr for tr in trades if isinstance(tr, vibe.Buy)]):
            self.assertEqual(p["buyer"], tr.recipient)
            self.assertNotEqual(p["buyer"], VIBE["router"])
            self.assertAlmostEqual(p["tokens"], tr.tokens / 1e18)       # what the buyer received
            self.assertAlmostEqual(p["tax_tokens"], tr.tax_tokens / 1e18)
            self.assertAlmostEqual(p["quote"], tr.quote_paid / 1e18)   # what the buyer paid
            self.assertEqual((p["venue"], p["launchpad"], p["network"]),
                             ("curve", "vibe/vibe", "Robinhood Chain testnet"))
        self.assertAlmostEqual(buys[-1]["progress"], 1.0, places=6)
        sell = next(r.payload for r in rows if r.kind == "sell")
        st = next(tr for tr in trades if isinstance(tr, vibe.Sell))
        self.assertEqual(sell["buyer"], st.seller)
        self.assertAlmostEqual(sell["quote"], st.quote_received / 1e18)
        self.assertIn("UG", render.buy_text(buys[1], chat()))
        grad = rows[-1].payload
        self.assertEqual((grad["raised"], grad["dex"], grad["launchpad"]), (4.0, "Uniswap v4", "vibe/vibe"))
        # the graduation card: the pool's opening market cap (4 ETH for 200M tokens: 20 ETH), done curve
        self.assertAlmostEqual(grad["mc_quote"], 20.0)
        self.assertEqual((grad["venue"], grad["progress"], grad["quote"]), ("v4", 1.0, 4.0))
        self.assertAlmostEqual(grad["mc_usd"], 20.0 * 3000)
        self.assertIn("Uniswap v4", render.graduation_text(grad))
        self.assertTrue(self.db.get_token(self.token.address).graduated)
        self.assertEqual(len(self.db.trades_since(self.token.address, 0)), n_buys + n_sells)

    def test_two_quick_buys_of_one_wallet_make_one_new_holder(self):
        """Seen on testnet ($TRAV): a wallet bought twice within seconds and both posts said
        "new holder". The balance is read once a tick, so each buy takes back the wallet's
        later trades in it; and a wallet seen buying before is never new again."""
        buy = next(lg for lg in VIBE["curve"]["logs"] if lg["topics"][0] == vibe.TOPIC_CURVE_BUY)
        got = vibe.decode_buy(buy).tokens / 1e18

        def buy_of(wallet, block, index):
            return dict(buy, topics=[*buy["topics"][:2], abi.address_topic(wallet)], blockNumber=hex(block),
                        logIndex=hex(index), transactionHash="0x" + format(block * 100 + index, "064x"))

        def holders(rows, wallet):
            return [r.payload.get("holder") for r in rows if r.kind == "buy" and r.payload["buyer"] == wallet]

        same = "0x" + "e6" * 20  # both buys in one block: the balance holds both
        first, second = holders(self.tick([buy_of(same, 1000, 1), buy_of(same, 1000, 2)], balance=2 * got), same)
        self.assertTrue(first["new"])
        self.assertNotIn("new", second)
        self.assertAlmostEqual(second["change"], 1.0)  # held one buy, bought another: +100%

        apart = "0x" + "e7" * 20  # adjacent blocks, two ticks, sold out in between
        self.assertTrue(holders(self.tick([buy_of(apart, 2000, 1)], balance=got), apart)[0]["new"])
        again = holders(self.tick([buy_of(apart, 2001, 1)], balance=got), apart)
        self.assertEqual(len(again), 2)
        self.assertNotIn("new", again[1])
        self.assertAlmostEqual(again[1]["share"], got / 1e9)
        self.assertNotIn("New holder", render.buy_text(dict(self.db.pending_outbox()[-1].payload), chat()))

    def test_dust_in_a_quote_without_a_price_is_skipped(self):
        """Seen on testnet ($TRAV, quoted in a test AAPL that has no price): a 0.0004 AAPL
        buy was posted. A new chat's minimum also skips such dust, measured against the
        curve's target; /minbuy 0 posts it again."""
        from buybot.db import DEFAULT_MIN_USD
        self.db.ensure_chat(-2)
        self.assertEqual(self.db.get_chat(-2).min_usd, DEFAULT_MIN_USD)
        self.db.update_chat(-1, min_usd=DEFAULT_MIN_USD)
        with self.db.conn:  # the same curve, quoted in a stock token without a price: target 4 AAPL
            self.db.conn.execute("UPDATE tokens SET quote_symbol='AAPL', quote_kind='other'")
        buys = [lg for lg in VIBE["curve"]["logs"] if lg["topics"][0] == vibe.TOPIC_CURVE_BUY]
        quotes_paid = [vibe.decode_buy(lg).quote_paid / 1e18 for lg in buys]
        tiny = [q for q in quotes_paid if q < 4 * 1e-4]
        self.assertTrue(tiny and len(tiny) < len(quotes_paid))  # the fixture has both kinds
        posted = [r.payload for r in self.tick(buys) if r.kind == "buy"]
        self.assertTrue(all(p["usd"] is None for p in posted))
        self.assertEqual(len(posted), len(quotes_paid) - len(tiny))
        self.assertGreaterEqual(min(p["quote"] for p in posted), 4 * 1e-4)

        self.db.update_chat(-1, min_usd=0)
        self.db.clear_chat_outbox(-1)
        self.assertEqual(len([r for r in self.tick(buys) if r.kind == "buy"]), len(buys))

    def test_a_curve_in_test_aapl_gets_approximate_dollars(self):
        """$SHBUYS and $TRAV trade against vibe/vibe's test AAPL: the dollars come from the
        AAPL/ETH pool (marked approximate), and the phase check gives a token tracked
        before this its pool once."""
        with self.db.conn:
            self.db.conn.execute("UPDATE tokens SET quote_symbol='AAPL', quote_kind='other', pair=?",
                                 (TEST_AAPL,))
        w = Watcher(EthPoolRpc(0, []), self.db, FakeEthUsd())
        run(w.refresh_phases(self.db.tracked_tokens()))
        self.assertTrue(self.db.get_token(self.token.address).quote_kind.startswith("ethpool:"))

        buys = [lg for lg in VIBE["curve"]["logs"] if lg["topics"][0] == vibe.TOPIC_CURVE_BUY][:3]
        blocks = [int(lg["blockNumber"], 16) for lg in buys]
        self.db.set_last_block(min(blocks) - 1)
        w = Watcher(EthPoolRpc(max(blocks), buys), self.db, FakeEthUsd(), max_range=10 ** 7, max_catchup=10 ** 7)
        w._phase_checked = time.time()
        run(w.tick())
        posted = [r.payload for r in self.db.pending_outbox() if r.kind == "buy"]
        self.assertEqual(len(posted), 3)
        for p in posted:
            self.assertTrue(p["usd_approx"])
            self.assertAlmostEqual(p["usd"], p["quote"] * 1.31155 * 3000, delta=p["usd"] * 1e-4)
            self.assertIsNotNone(p["mc_usd"])

    def test_pool_buys_show_what_the_buyer_got_after_the_hooks_tax(self):
        self.db.upsert_token(vibe_token(graduated=True))
        gross, tax, block = 10 ** 24, 128 * 10 ** 20, 1000
        wallet, tx = "0x" + "77" * 20, "0x" + "cd" * 32
        swap = v4_swap_log(self.token.pool_id, -ETH, gross, 1000 * 2 ** 96, block=block)

        def transfer(frm, to, value, index):
            return {"address": self.token.address, "transactionHash": tx, "blockNumber": hex(block),
                    "logIndex": hex(index), "data": "0x" + abi.encode_uint(value),
                    "topics": [chain.TOPIC_TRANSFER, abi.address_topic(frm), abi.address_topic(to)]}

        logs = [swap, transfer(chain.POOL_MANAGER, self.token.address, tax, 2),
                transfer(chain.POOL_MANAGER, VIBE["router"], gross - tax, 3),
                transfer(VIBE["router"], wallet, gross - tax, 4)]
        rows = self.tick(logs)
        self.assertEqual([r.kind for r in rows], ["buy"])
        p = rows[0].payload
        self.assertEqual((p["venue"], p["dex"], p["buyer"]), ("v4", "Uniswap v4", wallet))
        self.assertAlmostEqual(p["tokens"], (gross - tax) / 1e18)
        self.assertAlmostEqual(p["tax_tokens"], tax / 1e18)

    def test_pool_sells_show_what_the_seller_gave(self):
        # testnet: the seller sends n tokens to the PoolManager, the hook sends the token
        # contract the tax, and the Swap reports n - tax
        self.db.upsert_token(vibe_token(graduated=True))
        sent, tax, block, tx = 10 ** 24, 128 * 10 ** 20, 1000, "0x" + "cd" * 32
        swap = v4_swap_log(self.token.pool_id, ETH // 10, -(sent - tax), 1000 * 2 ** 96, block=block)
        tax_log = {"address": self.token.address, "transactionHash": tx, "blockNumber": hex(block),
                   "logIndex": "0x2", "data": "0x" + abi.encode_uint(tax),
                   "topics": [chain.TOPIC_TRANSFER, abi.address_topic(chain.POOL_MANAGER),
                              abi.address_topic(self.token.address)]}
        rows = self.tick([swap, tax_log])
        self.assertEqual([r.kind for r in rows], ["sell"])
        self.assertAlmostEqual(rows[0].payload["tokens"], sent / 1e18)
        self.assertAlmostEqual(rows[0].payload["tax_tokens"], tax / 1e18)

    def test_backfill_loads_the_curves_history(self):
        logs = VIBE["curve"]["logs"]
        head = max(int(lg["blockNumber"], 16) for lg in logs)
        w = Watcher(VibeFakeRpc(head, logs), self.db, FakeEthUsd())
        n = sum(isinstance(vibe.decode_trade(lg), (vibe.Buy, vibe.Sell)) for lg in logs)
        self.assertEqual(run(w.backfill(self.token, hours=24 * 30)), n)
        self.assertEqual(len(self.db.trades_since(self.token.address, 0)), n)

    def test_backfill_reads_the_hours_at_the_chains_real_pace(self):
        """Testnet's pace drifts (6.7 blocks a second in October, 8.85 two days later): the
        block times decide where a day of history starts, not the constant."""
        class Slow(VibeFakeRpc):
            async def block_time(self, block):
                return 1_700_000_000 + block // 4      # 4 blocks a second

        head = 10_000_000
        rpc = Slow(head, [])
        run(Watcher(rpc, self.db, FakeEthUsd()).backfill(self.token, hours=1))
        start = min(frm for frm, *_ in rpc.get_logs_calls)
        self.assertAlmostEqual(head - start, 3600 * 4, delta=10)

    def test_phase_refresh_catches_a_missed_graduation(self):
        w = Watcher(VibeRpc(), self.db, FakeEthUsd())
        run(w.refresh_phases(self.db.tracked_tokens()))
        self.assertTrue(self.db.get_token(self.token.address).graduated)


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
    def test_network_defaults_and_miniapp_configuration(self):
        from unittest.mock import patch
        from buybot.config import load_config
        with patch.dict(os.environ, {"BOT_TOKEN": "test", "RPC_URL":
                                    "https://rpc.testnet.chain.robinhood.com"}, clear=True):
            cfg = load_config("missing-test.env")
            self.assertEqual(cfg.chain_id, 46630)
            self.assertIn("testnet", cfg.explorer_url)
            self.assertIn("testnet", cfg.trade_url)
            os.environ["CHAIN_ID"] = "123"
            with self.assertRaises(SystemExit):
                load_config("missing-test.env")
            os.environ["CHAIN_ID"] = "46630"
            os.environ["WEBAPP_URL"] = "https://buys.example.com"
            with self.assertRaises(SystemExit):
                load_config("missing-test.env")
            os.environ["WEBAPP_PORT"] = "8095"
            self.assertEqual(load_config("missing-test.env").webapp_port, 8095)
            os.environ["WEBAPP_URL"] = "https://buys.example.com/nested"
            with self.assertRaises(SystemExit):
                load_config("missing-test.env")

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


C_VQ0 = 1_764_594_628_672_298_575       # INITIAL_VIRTUAL_ETH_RESERVE of a 5 ETH curve
C_VT0 = 1_073_000_000 * 10 ** 18        # virtual tokens at start: 793.1M on the curve + 279.9M
C_HOOK = chain.vibe_contracts(chain.TESTNET_CHAIN_ID)[1].hook


def c_views(**over) -> dict:
    v = dict(VIBE_C["live"]["views"])
    v.update({k: "0x" + abi.encode_uint(n) for k, n in over.items()})
    return v


def c_token(**over) -> Token:
    launch = vibe.decode_launch(VIBE_C["launch"], VIBE_C["factory"])
    t = Token(address=launch.token, curve=launch.curve, deployer=launch.launcher, pair=chain.NATIVE,
              quote_symbol="ETH", quote_decimals=18, quote_kind="eth", name="Curve Token", symbol="CRV",
              decimals=18, total_supply=10 ** 27, threshold=5 * ETH,
              pool_id=vibe.pool_id(launch.token, chain.NATIVE, C_HOOK), curve_k=C_VQ0 * C_VT0, phantom=C_VQ0,
              graduated=False, added_at=0, kind="vibe")
    for k, v in over.items():
        setattr(t, k, v)
    return t


class CRpc:
    """A testnet RPC that knows the live curve-factory launch of the fixture."""

    def __init__(self, views=None):
        live = VIBE_C["live"]
        self.launch, self.curve, self.views = live["launch"], live["curve"], views or live["views"]
        self.token = abi.topic_address(self.launch["topics"][2])
        self.head = int(self.launch["blockNumber"], 16) + 25_000_000  # three windows back
        self.queries = []

    async def chain_id(self):
        return chain.TESTNET_CHAIN_ID

    async def block_number(self):
        return self.head

    async def get_logs(self, frm, to, address=None, topics=None):
        self.queries.append((frm, to, address))
        lg = self.launch
        if (address == lg["address"] and frm <= int(lg["blockNumber"], 16) <= to and topics[0] == lg["topics"][0]
                and topics[2] == lg["topics"][2]):
            return [lg]
        return []

    async def eth_call(self, to, data, block="latest"):
        if to == self.token:
            if data == "0x7165485d":
                return abi.address_topic(self.curve)
            if data == chain.SEL_DECIMALS:
                return "0x" + abi.encode_uint(18)
            if data == chain.SEL_TOTAL_SUPPLY:
                return "0x" + abi.encode_uint(10 ** 27)
            if data in (chain.SEL_NAME, chain.SEL_SYMBOL):
                return "0x" + abi.encode_uint(32) + abi.encode_uint(3) + b"CRV".hex().ljust(64, "0")
        if to == self.curve:
            by_sel = {vibe.SEL_TOKEN: "token", vibe.SEL_FACTORY: "factory", vibe.SEL_COMPLETE: "complete",
                      vibe.SEL_C_GRADUATED: "graduated", vibe.SEL_C_TARGET: "target", vibe.SEL_C_REAL: "real",
                      vibe.SEL_C_LEFT: "left", vibe.SEL_C_VIRTUAL_QUOTE0: "virtual_quote0",
                      vibe.SEL_C_VIRTUAL_TOKENS: "virtual_tokens"}
            if data in by_sel:
                return self.views[by_sel[data]]
            raise Exception("RPC error 3: execution reverted")  # a V6 view on this curve
        return "0x"


class VibeCurveFactory(unittest.TestCase):
    """vibe/vibe's curve factory (the deployment its own config names): its events, its
    curve math over one whole curve life, and tracking one of its tokens."""

    def test_the_launch_names_token_curve_and_creator(self):
        live = VIBE_C["live"]["launch"]
        launch = vibe.decode_launch(live, VIBE_C["factory"])
        self.assertEqual((launch.token, launch.curve), (abi.topic_address(live["topics"][2]), VIBE_C["live"]["curve"]))
        self.assertEqual(launch.launcher, abi.word_address(abi.to_bytes(live["data"]), 0))
        self.assertEqual((launch.quote, launch.target, launch.name), (chain.NATIVE, 0, None))  # from the curve
        self.assertIsNone(vibe.decode_launch(live, "0x" + "11" * 20))  # anyone can emit the topic
        self.assertIsNone(vibe.decode_launch(dict(live, address="0x" + "11" * 20), VIBE_C["factory"]))

    def test_buys_and_sells_against_their_fees(self):
        trades = [vibe.decode_trade(lg) for lg in VIBE_C["first_trades"]]
        self.assertTrue(all(trades))
        for tr in trades:
            if isinstance(tr, vibe.Buy):
                self.assertEqual((tr.recipient, tr.tax_tokens, tr.first), (tr.caller, 0, False))
                self.assertEqual(round(tr.fee * 10_000 / tr.quote_paid), vibe.C_FEE_BPS)  # 1.25% of the gross
            else:
                self.assertEqual((tr.recipient, tr.tax_tokens, tr.tokens_net), (tr.seller, 0, tr.tokens))
                self.assertEqual(round(tr.fee * 10_000 / tr.quote_gross), vibe.C_FEE_BPS)
        first = vibe.decode_trade(VIBE_C["live"]["first_buy"][0])  # the factory calls it at launch
        self.assertTrue(first.first)
        self.assertEqual(first.caller, VIBE_C["factory"])
        self.assertEqual(first.tokens, vibe.decode_launch(VIBE_C["live"]["launch"], VIBE_C["factory"]).first_buy)

    def test_a_whole_curve_life_replays_to_its_target(self):
        """Every trade of one curve, from launch to graduation, through constant product over
        the curve's virtual reserves: each fits to a millionth and the curve ends at exactly
        its 5 ETH target with no tokens left."""
        st = vibe.State("t", chain.NATIVE, 5 * ETH, 0, vibe.C_CURVE_SUPPLY, 0, False, None,
                        virtual_quote0=C_VQ0, virtual_tokens=C_VT0 - vibe.C_CURVE_SUPPLY)
        self.assertEqual(st.k, C_VQ0 * C_VT0)
        self.assertEqual(C_VQ0, 5 * ETH * 2799 // 7931)  # the target * 279.9M / 793.1M
        x, y, last = C_VQ0, C_VT0, None
        for kind, *w in VIBE_C["replay"]:
            w = [int(v) for v in w]
            if kind == "b":
                gross, net, out, cf, pf = w
                self.assertEqual(gross - net, cf + pf)
                self.assertAlmostEqual(y - st.k // (x + net), out, delta=out // 10 ** 6)
                x, y = x + net, y - out
                tr = vibe.Buy("c", "a", "a", out, out, 0, net, gross - net, 125, False, 1, "0x", 0)
            else:
                tokens, gross, net, cf, pf = w
                self.assertAlmostEqual(x - st.k // (y + tokens), gross, delta=gross // 10 ** 6)
                x, y = x - gross, y + tokens
                tr = vibe.Sell("c", "a", "a", tokens, tokens, 0, gross, gross - net, 1, "0x", 0)
            price, last = vibe.after_trade(tr, st.k, st.phantom, st.target, 18, 18)
            self.assertIsNotNone(price)
        self.assertAlmostEqual((x - C_VQ0) / ETH, 5.0, places=12)
        self.assertLess(abs(y - (C_VT0 - vibe.C_CURVE_SUPPLY)), 10 ** 12)
        self.assertAlmostEqual(last, 1.0, places=9)

    def test_graduation_names_the_pool_with_this_factorys_hook(self):
        grad = next(lg for lg in VIBE_C["graduation"] if lg["topics"][0] == vibe.TOPIC_C_GRADUATED)
        init = next(lg for lg in VIBE_C["graduation"] if lg["topics"][0].startswith("0xdd466e67"))
        g = vibe.decode_graduated(grad)
        token = abi.topic_address(init["topics"][3])
        self.assertEqual(g.pool_id, init["topics"][1])
        self.assertEqual(g.pool_id, vibe.pool_id(token, chain.NATIVE, C_HOOK))
        self.assertAlmostEqual(g.quote_to_pool / ETH, 5.0, places=9)
        self.assertEqual(g.tokens_to_pool, 206_900_000 * ETH)

    def test_state_from_the_curves_own_views(self):
        st = run(vibe.state(CRpc(), VIBE_C["live"]["curve"]))
        v = {k: int(x, 16) for k, x in VIBE_C["live"]["views"].items()}
        self.assertEqual((st.quote, st.target, st.real_quote, st.complete, st.pool_id),
                         (chain.NATIVE, 5 * ETH, v["real"], False, None))
        self.assertEqual((st.phantom, st.extra_tokens), (C_VQ0, 279_900_000 * ETH))
        self.assertAlmostEqual(st.price(), v["virtual_quote"] / v["virtual_tokens"])
        self.assertAlmostEqual(st.progress, (vibe.C_CURVE_SUPPLY - st.tokens_left) / vibe.C_CURVE_SUPPLY)
        # the event-only model agrees with the views (curve_supply knows this shape)
        self.assertEqual(vibe.curve_supply(st.k, st.phantom), vibe.C_CURVE_SUPPLY)
        self.assertAlmostEqual(vibe.sold_share(st.k, st.phantom, st.phantom + st.real_quote), st.progress, places=6)
        self.assertIsNone(run(vibe.state(CRpc(c_views(target=7 * ETH)), VIBE_C["live"]["curve"])))  # unknown quote
        spcx = run(vibe.state(CRpc(c_views(target=82_740_374_439_909_720_323)), VIBE_C["live"]["curve"]))
        self.assertEqual(spcx.quote, "0x5a5398155d98374c0e26265ea3cb9818169c2739")
        self.assertTrue(run(vibe.state(CRpc(c_views(graduated=1)), VIBE_C["live"]["curve"])).complete)
        # full but not graduated yet: no pool to read, so not complete for the bot
        self.assertFalse(run(vibe.state(CRpc(c_views(complete=1)), VIBE_C["live"]["curve"])).complete)

    def test_track_finds_the_launch_in_ten_million_block_windows(self):
        from buybot import tokens
        rpc = CRpc()
        t, found = run(tokens.load_token(rpc, rpc.token))
        launch = vibe.decode_launch(rpc.launch, VIBE_C["factory"])
        self.assertEqual(found, [])
        self.assertEqual((t.kind, t.curve, t.deployer, t.pair, t.threshold, t.quote_kind),
                         ("vibe", rpc.curve, launch.launcher, chain.NATIVE, 5 * ETH, "eth"))
        self.assertEqual((t.curve_k, t.phantom, t.graduated), (C_VQ0 * C_VT0, C_VQ0, False))
        self.assertEqual(t.pool_id, vibe.pool_id(rpc.token, chain.NATIVE, C_HOOK))
        searched = [q for q in rpc.queries if q[2] == VIBE_C["factory"]]
        self.assertEqual(len(searched), 3)
        self.assertTrue(all(to - frm < tokens.C_LAUNCH_WINDOW for frm, to, _ in searched))

    def test_a_v6_token_is_not_searched_for_in_the_curve_factory(self):
        from buybot import tokens
        curve_factory = chain.vibe_contracts(chain.TESTNET_CHAIN_ID)[1]
        rpc = VibeRpc()
        self.assertIsNone(run(tokens.find_vibe_launch(rpc, VIBE["curve"]["token"], curve_factory)))


class VibeCurveWatcher(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Db(os.path.join(self.dir.name, "t.db"))
        self.token = c_token()
        self.db.upsert_token(self.token)
        self.db.ensure_chat(-1)
        self.db.update_chat(-1, token=self.token.address, sells=1, min_usd=0)

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def test_trades_and_the_graduation_are_posted(self):
        unique = {(lg["transactionHash"], lg["logIndex"]): lg
                  for lg in VIBE_C["first_trades"] + VIBE_C["completing"] + VIBE_C["graduation"]}
        logs = sorted(unique.values(), key=lambda lg: (int(lg["blockNumber"], 16), int(lg["logIndex"], 16)))
        blocks = [int(lg["blockNumber"], 16) for lg in logs]
        self.db.set_last_block(min(blocks) - 1)
        w = Watcher(VibeFakeRpc(max(blocks), logs), self.db, FakeEthUsd(), max_range=10 ** 8, max_catchup=10 ** 8)
        w._phase_checked = time.time()
        run(w.tick())
        rows = [r for r in self.db.pending_outbox() if r.kind in ("buy", "sell", "grad")]
        trades = [vibe.decode_trade(lg) for lg in logs if lg["topics"][0] in vibe.TRADE_TOPICS]
        kinds = [r.kind for r in rows]
        self.assertEqual((kinds.count("buy"), kinds.count("sell"), kinds[-1]),
                         (sum(isinstance(t, vibe.Buy) for t in trades),
                          sum(isinstance(t, vibe.Sell) for t in trades), "grad"))
        buys = [r.payload for r in rows if r.kind == "buy"]
        for p, tr in zip(buys, [t for t in trades if isinstance(t, vibe.Buy)]):
            self.assertEqual(p["buyer"], tr.recipient)
            self.assertAlmostEqual(p["quote"], tr.quote_paid / ETH)
            self.assertAlmostEqual(p["tokens"], tr.tokens / ETH)
            self.assertEqual((p["tax_tokens"], p["launchpad"], p["venue"]), (0, "vibe/vibe", "curve"))
        self.assertAlmostEqual(buys[-1]["progress"], 1.0, places=6)  # the buy that filled the curve
        sell = next(r.payload for r in rows if r.kind == "sell")
        st = next(t for t in trades if isinstance(t, vibe.Sell))
        self.assertEqual(sell["buyer"], st.seller)
        self.assertAlmostEqual(sell["quote"], st.quote_received / ETH)
        grad = rows[-1].payload
        self.assertAlmostEqual(grad["raised"], 5.0, places=9)
        self.assertEqual(grad["pool_id"], self.token.pool_id)
        self.assertTrue(self.db.get_token(self.token.address).graduated)


# vibe/vibe's read API: a V6 launch, its search item, a curve-factory launch and its candles.
VIBE_API = json.loads((Path(__file__).parent / "fixtures" / "vibe_api_testnet.json").read_text(encoding="utf-8"))


def png(w, h, mode="RGB") -> bytes:
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new(mode, (w, h), (10, 200, 120)).save(out, "PNG")
    return out.getvalue()


@needs("aiohttp", "PIL")
class Promo(unittest.TestCase):
    """/promo: untrusted texts and logos, the API's facts, the spec, cache keys, limits, the queue."""

    def setUp(self):
        from buybot import promo
        self.p = promo

    def test_texts_lose_what_cannot_be_drawn_safely(self):
        c = self.p.clean
        self.assertEqual(c("Silverhand Buys", 40), "Silverhand Buys")
        self.assertEqual(c("\U0001f438 Pepe \U0001f680", 40), "Pepe")             # emoji
        self.assertEqual(c("‮evil‬ name​", 40), "evil name")         # bidi, zero width
        self.assertEqual(c("שלום Coin", 40), "Coin")            # RTL: no shaping here
        self.assertEqual(c("مرحبا", 40), "")
        self.assertEqual(c("ＦＵＬＬ\nline\ttwo", 40), "FULL line two")  # NFKC, one line
        self.assertEqual(c("x" * 500, 40), "x" * 40)
        self.assertEqual(c(None, 40), "")
        self.assertEqual(self.p.clean_symbol("$DO GE\U0001f680"), "DOGE")

    def test_logo_checks(self):
        ok = self.p.open_logo(png(64, 32, "RGBA"))
        self.assertEqual((ok.mode, ok.size), ("RGBA", (64, 32)))
        self.assertEqual(self.p.open_logo(png(900, 900)).size, (512, 512))   # kept at most 512 px
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        self.assertIsNone(self.p.open_logo(svg))
        self.assertIsNone(self.p.open_logo(png(3000, 4)))                    # a side over 2048 px
        self.assertIsNone(self.p.open_logo(b"\x89PNG\r\n\x1a\n" + b"\0" * 100))  # broken
        self.assertIsNone(self.p.open_logo(b"GIF89a" + b"\0" * 100))         # not on the list
        self.assertIsNone(self.p.open_logo(png(8, 8) + b"\0" * self.p.LOGO_MAX))  # too many bytes
        self.assertIsNone(self.p.open_logo(None))
        # a "bomb": a tiny file that claims a huge picture is refused before decoding
        import struct
        import zlib
        bomb = bytearray(png(1, 1))
        bomb[16:24] = struct.pack(">II", 60000, 60000)
        bomb[29:33] = struct.pack(">I", zlib.crc32(bytes(bomb[12:29])))
        self.assertIsNone(self.p.open_logo(bytes(bomb)))

    def test_logo_only_from_vibe_media_by_ipfs_id(self):
        cid = "bafkreicwr5vqkle4qspblzcj4g7ibmtxwc37ixiqe2ruqyovcikktzgp4m"
        self.assertEqual(self.p.logo_urls(f"ipfs://{cid}"),
                         [f"https://testnet.vibevibe.fun/media/{cid}.sq410.webp",
                          f"https://testnet.vibevibe.fun/media/{cid}.thumb.webp"])
        for bad in ("https://evil.example/x.png", "ipfs://../../etc", f"ipfs://{cid}/x", "ipfs://ABC123XYZ",
                    "data:image/svg+xml,<svg/>", None, 42):
            self.assertEqual(self.p.logo_urls(bad), [], bad)

    def test_api_facts(self):
        api = VIBE_API
        shbuys = api["v6_launch"]["launch"]["tokenAddress"]
        f = self.p.api_facts(None, api["v6_launch"], api["v6_search"]["items"][0], None, shbuys.upper().replace("0X", "0x"))
        self.assertEqual((f["holders"], f["buys_24h"]), (5, 7))
        self.assertTrue(f["image"].startswith("ipfs://bafkrei"))
        kyle = api["curve_launch"]["tokenAddress"]
        f = self.p.api_facts(api["curve_launch"], None, None, api["curve_candles"], kyle)
        self.assertEqual((f["holders"], f["buys_24h"]), (1118, 515))
        # an answer about another token, or with nonsense counts, gives nothing
        f = self.p.api_facts(api["curve_launch"], None, None, api["curve_candles"], shbuys)
        self.assertEqual(f, {"holders": None, "buys_24h": None, "image": None})
        bad = dict(api["v6_search"]["items"][0], market={"holderCount": -3, "buyCount24h": "lots"})
        f = self.p.api_facts(None, None, bad, None, shbuys)
        self.assertEqual((f["holders"], f["buys_24h"]), (None, None))

    def spec(self, token=None, state=None, rate=1500.0, kind="ethpool:0xq:0xp"):
        token = token or fixture_token(kind="vibe", quote_kind=kind, quote_symbol="AAPL", pair="0x" + "44" * 20,
                                       symbol="\U0001f680SH<b>", name="Silver <i>hand</i>")
        state = state or vibe.State(token=TOKEN, quote=token.pair, target=4 * ETH, real_quote=ETH, tokens_left=600 * 10 ** 24,
                                    tokens_sold=200 * 10 ** 24, complete=False, pool_id=None)

        class Rates:
            async def get(self, k):
                return rate if k == kind else None

        async def fake_state(rpc, curve, factory=None):
            return state
        real = vibe.state
        vibe.state = fake_state
        try:
            return run(self.p.gather(None, None, Rates(), TOKEN, token=token, bot="@b"))
        finally:
            vibe.state = real

    def test_spec_from_the_chain(self):
        spec, logo = self.spec()
        self.assertIsNone(logo)
        self.assertEqual((spec["symbol"], spec["name"], spec["pair"]), ("SH<b>", "Silver <i>hand</i>", "AAPL"))
        self.assertAlmostEqual(spec["progress"], 0.25)
        # a V6 curve: its % is the quote raised of the target, as vibe/vibe's site shows it
        self.assertEqual((spec["raised"], spec["target"], spec["sold"]), (1.0, 4.0, None))
        # a curve-factory curve (it reports its virtual tokens): the curve's tokens sold
        c_state = vibe.State(TOKEN, "0x" + "44" * 20, 4 * ETH, ETH, 600 * 10 ** 24, 200 * 10 ** 24, False, None,
                             virtual_quote0=ETH, virtual_tokens=100 * 10 ** 24)
        c_spec, _ = self.spec(state=c_state)
        self.assertEqual((c_spec["sold"], c_spec["curve_tokens"], c_spec["raised"]), (200e6, 800e6, None))
        st = vibe.State(TOKEN, "0x" + "44" * 20, 4 * ETH, ETH, 600 * 10 ** 24, 200 * 10 ** 24, False, None)
        self.assertAlmostEqual(spec["mc_quote"], st.price() * 10 ** 9)
        self.assertAlmostEqual(spec["mc_usd"], spec["mc_quote"] * 1500)
        self.assertTrue(spec["mc_approx"])                       # dollars from a pool price
        self.assertIsNone(spec["holders"])                       # no API: no tile, no guess
        spec, _ = self.spec(rate=None)
        self.assertIsNone(spec["mc_usd"])
        with self.assertRaises(self.p.PromoError):
            self.spec(token=fixture_token(kind="pons"))

    def test_caption_escapes_and_states_facts_only(self):
        spec, _ = self.spec()
        spec.update(holders=1234, buys_24h=56)
        text = self.p.caption(spec, "en", now=0)
        self.assertIn("$SH&lt;b&gt;", text)
        self.assertNotIn("<i>hand", text)
        self.assertIn("25.0% of the bonding curve filled, 1,234 holders", text)
        self.assertIn(f"CA {TOKEN}", text)
        self.assertIn("00:00 UTC", text)
        for word in ("price", "moon", "x100", "partner", "endorse", "guarantee"):
            self.assertNotIn(word, text.lower())

    def test_cache_key_follows_the_facts_drawn(self):
        spec, _ = self.spec()
        key = self.p.cache_key(spec, None)
        self.assertEqual(key, self.p.cache_key(dict(spec, mc_usd=spec["mc_usd"] * 1.0001), None))
        self.assertNotEqual(key, self.p.cache_key(dict(spec, progress=spec["progress"] + 0.01), None))
        self.assertNotEqual(key, self.p.cache_key(dict(spec, holders=7), None))
        self.assertNotEqual(key, self.p.cache_key(spec, object()))

    def test_limits(self):
        lim = self.p.Limiter(((60, 1), (3600, 3)))
        self.assertEqual(lim.allow("u", 0), 0)
        self.assertAlmostEqual(lim.allow("u", 10), 50)
        self.assertEqual(lim.allow("v", 10), 0)          # someone else
        self.assertEqual(lim.allow("u", 61), 0)
        self.assertEqual(lim.allow("u", 130), 0)
        self.assertAlmostEqual(lim.allow("u", 200), 3400)  # three in the hour
        self.assertEqual(lim.allow("u", 3601), 0)

    def test_cache_and_queue(self):
        promos = self.p.Promos()
        promos.keep_video("k", "file1")
        self.assertEqual(promos.cached_video("k"), "file1")
        promos.videos["k"] = (time.time() - self.p.CACHE_TTL - 1, "file1")
        self.assertIsNone(promos.cached_video("k"))
        promos.waiting = self.p.MAX_WAITING
        with self.assertRaises(self.p.PromoError) as e:
            run(promos.render({}, None))
        self.assertEqual(e.exception.key, "promo_busy")

    def test_render_is_one_at_a_time_and_after_cards(self):
        from buybot import heavy, promo_video
        log, real = [], promo_video.render

        def fake(spec, logo, style="dashboard"):
            log.append(spec["n"])
            time.sleep(0.05)
            return b"mp4"

        async def go():
            promos = self.p.Promos()
            async with heavy.slot():  # a buy card is being drawn: the promo waits for it
                task = asyncio.ensure_future(promos.render({"n": 1}, None))
                await asyncio.sleep(0.05)
                self.assertEqual(log, [])
            return await asyncio.gather(task, promos.render({"n": 2}, None))
        promo_video.render = fake
        try:
            self.assertEqual(run(go()), [b"mp4", b"mp4"])
        finally:
            promo_video.render = real
        self.assertEqual(log, [1, 2])

    def test_a_small_render(self):
        from buybot import chart, promo_video
        if not chart.find_ffmpeg():
            self.skipTest("no ffmpeg")
        spec, _ = self.spec()
        spec.update(holders=5, buys_24h=7, testnet=True, network="Robinhood Chain testnet")
        data = promo_video.render(spec, self.p.open_logo(png(64, 64)), size=(160, 200), fps=6, seconds=1.0)
        self.assertEqual(data[4:8], b"ftyp")
        for t in (0, 2.5, 4.0, 6.5):  # every scene draws, graduated too
            promo_video.still(dict(spec, graduated=True), None, t, size=(160, 200))
        data = promo_video.render(spec, None, size=(160, 200), fps=4, seconds=1.0, style="terminal")
        self.assertEqual(data[4:8], b"ftyp")

    def test_every_style_draws_every_scene(self):
        from buybot import promo_styles, promo_video
        self.assertEqual(tuple(promo_styles.STYLES), self.p.STYLES)  # the bot's list, without Pillow
        self.assertEqual(set(promo_styles.NAMES), set(self.p.STYLES))
        spec, _ = self.spec()
        spec.update(holders=5, buys_24h=7, testnet=True, network="Robinhood Chain testnet", bot="@b")
        sparse = dict(spec, holders=None, buys_24h=None, mc_usd=None, mc_quote=None, raised=None, testnet=False)
        long = dict(spec, symbol="W" * 16, name="W" * 40, pair="WWWWWWWWWWWW", mc_usd=123456789012.0)
        logo = self.p.open_logo(png(64, 64))
        for style in self.p.STYLES:
            for s in (spec, dict(spec, graduated=True, progress=1.0, raised=None), sparse, long):
                v = promo_video.video(s, logo, style, size=(160, 200))
                self.assertIsInstance(v, promo_styles.STYLES[style])
                for t in (0, 1.2, 2.5, 3.3, 4.4, 5.5, 6.2, 7.3):
                    v.frame(t).close()
        self.assertIsInstance(promo_video.video(spec, None, "nonsense", size=(160, 200)), promo_video.Video)


if __name__ == "__main__":
    unittest.main(verbosity=1)
