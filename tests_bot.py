"""Offline smoke test of the Telegram layer: real aiogram dispatcher, fake Telegram API.

Needs the requirements installed (pip install -r requirements.txt); no network,
no bot token. Run: python3 tests_bot.py
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import (BotDescription, BotShortDescription, CallbackQuery, Chat, ChatMemberLeft,
                           ChatMemberMember, ChatMemberOwner, ChatMemberUpdated, Message, MessageEntity,
                           MessageOriginChannel, Update, User,
                           Animation, Video)

from buybot import abi, chain, profile, render
from buybot.bot import App, build_router
from buybot.config import Config
from buybot.db import DEFAULT_MIN_USD, Db
from buybot.sender import Sender

FIX = json.loads((Path(__file__).parent / "fixtures" / "pons_v2_mainnet.json").read_text())
TOKEN = FIX["launch"]["token"]
DEX_TOKEN = "0x" + "d1" * 20   # sorts after WETH: token1 of its pool
DEX_POOL = "0x" + "90" * 20
GROUP, CHANNEL = -1001, -1002
ADMIN, MEMBER, OWNER = 10, 11, 99
BOT_ID = 42


def now():
    return datetime.now(timezone.utc)


def _transfer(frm, to, value, block):
    return {"address": TOKEN, "blockNumber": hex(block), "logIndex": "0x0", "transactionHash": "0x" + format(block, "064x"),
            "data": "0x" + format(value, "064x"),
            "topics": [chain.TOPIC_TRANSFER, abi.address_topic(frm), abi.address_topic(to)]}


# TOKEN's Transfers: minted to its curve, a sniper's buy in the next block, a buy much later
TOKEN_HISTORY = [_transfer(abi.ZERO_ADDRESS, FIX["launch"]["curve"], 10 ** 27, 100),
                 _transfer(FIX["launch"]["curve"], "0x" + "51" * 20, 10 ** 24, 101),
                 _transfer(FIX["launch"]["curve"], "0x" + "52" * 20, 2 * 10 ** 24, 500)]


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail: dict[int, Exception] = {}
        self.bad_ids: set[str] = set()   # file_ids Telegram refuses (another bot's)
        self.profile: dict[tuple[str, str | None], str] = {}

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        chat_id = getattr(method, "chat_id", None)
        if chat_id in self.fail:
            raise self.fail[chat_id]
        if name == "SendAnimation" and isinstance(method.animation, str) and method.animation in self.bad_ids:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=method, message="Bad Request: wrong file identifier/HTTP URL specified")
        if name == "GetMe":
            return User(id=BOT_ID, is_bot=True, first_name="Bot", username="testbuybot")
        if name == "GetChatMember":
            user = User(id=method.user_id, is_bot=False, first_name="U")
            if method.user_id in (ADMIN, OWNER):
                return ChatMemberOwner(user=user, is_anonymous=False)
            return ChatMemberMember(user=user)
        if name == "GetMyDescription":
            return BotDescription(description=self.profile.get(("d", method.language_code), ""))
        if name == "GetMyShortDescription":
            return BotShortDescription(short_description=self.profile.get(("s", method.language_code), ""))
        if name == "SetMyDescription":
            self.profile[("d", method.language_code)] = method.description
        if name == "SetMyShortDescription":
            self.profile[("s", method.language_code)] = method.short_description
        if name == "SendMediaGroup":  # /promo's sample clips: one message each
            if "media_group" in self.fail:
                raise self.fail["media_group"]
            return [Message(message_id=len(self.calls) * 10 + i, date=now(), chat=Chat(id=chat_id or 1, type="private"),
                            video=Video(file_id=f"sample{i}", file_unique_id=f"s{i}", width=432, height=540,
                                        duration=7)) for i, _ in enumerate(method.media)]
        if name == "SendVideo":  # /promo: Telegram's file_id is what the cache keeps
            return Message(message_id=len(self.calls), date=now(), chat=Chat(id=chat_id or 1, type="private"),
                           video=Video(file_id=f"video{len(self.calls)}", file_unique_id="u", width=720,
                                       height=900, duration=7))
        if name.startswith("Send") or name == "EditMessageText":
            return Message(message_id=len(self.calls), date=now(),
                           chat=Chat(id=chat_id or 1, type="private"))
        return True

    def sent(self, name=None):
        return [c for c in self.calls if name is None or type(c).__name__ == name]


class FakeRpc:
    """Answers the Pons factory for TOKEN, ERC-20 reads for any address, and, for
    DEX_TOKEN, one Uniswap v3 pool against WETH, 50 WETH deep."""
    calls = errors = 0

    async def block_number(self):
        return 1000

    async def get_logs(self, frm, to, address=None, topics=None):
        from buybot import pools
        if address == TOKEN and topics and topics[0] == chain.TOPIC_TRANSFER:  # TOKEN's history, for /scan
            return [lg for lg in TOKEN_HISTORY if frm <= int(lg["blockNumber"], 16) <= to
                    and (len(topics) < 2 or lg["topics"][1] == topics[1])]
        if topics and topics[0] == pools.TOPIC_V3_POOL_CREATED and topics[1] == abi.address_topic(DEX_TOKEN) \
                and address == next(iter(pools.V3_FACTORIES)):  # each factory answers for its own pools
            return [{"address": next(iter(pools.V3_FACTORIES)), "blockNumber": "0x1", "logIndex": "0x0",
                     "transactionHash": "0x" + "ee" * 32, "data": "0x" + format(60, "064x") + abi.encode_address(DEX_POOL),
                     "topics": [pools.TOPIC_V3_POOL_CREATED, abi.address_topic(DEX_TOKEN),
                                abi.address_topic("0x0bd7d308f8e1639fab988df18a8011f41eacad73"),
                                "0x" + format(3000, "064x")]}]
        return []

    async def eth_call(self, to, data, block="latest"):
        from buybot import pools
        if data.startswith(pools.SEL_V3_SLOT0):  # price 1: the quote depth is the liquidity
            return "0x" + format(2 ** 96, "064x") + "00" * 32 * 6
        if data.startswith(pools.SEL_V3_LIQUIDITY):
            return "0x" + format(50 * 10 ** 18, "064x")
        if data.startswith(chain.SEL_BALANCE_OF):
            return "0x" + format(50 * 10 ** 18, "064x")
        sel = data[:10]
        q = abi.word_uint(abi.to_bytes(FIX["launch"]["get_reserves_return"]), 0)
        string = lambda s: "0x" + format(32, "064x") + format(len(s), "064x") + s.encode().hex().ljust(64, "0")  # noqa: E731
        answers = {
            chain.SEL_GET_LAUNCHED_TOKEN: FIX["launch"]["get_launched_token_return"],
            chain.SEL_DECIMALS: "0x" + format(18, "064x"),
            chain.SEL_TOTAL_SUPPLY: "0x" + format(10 ** 27, "064x"),
            chain.SEL_NAME: string("Test Token"),
            chain.SEL_SYMBOL: string("TST"),
            chain.SEL_GET_RESERVES: FIX["launch"]["get_reserves_return"],
            chain.SEL_REAL_QUOTE_RESERVE: "0x" + format(q - 168 * 10 ** 16, "064x"),
        }
        if sel == chain.SEL_GET_LAUNCHED_TOKEN and data[-40:] != TOKEN[2:]:
            return "0x" + "00" * 32 * 15
        return answers[sel]


class FakeEthUsd:
    current = 3000.0

    async def get(self):
        return 3000.0


def user(uid):
    return User(id=uid, is_bot=False, first_name=f"u{uid}", username=f"u{uid}")


def message(chat_id, uid, text, chat_type="supergroup", **extra):
    entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] \
        if text.startswith("/") else None
    return Message(message_id=int(time.time() * 1000) % 10 ** 9, date=now(),
                   chat=Chat(id=chat_id, type=chat_type, title="Chat" if chat_type != "private" else None),
                   from_user=user(uid), text=text, entities=entities, **extra)


class BotFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Db(os.path.join(self.dir.name, "t.db"))
        self.session = FakeSession()
        self.bot = Bot("42:TEST", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.cfg = Config(bot_token="x", owner_ids=frozenset({OWNER}), chain_id=46630,
                          trade_url="https://gmgn.ai/robinhood/token/REF_{token}")
        self.app = App(cfg=self.cfg, db=self.db, rpc=FakeRpc(), eth_usd=FakeEthUsd(),
                       username="testbuybot")
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.app))
        self.sender = Sender(self.bot, self.db, trade_url=self.cfg.trade_url,
                             explorer="https://explorer", chat_interval=0)
        self.uid = 0

    async def asyncTearDown(self):
        self.db.close()
        self.dir.cleanup()

    async def feed(self, **kind):
        self.uid += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.uid, **kind))

    def last_text(self, name=None):
        """The text of the last message sent (a post's caption when it came as a card)."""
        if name is None:
            calls = [c for c in self.session.calls if type(c).__name__ in ("SendMessage", "SendAnimation", "SendPhoto")]
        else:
            calls = self.session.sent(name)
        if not calls:
            return None
        return calls[-1].text if type(calls[-1]).__name__ == "SendMessage" else calls[-1].caption

    async def join(self, chat_id, chat_type="supergroup"):
        await self.feed(my_chat_member=ChatMemberUpdated(
            chat=Chat(id=chat_id, type=chat_type, title="Chat"), from_user=user(ADMIN), date=now(),
            old_chat_member=ChatMemberLeft(user=User(id=BOT_ID, is_bot=True, first_name="B")),
            new_chat_member=ChatMemberMember(user=User(id=BOT_ID, is_bot=True, first_name="B"))))

    async def track(self):
        await self.join(GROUP)
        await self.feed(message=message(GROUP, ADMIN, f"/track {TOKEN}"))

    def buy_row(self, n, quote=0.1, usd=300.0):
        p = {"token": TOKEN, "symbol": "TST", "name": "Test Token", "quote_symbol": "ETH",
             "quote": quote, "usd": usd, "tokens": 1e6, "buyer": "0x" + "12" * 20, "dev": False,
             "tx": "0x" + "ab" * 32, "mc_quote": 5.0, "mc_usd": 15000.0, "progress": 0.2,
             "venue": "curve"}
        self.db.add_outbox(GROUP, "buy", f"buy:{n}", p)

    async def test_join_track_and_settings(self):
        await self.join(GROUP)
        self.assertIn("Thanks for adding me", self.last_text())
        await self.feed(message=message(GROUP, ADMIN, f"/track {TOKEN}"))
        self.assertIn("Tracking started: posting buys of <b>TST</b>", self.last_text())
        self.assertEqual(self.db.get_chat(GROUP).token, TOKEN)
        t = self.db.get_token(TOKEN)
        self.assertEqual(t.phantom, 168 * 10 ** 16)
        self.assertEqual(t.pool_id, chain.pool_id(TOKEN))

        before = len(self.session.calls)
        await self.feed(message=message(GROUP, MEMBER, "/minbuy 5"))
        self.assertEqual(self.db.get_chat(GROUP).min_usd, DEFAULT_MIN_USD)  # non-admins are ignored
        self.assertEqual(len(self.session.sent("SendMessage")),
                         len([c for c in self.session.calls[:before] if type(c).__name__ == "SendMessage"]))

        await self.feed(message=message(GROUP, ADMIN, "/minbuy 50"))
        self.assertEqual(self.db.get_chat(GROUP).min_usd, 50)
        await self.feed(message=message(GROUP, ADMIN, "/emoji 🚀"))
        await self.feed(message=message(GROUP, ADMIN, "/emoji abc"))
        self.assertEqual(self.db.get_chat(GROUP).emoji, "🚀")
        await self.feed(message=message(GROUP, ADMIN, "/settings"))
        self.assertIn("Min buy: $50.00", self.last_text())

    async def test_a_token_without_pools(self):
        await self.join(GROUP)
        await self.feed(message=message(GROUP, ADMIN, "/track 0x" + "12" * 20))
        self.assertIn("has no pool I can read yet", self.last_text())
        self.assertIsNone(self.db.get_chat(GROUP).token)

    async def test_any_token_with_a_uniswap_pool(self):
        await self.join(GROUP)
        await self.feed(message=message(GROUP, ADMIN, f"/track {DEX_TOKEN}"))
        text = self.last_text()
        self.assertIn("Tracking started", text)
        self.assertIn("🦄 Pools: Uniswap v3 · WETH ($300K)", text)  # 2 × 50 WETH at $3,000
        token = self.db.get_token(DEX_TOKEN)
        self.assertEqual((token.kind, token.quote_symbol, token.quote_kind), ("dex", "WETH", "eth"))
        self.assertEqual([p.id for p in self.db.pools_of(DEX_TOKEN)], [DEX_POOL])
        await self.feed(message=message(GROUP, ADMIN, "/settings"))
        self.assertIn("(Uniswap v3 · WETH)", self.last_text())

    async def test_preview_and_real_posts(self):
        await self.track()
        await self.feed(message=message(GROUP, ADMIN, "/test"))
        self.assertIn("TEST POST", self.last_text())
        self.assertIn("NFA · DYOR", self.last_text())
        self.assertNotIn("via @", self.last_text())

        self.buy_row(1)
        await self.sender.flush()
        text = self.last_text()
        self.assertIn("BUY</b> · <b>$TST", text)
        self.assertIn("$300", text)
        for promo in ("via @", "Advertise", "Free buy alerts", "start=ads"):
            self.assertNotIn(promo, text)  # every chat: no ad, no promotion
        markup = [c for c in self.session.calls if type(c).__name__ in ("SendMessage", "SendAnimation")][-1].reply_markup
        self.assertEqual(markup.inline_keyboard[0][0].url, f"https://gmgn.ai/robinhood/token/REF_{TOKEN}")
        self.assertEqual(self.db.outbox_size(), 0)

        for i, q in enumerate((0.1, 1.0, 0.2)):
            self.buy_row(10 + i, quote=q, usd=q * 3000)
        await self.sender.flush()
        self.assertIn("2 more buys", self.last_text())
        self.assertIn("<b>$3,000</b> · 1 ETH", self.last_text())
        self.assertEqual(self.db.outbox_size(), 0)

    async def test_kicked_bot_stops_posting(self):
        await self.track()
        self.session.fail[GROUP] = TelegramForbiddenError(method=None, message="Forbidden: bot was kicked")
        self.buy_row(1)
        await self.sender.flush()
        self.assertFalse(self.db.get_chat(GROUP).active)
        self.assertEqual(self.db.outbox_size(), 0)

    async def test_media_for_every_chat(self):
        await self.track()
        gif = message(GROUP, ADMIN, "x").model_copy(update={
            "text": None, "entities": None,
            "animation": Animation(file_id="GIF1", file_unique_id="u", width=1, height=1, duration=1)})
        await self.feed(message=message(GROUP, ADMIN, "/media", reply_to_message=gif))
        self.assertEqual(self.db.get_chat(GROUP).media_file_id, "GIF1")
        self.buy_row(1)
        await self.sender.flush()
        caption = self.last_text("SendAnimation")
        self.assertIn("BUY</b> · <b>$TST", caption)
        self.assertNotIn("via @", caption)
        await self.feed(message=message(GROUP, ADMIN, "/settings"))
        self.assertIn("Media: animation", self.last_text())
        self.assertNotIn("(Pro)", self.last_text())
        await self.feed(message=message(GROUP, ADMIN, "/media off"))
        self.assertIsNone(self.db.get_chat(GROUP).media_file_id)

    async def test_nothing_is_sold(self):
        """Old commands, deep links and buttons of the removed paid plan lead nowhere paid."""
        await self.track()
        replies = len(self.session.sent("SendMessage"))
        await self.feed(message=message(GROUP, ADMIN, "/pro"))
        self.assertEqual(len(self.session.sent("SendMessage")), replies)  # not a command any more
        self.db.set_user_lang(ADMIN, "en")
        for text in ("/advertise 2 https://t.me/x Join", "/start ads", f"/start pro_{GROUP}"):
            await self.feed(message=message(ADMIN, ADMIN, text, chat_type="private"))
        for data in ("m:pro", "m:ads", f"pro:{GROUP}:1", "ad:ok:1", f"c:pro:{GROUP}", f"a:grant:{GROUP}:0"):
            await self.callback(ADMIN, data)
        self.assertEqual(self.session.sent("SendInvoice"), [])
        self.assertIn("buy bot", self.last_caption())  # /start with an old argument opens the menu
        self.assertEqual(self.session.sent("AnswerCallbackQuery")[-1].show_alert or False, True)  # a:… is the owner's

    async def test_support_reaches_the_owners_once_in_a_while(self):
        self.db.set_user_lang(MEMBER, "en")
        await self.feed(message=message(MEMBER, MEMBER, "/support my posts stopped", chat_type="private"))
        to_owner = [c for c in self.session.sent("SendMessage") if c.chat_id == OWNER]
        self.assertIn("my posts stopped", to_owner[-1].text)
        self.assertIn("Sent", self.last_text())
        await self.feed(message=message(MEMBER, MEMBER, "/support again", chat_type="private"))
        self.assertIn("wait", self.last_text())
        self.assertEqual(len([c for c in self.session.sent("SendMessage") if c.chat_id == OWNER]), len(to_owner))

        # the owner answers by replying; the answer goes back to the sender
        forwarded = Message(message_id=500, date=now(), chat=Chat(id=OWNER, type="private"),
                            text=f"🆘 /support from {MEMBER} (@u{MEMBER}):\nmy posts stopped")
        await self.feed(message=message(OWNER, OWNER, "Fixed, try /test", chat_type="private",
                                        reply_to_message=forwarded))
        copy = self.session.sent("CopyMessage")[-1]
        self.assertEqual((copy.chat_id, copy.from_chat_id), (MEMBER, OWNER))

    async def test_support_that_reaches_nobody_does_not_lock_the_sender(self):
        self.db.set_user_lang(MEMBER, "en")
        self.session.fail[OWNER] = TelegramForbiddenError(method=None, message="Forbidden: bot was blocked")
        await self.feed(message=message(MEMBER, MEMBER, "/support hello", chat_type="private"))
        self.assertIn("Couldn't deliver", self.last_text())
        del self.session.fail[OWNER]
        await self.feed(message=message(MEMBER, MEMBER, "/support hello", chat_type="private"))
        self.assertIn("Sent", self.last_text())

    async def test_channel_is_set_up_from_private_chat(self):
        await self.join(CHANNEL, chat_type="channel")
        dm = self.session.sent("SendMessage")[-1]
        self.assertEqual(dm.chat_id, ADMIN)
        self.assertIn(f"/channel {CHANNEL} track", dm.text)
        await self.feed(message=message(MEMBER, MEMBER, f"/channel {CHANNEL} track {TOKEN}",
                                        chat_type="private"))
        self.assertIsNone(self.db.get_chat(CHANNEL).token)
        await self.feed(message=message(ADMIN, ADMIN, f"/channel {CHANNEL} track {TOKEN}",
                                        chat_type="private"))
        self.assertEqual(self.db.get_chat(CHANNEL).token, TOKEN)
        self.assertIn("Tracking started", self.last_text())

        # plain commands in the private chat now apply to that channel, with a confirmation
        await self.feed(message=message(ADMIN, ADMIN, "/test", chat_type="private"))
        sample, confirm = self.session.sent("SendAnimation")[-1], self.session.sent("SendMessage")[-1]
        self.assertEqual((sample.chat_id, confirm.chat_id), (CHANNEL, ADMIN))
        self.assertIn("TEST POST", sample.caption)
        self.assertIn("Sample post sent", confirm.text)
        await self.feed(message=message(ADMIN, ADMIN, "/settings", chat_type="private"))
        self.assertIn("Token: <b>TST</b>", self.last_text())

    async def test_sells_on_and_off(self):
        await self.track()
        await self.feed(message=message(GROUP, ADMIN, "/sells on"))
        self.assertIn("Sells are on", self.last_text())
        self.assertTrue(self.db.get_chat(GROUP).sells)
        await self.feed(message=message(GROUP, ADMIN, "/settings"))
        self.assertIn("Sells: on", self.last_text())

        p = dict(render.sample_payload("TST", "ETH", 3000.0), token=TOKEN, tx="0x" + "cd" * 32)
        self.db.add_outbox(GROUP, "buy", "buy:1", p)
        self.db.add_outbox(GROUP, "sell", "sell:1", p)
        await self.sender.flush()
        self.assertIn("BUY</b> · <b>$TST", self.last_text())  # buys and sells are posted apiece
        await self.sender.flush()
        self.assertIn("SELL</b> · <b>$TST", self.last_text())

        await self.feed(message=message(GROUP, ADMIN, "/sells off"))
        self.db.add_outbox(GROUP, "sell", "sell:2", p)
        await self.sender.flush()
        self.assertEqual(self.db.outbox_size(), 0)  # dropped, not posted
        self.assertNotIn("Sell!", self.last_text())

    async def test_posts_come_with_a_chart(self):
        await self.track()
        t0 = int(time.time()) - 3600
        self.db.add_trades([(TOKEN, f"u{i}", t0 + i * 120, 1e-8 * (1 + (i % 7) / 10), 0.1, 1 if i % 3 else -1)
                            for i in range(30)])
        self.buy_row(1)
        await self.sender.flush()
        from buybot.chart import find_ffmpeg
        if find_ffmpeg():  # the default: an animated chart, an MP4 Telegram loops like a GIF
            anim = self.session.sent("SendAnimation")[-1]
            self.assertIn("BUY</b> · <b>$TST", anim.caption)
            self.assertEqual(anim.animation.data[4:8], b"ftyp")
            buttons = [b.callback_data for b in anim.reply_markup.inline_keyboard[0] if b.callback_data]
            self.assertEqual(buttons, [f"s:{TOKEN}", f"x:{TOKEN}"])

        await self.feed(message=message(GROUP, ADMIN, "/chart still"))
        self.assertIn("still picture", self.last_text())
        self.buy_row(2)
        await self.sender.flush()
        photo = self.session.sent("SendPhoto")[-1]
        self.assertIn("BUY</b> · <b>$TST", photo.caption)
        self.assertTrue(photo.photo.data.startswith(b"\x89PNG"))

        await self.feed(message=message(GROUP, ADMIN, "/chart off"))
        self.assertIn("Chart off", self.last_text())
        self.buy_row(3)
        await self.sender.flush()
        self.assertEqual(len(self.session.sent("SendPhoto")), 1)
        self.assertIn("BUY</b> · <b>$TST", self.last_text())

        # 📊 24h under a post: a pop-up for whoever taps it, nothing posted in the chat
        posts = len(self.session.sent("SendMessage"))
        await self.callback(MEMBER, f"s:{TOKEN}")
        popup = self.session.sent("AnswerCallbackQuery")[-1]
        self.assertTrue(popup.show_alert)
        self.assertIn("TST · last", popup.text)
        self.assertLessEqual(len(popup.text), 200)
        self.assertEqual(len(self.session.sent("SendMessage")), posts)

    async def test_forum_topic(self):
        from aiogram.exceptions import TelegramBadRequest
        await self.join(GROUP)
        await self.feed(message=message(GROUP, ADMIN, f"/track {TOKEN}", is_topic_message=True,
                                        message_thread_id=7))
        self.assertEqual(self.db.get_chat(GROUP).thread_id, 7)
        self.buy_row(1)
        await self.sender.flush()
        post = [c for c in self.session.calls if type(c).__name__.startswith("Send")][-1]
        self.assertEqual(post.message_thread_id, 7)

        # the topic was deleted: the next post goes to the chat itself
        self.session.fail[GROUP] = TelegramBadRequest(method=None, message="Bad Request: message thread not found")
        self.buy_row(2)
        await self.sender.flush()
        del self.session.fail[GROUP]
        self.assertIsNone(self.db.get_chat(GROUP).thread_id)
        self.assertTrue(self.db.get_chat(GROUP).active)
        await self.sender.flush()
        post = [c for c in self.session.calls if type(c).__name__.startswith("Send")][-1]
        self.assertIsNone(post.message_thread_id)

        # /track outside any topic posts to the chat itself
        await self.feed(message=message(GROUP, ADMIN, f"/track {TOKEN}"))
        self.assertIsNone(self.db.get_chat(GROUP).thread_id)

    async def test_chart_candles(self):
        from buybot import chart
        now = 10_000
        pts = [(now - 290, 1.0, 1, 1), (now - 250, 2.0, 1, 1), (now - 130, 1.5, 1, -1), (now - 10, 3.0, 2, 1)]
        self.assertEqual(chart.pick_interval(290), (15, "15s"))
        candles = chart.build_candles(pts, now, 60)
        # minute candles from 9660: each opens at the previous close
        self.assertEqual([(c.open, c.close) for c in candles if c.trades],
                         [(1.0, 1.0), (1.0, 2.0), (2.0, 1.5), (1.5, 3.0)])
        flat = [c for c in candles if not c.trades]
        self.assertTrue(flat and all(c.open == c.close for c in flat))  # gaps hold the last close
        png = chart.render(pts, symbol="TST", supply=1e9, usd_per_quote=3000, quote_symbol="ETH",
                           side=1, now=now)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertIsNone(chart.render(pts[:1], symbol="TST", supply=1e9, usd_per_quote=None,
                                       quote_symbol="ETH", now=now))

    async def test_channel_id_in_any_form_and_by_forward(self):
        await self.join(CHANNEL, chat_type="channel")
        # the id as apps show it, without -100 (here -1002 -> "2")
        await self.feed(message=message(ADMIN, ADMIN, f"/channel {str(CHANNEL)[4:]} track {TOKEN}",
                                        chat_type="private"))
        self.assertEqual(self.db.get_chat(CHANNEL).token, TOKEN)
        await self.feed(message=message(ADMIN, ADMIN, "/channel 777 settings", chat_type="private"))
        self.assertIn("forward me any post", self.last_text())

        self.db.set_dm_chat(ADMIN, GROUP)  # some other chat was picked before
        origin = MessageOriginChannel(date=now(), chat=Chat(id=CHANNEL, type="channel", title="Chat"),
                                      message_id=5)
        await self.feed(message=message(ADMIN, ADMIN, "a post", chat_type="private",
                                        forward_origin=origin))
        self.assertIn("Settings", self.last_text())
        self.assertEqual(self.db.dm_chat(ADMIN), CHANNEL)

    async def test_private_command_without_a_channel_explains(self):
        await self.feed(message=message(MEMBER, MEMBER, f"/track {TOKEN}", chat_type="private"))
        self.assertIn("works in your group or channel", self.last_text())

    async def test_commands_posted_in_a_channel(self):
        self.app.note_ttl = 0
        await self.join(CHANNEL, chat_type="channel")
        channel = Chat(id=CHANNEL, type="channel", title="Chat")
        text = f"/track {TOKEN}"
        post = Message(message_id=500, date=now(), chat=channel, sender_chat=channel, text=text,
                       entities=[MessageEntity(type="bot_command", offset=0, length=6)])
        await self.feed(channel_post=post)
        self.assertEqual(self.db.get_chat(CHANNEL).token, TOKEN)
        note = self.session.sent("SendMessage")[-1]
        self.assertEqual(note.chat_id, CHANNEL)
        self.assertIn("Tracking started", note.text)
        self.assertTrue(note.disable_notification)
        for _ in range(3):
            await asyncio.sleep(0)  # let the tidy-up task run
        deleted = self.session.sent("DeleteMessages")[-1]
        self.assertEqual(deleted.chat_id, CHANNEL)
        self.assertIn(500, deleted.message_ids)
        self.assertEqual(len(deleted.message_ids), 2)

    async def test_terms_link_the_source(self):
        self.app.cfg = Config(bot_token="x", owner_ids=frozenset(), source_url="https://example.org/src")
        await self.feed(message=message(MEMBER, MEMBER, "/terms", chat_type="private"))
        terms = self.last_text("SendAnimation")
        self.assertIn("Source code (AGPL-3.0): https://example.org/src", terms)
        self.assertNotIn("Stars", terms)
        self.assertIn("no ads", terms)

    async def test_scan_in_a_group(self):
        await self.track()
        replies = len(self.session.sent("SendMessage"))
        await self.feed(message=message(GROUP, MEMBER, "/scan"))
        self.assertEqual(len(self.session.sent("SendMessage")), replies)  # admins only
        await self.feed(message=message(GROUP, ADMIN, "/scan"))  # alone: the chat's own token
        text = self.last_text()
        self.assertIn("Scan · $TST</b> · Test Token", text)
        self.assertIn("🎯 Snipers: <b>1</b> wallet bought in the first 5 s", text)
        # the curve holds the rest and is not a holder; the dev holds what the chain says (balanceOf)
        self.assertIn("👥 Holders: <b>3</b>", text)
        self.assertNotIn(render.short_addr(FIX["launch"]["curve"]), text)
        self.assertIn(f"👨‍💻 Dev <a href=\"https://robinhoodchain.blockscout.com/address/{FIX['launch']['deployer']}\">",
                      text)  # the Pons deployer, from the factory's record
        self.assertIn("honeypot", text)
        await self.feed(message=message(GROUP, ADMIN, "/scan 0x" + "12" * 20))
        self.assertIn("No mint of this token", self.last_text())
        await self.feed(message=message(GROUP, ADMIN, "/scan nonsense"))
        self.assertIn("/scan <code>0x…</code>", self.last_text())

    async def test_scan_in_the_private_chat(self):
        self.db.set_user_lang(MEMBER, "en")
        await self.feed(message=message(MEMBER, MEMBER, f"/scan {TOKEN}", chat_type="private"))
        self.assertIn("Scan · $TST", self.last_text())
        await self.feed(message=message(MEMBER, MEMBER, f"/scan {DEX_TOKEN}", chat_type="private"))
        self.assertIn("One scan a minute", self.last_text())  # a new token costs requests
        await self.feed(message=message(MEMBER, MEMBER, f"/scan {TOKEN.upper().replace('0X', '0x')}",
                                        chat_type="private"))
        self.assertIn("Scan · $TST", self.last_text())  # the kept one costs nothing
        await self.feed(message=message(MEMBER, MEMBER, "/scan", chat_type="private"))
        self.assertIn("/scan <code>0x…</code>", self.last_text())  # no current chat

    async def tap_picker(self, uid, style, chat_id=None):
        """A style picked under the last /promo picker."""
        (chat, mid), = [k for k in self.app.promos.pickers if chat_id is None or k[0] == chat_id][-1:]
        pic = Message(message_id=mid, date=now(), chat=Chat(id=chat, type="private" if chat > 0 else "supergroup"),
                      caption="pick")
        self.uid += 1
        await self.feed(callback_query=CallbackQuery(id=str(self.uid), from_user=user(uid), chat_instance="c",
                                                     data=f"pr:{style}", message=pic))

    async def test_promo(self):
        from buybot import promo, promo_video
        self.db.set_user_lang(MEMBER, "en")
        # a Pons token is not a vibe/vibe launch (a style named: no picker)
        await self.feed(message=message(OWNER, OWNER, f"/promo {TOKEN} dashboard", chat_type="private"))
        self.assertIn("making its Dashboard video", self.last_text())  # an answer at once
        self.assertIn("works for vibe/vibe launches", self.session.sent("EditMessageText")[-1].text)
        await self.feed(message=message(MEMBER, MEMBER, "/promo", chat_type="private"))
        self.assertIn("/promo <code>0x…</code>", self.last_text())  # no current chat
        spec = {"token": TOKEN, "symbol": "TST", "name": "Test Token", "pair": "AAPL", "graduated": False,
                "progress": 0.25, "raised": 1.0, "target": 4.0, "mc_quote": 2.0, "mc_usd": 3000.0,
                "mc_approx": True, "holders": 12, "buys_24h": 3, "testnet": True}
        renders = []

        async def gather(rpc, session, rates, address, **kw):
            self.assertEqual(kw["bot"], "@testbuybot")
            return dict(spec), None

        def render(s, logo, style="dashboard"):
            renders.append(style)
            return b"fake mp4"
        real = promo.gather, promo_video.render
        promo.gather, promo_video.render = gather, render
        try:
            # no style: the picker, six sample clips in an album, then a button per style
            await self.feed(message=message(ADMIN, ADMIN, f"/promo {TOKEN}", chat_type="private"))
            album = self.session.sent("SendMediaGroup")[-1]
            self.assertEqual([m.caption for m in album.media], ["1 · Dashboard", "2 · Kinetic", "3 · Terminal",
                                                                 "4 · Ticket", "5 · Gauge", "6 · Lime"])
            pick = self.session.sent("SendMessage")[-1]
            self.assertIn("The clips above are samples", pick.text)
            labels = [b.text for row in pick.reply_markup.inline_keyboard for b in row]
            self.assertEqual(labels, ["1 · Dashboard", "2 · Kinetic", "3 · Terminal", "4 · Ticket", "5 · Gauge",
                                      "6 · Lime"])
            self.assertEqual(renders, [])
            await self.tap_picker(MEMBER, "terminal")  # someone else's picker: nothing is made
            self.assertEqual(renders, [])
            await self.tap_picker(ADMIN, "terminal")
            self.assertEqual(renders, ["terminal"])
            video = self.session.sent("SendVideo")[-1]
            self.assertIn("<b>$TST</b> on vibe/vibe", video.caption)
            self.assertIn("25.0% of the bonding curve filled, 12 holders", video.caption)
            self.assertIn("≈$3K", video.caption)
            self.assertEqual((video.width, video.height, video.supports_streaming), (640, 800, True))
            self.assertTrue(self.session.sent("DeleteMessage"))  # the "making it" note goes
            (_, video_id), = self.app.promos.videos.values()  # the file_id Telegram gave it
            # the same facts and style again, from someone else: the same video, not drawn again
            await self.feed(message=message(MEMBER, MEMBER, f"/promo {TOKEN} terminal", chat_type="private"))
            self.assertEqual(self.session.sent("SendVideo")[-1].video, video_id)
            self.assertEqual(renders, ["terminal"])
            await self.feed(message=message(MEMBER, MEMBER, f"/promo {TOKEN} lime", chat_type="private"))
            self.assertIn("Not so fast", self.last_text())  # one a minute per person
            # in a group: admins only, the chat's own token by default
            await self.track()
            albums = len(self.session.sent("SendMediaGroup"))
            await self.feed(message=message(GROUP, MEMBER, "/promo"))
            self.assertEqual(len(self.session.sent("SendMediaGroup")), albums)
            await self.feed(message=message(GROUP, ADMIN, "/promo"))
            self.assertEqual(len(self.session.sent("SendMediaGroup")), albums + 1)
            # the second time the clips go by the file_ids Telegram gave them
            self.assertEqual([m.media for m in self.session.sent("SendMediaGroup")[-1].media],
                             [f"sample{i}" for i in range(6)])
            # without the album (Telegram refuses it): one picture of all the styles, with the buttons
            from aiogram.exceptions import TelegramBadRequest
            self.session.fail["media_group"] = TelegramBadRequest(method=None, message="no")
            await self.feed(message=message(GROUP, ADMIN, "/promo"))
            self.assertEqual(len(self.session.sent("SendPhoto")[-1].reply_markup.inline_keyboard[0]), 3)
            del self.session.fail["media_group"]
            await self.feed(message=message(GROUP, ADMIN, "/promo terminal"))  # not an address
            self.assertIn("/promo <code>0x…</code>", self.last_text())
        finally:
            promo.gather, promo_video.render = real

    async def test_scan_button_under_a_post(self):
        await self.track()
        await self.callback(MEMBER, f"x:{TOKEN}")
        first = self.session.sent("AnswerCallbackQuery")[-1]
        self.assertTrue(first.show_alert)
        self.assertIn("Scanning $TST", first.text)
        await asyncio.gather(*self.app.tasks)  # the scan the tap started
        await self.callback(MEMBER, f"x:{TOKEN}")
        popup = self.session.sent("AnswerCallbackQuery")[-1]
        self.assertIn("🛡 $TST", popup.text)
        self.assertIn("Snipers: 1", popup.text)
        self.assertIn("Honeypot not checked", popup.text)
        self.assertLessEqual(len(popup.text), 200)
        await self.callback(MEMBER, "x:0x" + "12" * 20)  # a token the bot does not know: nothing
        self.assertIsNone(self.session.sent("AnswerCallbackQuery")[-1].text)

        self.db.set_user_lang(ADMIN, "en")
        await self.callback(ADMIN, f"c:scan:{GROUP}")  # from the chat's screen in the private chat
        self.assertIn("Scan · $TST", self.last_caption())

    async def test_dev_sell_posted_with_sells_off(self):
        await self.track()
        self.assertFalse(self.db.get_chat(GROUP).sells)
        p = dict(render.sample_payload("TST", "ETH", 3000.0), token=TOKEN, tx="0x" + "cd" * 32)
        self.db.add_outbox(GROUP, "sell", "sell:1", p)
        self.db.add_outbox(GROUP, "sell", "sell:2", p | {"dev": True, "dev_sell": True,
                                                       "holder": {"change": -0.5, "share": 0.02}})
        await self.sender.flush()
        text = self.last_text()
        self.assertTrue(text.startswith("🚨 <b>DEV SELL</b> · <b>$TST</b>"), text)
        self.assertIn("🚨 sold 50% of the bag · holds 2.00%", text)
        self.assertEqual(self.db.outbox_size(), 0)  # the other sell was dropped: sells are off

    def callback(self, uid, data, text=None):
        """A button press under a menu screen (an animation with a caption)."""
        screen = Message(message_id=77, date=now(), chat=Chat(id=uid, type="private"),
                         animation=Animation(file_id="A", file_unique_id="a", width=1, height=1, duration=1),
                         caption=text or "screen")
        self.uid += 1
        return self.feed(callback_query=CallbackQuery(id=str(self.uid), from_user=user(uid),
                                                      chat_instance="c", data=data, message=screen))

    def last_caption(self):
        edits = [c for c in self.session.calls
                 if type(c).__name__ in ("EditMessageMedia", "SendAnimation", "SendMessage")]
        c = edits[-1]
        return c.media.caption if type(c).__name__ == "EditMessageMedia" else getattr(c, "caption", None) or c.text

    async def test_first_start_asks_the_language(self):
        await self.feed(message=message(MEMBER, MEMBER, "/start", chat_type="private"))
        picker = self.session.sent("SendAnimation")[-1]
        self.assertIn("Choose your language", picker.caption)
        self.assertIn("Выберите язык", picker.caption)
        self.assertEqual([b.callback_data for b in picker.reply_markup.inline_keyboard[0]], ["lang:en", "lang:ru"])

        await self.callback(MEMBER, "lang:ru")
        self.assertEqual(self.db.user_lang(MEMBER), "ru")
        self.assertIn("бот покупок", self.last_caption())
        await self.feed(message=message(MEMBER, MEMBER, "/start", chat_type="private"))
        self.assertIn("бот покупок", self.last_caption())  # remembered: no picker again
        await self.feed(message=message(MEMBER, MEMBER, "/lang en", chat_type="private"))
        self.assertIn("buy bot", self.last_caption())

    async def test_a_refused_cached_animation_is_sent_as_the_file(self):
        """After a bot token change the cached file_ids are the old bot's, and Telegram
        refuses them (seen on the testnet bot): the screen drops the cached id and sends
        the file, instead of going out without its animation every time."""
        from buybot import i18n
        from buybot.bot import MENU_DIR
        saved, i18n.LANGS = i18n.LANGS, ("en",)
        try:
            key = f"anim:en:start:{(MENU_DIR / 'en' / 'start.mp4').stat().st_size}"
            self.db.set_meta(key, "old-bot-file-id")
            self.session.bad_ids.add("old-bot-file-id")
            await self.feed(message=message(MEMBER, MEMBER, "/start", chat_type="private"))
            sent = self.session.sent("SendAnimation")
            self.assertEqual(sent[-2].animation, "old-bot-file-id")
            self.assertNotIsInstance(sent[-1].animation, str)  # the file itself
            self.assertIn("buy bot", sent[-1].caption)
            self.assertNotEqual(self.db.get_meta(key), "old-bot-file-id")
        finally:
            i18n.LANGS = saved

    async def test_english_only_never_asks_or_switches(self):
        from buybot import i18n
        saved, i18n.LANGS = i18n.LANGS, ("en",)
        try:
            await self.feed(message=message(MEMBER, MEMBER, "/start", chat_type="private"))
            self.assertIn("buy bot", self.last_caption())
            self.assertNotIn("Выберите язык", self.last_caption())
            await self.feed(message=message(MEMBER, MEMBER, "/lang ru", chat_type="private"))
            self.assertIn("buy bot", self.last_caption())
            self.assertNotEqual(self.db.user_lang(MEMBER), "ru")
            menu = self.session.sent("SendAnimation")[-1].reply_markup.inline_keyboard
            self.assertNotIn("m:lang", [b.callback_data for row in menu for b in row])
        finally:
            i18n.LANGS = saved

    async def test_chat_language_changes_posts(self):
        await self.track()
        await self.feed(message=message(GROUP, ADMIN, "/lang ru"))
        self.assertIn("на русском", self.last_text())
        self.buy_row(1)
        await self.sender.flush()
        self.assertIn("ПОКУПКА</b> · <b>$TST", self.last_text())
        self.assertIn("NFA · DYOR", self.last_text())
        self.assertNotIn("Бесплатные уведомления", self.last_text())
        await self.feed(message=message(GROUP, ADMIN, "/settings"))
        self.assertIn("Мин. покупка", self.last_text())

    async def test_my_chats_and_their_buttons(self):
        await self.track()  # ADMIN added the bot to GROUP
        await self.join(CHANNEL, chat_type="channel")
        self.db.set_user_lang(ADMIN, "en")
        await self.feed(message=message(ADMIN, ADMIN, "/chats", chat_type="private"))
        text = self.last_caption()
        self.assertIn("My chats", text)
        self.assertIn("TST", text)
        self.assertIn("👥", text)
        self.assertIn("📢", text)
        # someone else sees none of them
        self.db.set_user_lang(MEMBER, "en")
        await self.feed(message=message(MEMBER, MEMBER, "/chats", chat_type="private"))
        self.assertIn("No chats yet", self.last_caption())

        await self.callback(ADMIN, f"m:chat:{GROUP}")
        self.assertIn("Settings", self.last_caption())
        buttons = [b.callback_data for c in self.session.calls if type(c).__name__ == "EditMessageMedia"
                   for row in (c.reply_markup.inline_keyboard if c.reply_markup else []) for b in row]
        self.assertFalse([b for b in buttons if b and b.startswith("c:pro:")])
        self.assertEqual(self.db.dm_chat(ADMIN), GROUP)
        await self.callback(ADMIN, f"c:sells:{GROUP}")
        self.assertTrue(self.db.get_chat(GROUP).sells)
        self.assertIn("Sells are on", self.session.sent("AnswerCallbackQuery")[-1].text)
        await self.callback(ADMIN, f"c:test:{GROUP}")
        self.assertIn("TEST POST", self.last_text())
        await self.callback(MEMBER, f"c:sells:{GROUP}")  # not an admin there
        self.assertTrue(self.session.sent("AnswerCallbackQuery")[-1].show_alert)
        self.assertTrue(self.db.get_chat(GROUP).sells)

    async def test_owner_panel(self):
        from buybot.bot import claim_for_owners
        await self.track()
        self.db.set_user_lang(OWNER, "en")
        await self.feed(message=message(OWNER, OWNER, "/admin", chat_type="private"))
        caption = self.last_caption()
        self.assertIn("Control panel", caption)
        self.assertIn("1</b> active", caption)
        for gone in ("Pro", "Stars", "ads to review"):
            self.assertNotIn(gone, caption)
        await self.callback(OWNER, "a:chats:0")
        self.assertIn("All chats", self.last_caption())
        await self.callback(OWNER, f"a:chat:{GROUP}")
        self.assertIn("Added by", self.last_caption())

        await self.callback(ADMIN, "a:chats:0")  # not the owner
        self.assertTrue(self.session.sent("AnswerCallbackQuery")[-1].show_alert)
        await self.feed(message=message(ADMIN, ADMIN, "/admin", chat_type="private"))
        self.assertNotIn("Control panel", self.last_caption() or "")

        # a chat from before the bot recorded who added it: the owner admins it, so it is the owner's
        self.db.ensure_chat(-1003, "Old channel")
        await claim_for_owners(self.bot, self.app)
        self.assertEqual(self.db.get_chat(-1003).added_by, OWNER)

    async def test_top_buyers(self):
        await self.track()
        now = int(time.time())
        whale, fish = "0x" + "aa" * 20, "0x" + "bb" * 20
        self.db.add_trades([(TOKEN, "1", now - 600, 1e-8, 1.5, 1, whale), (TOKEN, "2", now - 500, 1e-8, 0.5, 1, whale),
                            (TOKEN, "3", now - 400, 1e-8, 0.3, 1, fish), (TOKEN, "4", now - 300, 1e-8, 9.0, -1, fish)])
        await self.feed(message=message(GROUP, ADMIN, "/top"))
        text = self.last_text()
        self.assertIn("Top buyers of TST", text)
        self.assertLess(text.index("0xaaaa"), text.index("0xbbbb"))  # the bigger buyer first
        self.assertIn("🥇", text)
        self.assertIn("<b>2 ETH</b> (≈$6,000) · 2 buys", text)
        self.assertIn("2 wallets", text)  # the sell does not count

    async def test_stats(self):
        await self.track()
        t0 = int(time.time()) - 5 * 3600
        self.db.add_trades([(TOKEN, "a", t0, 1e-8, 0.5, 1), (TOKEN, "b", t0 + 60, 2e-8, 0.2, -1),
                            (TOKEN, "c", t0 + 120, 3e-8, 1.0, 1)])
        self.buy_row(1)
        await self.sender.flush()
        await self.feed(message=message(GROUP, ADMIN, "/stats"))
        text = self.last_text()
        self.assertIn("2 buys", text)
        self.assertIn("1 sell", text)
        self.assertIn("≈$4,500", text)       # 1.5 ETH of buys at $3,000
        self.assertIn("MC now <b>$90,000</b>", text)  # 3e-8 ETH x 1e9 tokens x $3,000
        self.assertIn("▲ 200.0%", text)
        self.assertIn("Today 1", text)       # the one buy posted here
        self.assertNotIn("Last 3 days", text)  # no week block while the trades cover only the day

        # three days of trades: the week block shows them all
        self.db.add_trades([(TOKEN, "w", int(time.time()) - 3 * 86400, 5e-9, 2.0, 1)])
        await self.feed(message=message(GROUP, ADMIN, "/stats"))
        text = self.last_text()
        self.assertIn("Last 3 days", text)
        self.assertIn("🟢 3 buys · <b>≈$10,500</b>", text)  # 3.5 ETH of buys in the week
        self.assertIn("MC high $90,000 · low $15,000 · ▲ 500.0%", text)

    async def test_daily_digest(self):
        """At 18:00 UTC each tracking chat gets its token's last 24 hours once; a quiet day
        or /digest off sends nothing."""
        from buybot import digest
        await self.track()
        day = int(time.time()) // 86400 * 86400
        at = day + 18 * 3600 + 1800          # 18:30 UTC today
        self.assertEqual(await digest.queue(self.db, self.app.rates, now=day + 17 * 3600), 0)  # not yet
        self.assertEqual(await digest.queue(self.db, self.app.rates, now=at), 0)  # no trades: nothing
        self.db.delete_meta(f"digest:{GROUP}")
        self.db.add_trades([(TOKEN, "a", at - 7200, 1e-8, 0.5, 1, "0x" + "11" * 20),
                            (TOKEN, "b", at - 3600, 2e-8, 0.2, -1, "0x" + "22" * 20),
                            (TOKEN, "c", at - 600, 3e-8, 1.0, 1, "0x" + "33" * 20)])
        self.assertEqual(await digest.queue(self.db, self.app.rates, now=at), 1)
        self.assertEqual(await digest.queue(self.db, self.app.rates, now=at + 600), 0)  # once a day
        await self.sender.flush()
        text = self.last_text()
        self.assertIn("the last 24 hours", text)
        self.assertIn("🟢 2 buys · <b>≈$4,500</b>", text)
        self.assertIn("👥 2 wallets bought", text)
        self.assertIn("▲ 200.0%", text)
        self.assertIn("🥇 0x3333…3333 · <b>≈$3,000</b>", text)

        await self.feed(message=message(GROUP, ADMIN, "/digest off"))
        self.assertFalse(self.db.get_chat(GROUP).digest)
        self.assertEqual(await digest.queue(self.db, self.app.rates, now=at + 86400), 0)
        from buybot.bot import settings_text
        self.assertIn("Daily digest: off", settings_text(self.app, GROUP, "en"))

    async def test_drop_alert_goes_to_the_admin_in_private(self):
        """A 30%+ fall from the last hour's high: the admin who added the bot gets a
        private message, at most every 6 hours; /alerts off in the chat stops it."""
        from buybot.watcher import Watcher
        await self.track()
        self.db.update_chat(GROUP, added_by=ADMIN)
        now = time.time()
        self.db.add_trades([(TOKEN, "a", int(now - 1800), 3e-8, 0.5, 1), (TOKEN, "b", int(now - 60), 1e-8, 0.4, -1)])
        w = Watcher(self.app.rpc, self.db, FakeEthUsd(), rates=self.app.rates)
        await self.app.rates.get("eth")
        self.assertEqual(w.dump_alerts({TOKEN}, now=now), 1)
        self.assertEqual(w.dump_alerts({TOKEN}, now=now + 600), 0)  # quiet for 6 h
        await self.sender.flush()
        alert = [c for c in self.session.sent("SendMessage") if c.chat_id == ADMIN][-1]
        self.assertIn("fell <b>67%</b> in the last hour", alert.text)
        self.assertIn("MC $90,000 → <b>$30,000</b>", alert.text)
        self.assertIsNone(self.db.get_chat(ADMIN))  # a private alert, not a chat of its own

        await self.feed(message=message(GROUP, ADMIN, "/alerts off"))
        later = now + 7 * 3600
        self.db.add_trades([(TOKEN, "c", int(later - 1200), 2e-8, 0.3, 1), (TOKEN, "d", int(later), 5e-9, 0.1, -1)])
        self.assertEqual(w.dump_alerts({TOKEN}, now=later + 60), 0)  # a new 75% fall, but alerts are off
        self.db.update_chat(GROUP, alerts=1)
        self.assertEqual(w.dump_alerts({TOKEN}, now=later + 120), 1)

    def init_data(self, uid, age=0, token="42:TEST"):
        """A Mini App's initData for a user, signed as Telegram signs it."""
        import hashlib
        import hmac
        import json as _json
        from urllib.parse import urlencode
        data = {"auth_date": str(int(time.time()) - age), "query_id": "q1",
                "user": _json.dumps({"id": uid, "first_name": "U"})}
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(data)

    async def test_mini_app(self):
        """The Mini App's API: only Telegram-signed, fresh requests; a user sees the chats
        they added the bot to; a current admin changes settings, checked like the commands."""
        from aiohttp.test_utils import TestClient, TestServer
        from buybot import webapp
        await self.track()
        self.db.update_chat(GROUP, added_by=ADMIN)
        self.db.add_trades([(TOKEN, "a", int(time.time()) - 600, 1e-8, 0.5, 1, "0x" + "11" * 20)])
        self.assertIsNone(webapp.check_init_data(self.init_data(ADMIN), "42:OTHER"))  # another bot's
        self.assertIsNone(webapp.check_init_data(self.init_data(ADMIN, age=2 * 86400), "42:TEST"))  # too old
        object.__setattr__(self.app.cfg, "bot_token", "42:TEST")  # the token initData is signed with
        async with TestClient(TestServer(webapp.build(self.app, self.bot))) as client:
            r = await client.get("/api/chats")
            self.assertEqual(r.status, 401)
            admin = {"X-Init-Data": self.init_data(ADMIN)}
            r = await client.get("/api/chats", headers=admin)
            self.assertEqual([c["id"] for c in (await r.json())["chats"]], [GROUP])
            r = await client.get(f"/api/chat/{GROUP}", headers=admin)
            got = await r.json()
            self.assertEqual(got["token"]["symbol"], "TST")
            self.assertEqual(got["token"]["day"]["buys"], 1)
            self.assertEqual(got["token"]["recent"][0]["side"], 1)
            r = await client.post(f"/api/chat/{GROUP}", headers=admin,
                                  json={"min_usd": 25, "sells": True, "digest": False, "chart": 2, "emoji": "🚀"})
            self.assertEqual(r.status, 200)
            c = self.db.get_chat(GROUP)
            self.assertEqual((c.min_usd, c.sells, c.digest, c.chart, c.emoji), (25.0, True, False, 2, "🚀"))
            r = await client.post(f"/api/chat/{GROUP}", headers=admin, json={"min_usd": -1})
            self.assertEqual(r.status, 400)
            r = await client.post(f"/api/chat/{GROUP}", headers=admin, json={"emoji": "<b>"})
            self.assertEqual(r.status, 400)
            member = {"X-Init-Data": self.init_data(MEMBER)}
            r = await client.get(f"/api/chat/{GROUP}", headers=member)
            self.assertEqual(r.status, 404)       # not their chat
            r = await client.get("/")
            self.assertIn("<title>Silverhand Buys</title>", await r.text())

            # everything the chat does, through the same commands
            r = await client.get("/api/info", headers=admin)
            got = await r.json()
            self.assertFalse(got["owner"])
            self.assertIn("/track", got["help"])
            self.assertEqual(len(got["styles"]), 6)
            r = await client.post(f"/api/chat/{GROUP}/do", headers=admin, json={"action": "untrack"})
            self.assertIsNone(self.db.get_chat(GROUP).token)
            r = await client.post(f"/api/chat/{GROUP}/do", headers=admin, json={"action": "track", "args": [TOKEN]})
            self.assertIn("Tracking started", (await r.json())["text"])
            self.assertEqual(self.db.get_chat(GROUP).token, TOKEN)
            before = len(self.session.calls)
            r = await client.post(f"/api/chat/{GROUP}/do", headers=admin, json={"action": "test"})
            self.assertEqual(r.status, 200)
            self.assertGreater(len(self.session.calls), before)  # the sample post went out
            r = await client.post(f"/api/chat/{GROUP}/do", headers=admin, json={"action": "emojipack"})
            self.assertEqual(r.status, 400)                      # only the app's own buttons
            r = await client.post(f"/api/chat/{GROUP}", headers=admin, json={"lang": "ru", "media_off": True})
            self.assertEqual(self.db.get_chat(GROUP).lang, "ru")
            r = await client.get("/api/scan?address=0x123", headers=admin)
            self.assertEqual(r.status, 400)
            r = await client.post("/api/promo", headers=admin, json={"address": TOKEN, "style": "nope"})
            self.assertEqual(r.status, 400)
            r = await client.get("/api/owner", headers=admin)
            self.assertEqual(r.status, 404)                      # not an owner
            r = await client.get("/api/owner", headers={"X-Init-Data": self.init_data(OWNER)})
            self.assertIn("text", await r.json())
            r = await client.get("/samples/terminal.mp4")
            self.assertEqual(r.status, 200)
            r = await client.get("/samples/..%2Fbot.mp4")
            self.assertEqual(r.status, 404)

    async def test_owner_stats(self):
        await self.track()
        await self.feed(message=message(OWNER, OWNER, "/botstats", chat_type="private"))
        self.assertIn("chats_tracking: 1", self.last_text())
        self.assertNotIn("stars_total", self.last_text())
        count = len(self.session.sent("SendMessage"))
        await self.feed(message=message(MEMBER, MEMBER, "/botstats", chat_type="private"))
        self.assertEqual(len(self.session.sent("SendMessage")), count)  # strangers get nothing

    async def test_mainnet_hides_and_rejects_testnet_promo(self):
        from dataclasses import replace
        from aiohttp.test_utils import TestClient, TestServer
        from buybot import webapp
        self.app.cfg = replace(self.app.cfg, chain_id=4663, bot_token="42:TEST")
        await self.feed(message=message(MEMBER, MEMBER, f"/promo {TOKEN}", chat_type="private"))
        self.assertIn("only for vibe/vibe", self.last_text())
        self.assertFalse(self.app.promos.pickers)
        async with TestClient(TestServer(webapp.build(self.app, self.bot))) as client:
            headers = {"X-Init-Data": self.init_data(ADMIN)}
            r = await client.get("/api/info", headers=headers)
            info = await r.json()
            self.assertEqual(info["network"], "Robinhood Chain")
            self.assertFalse(info["promo"])
            self.assertEqual(info["styles"], [])
            r = await client.post("/api/promo", headers=headers,
                                  json={"address": TOKEN, "style": "dashboard"})
            self.assertEqual(r.status, 400)


class VibeCard(unittest.TestCase):
    def test_graduation_market_cap_of_a_vibe_curve(self):
        # a vibe/vibe curve ends at target / 200M per token, the pool's opening price: 4 ETH -> 20 ETH MC
        from tests import vibe_token
        from buybot.sender import graduation_mc, venue_label
        self.assertAlmostEqual(graduation_mc(vibe_token()), 20.0, places=6)
        self.assertIsNone(graduation_mc(vibe_token(graduated=True)))
        self.assertEqual(venue_label(vibe_token(), {"venue": "curve", "launchpad": "vibe/vibe"}), "vibe/vibe curve")


class MilestoneCard(unittest.TestCase):
    def test_a_vibe_milestone_is_drawn_on_the_new_card(self):
        """A vibe/vibe milestone ($25K MC, the curve 75% sold) takes the trade card, not the
        old green chart: its own pill and the level as the hero."""
        from buybot import card
        self.assertEqual([card._level_text(v) for v in (25_000, 2_500_000, 1e9, 12_345)],
                         ["$25K", "$2.5M", "$1B", "$12.3K"])
        spec = {"theme": "vibe", "side": 1, "symbol": "SHBUYS", "name": "Silverhand Buys",
                "network": "Robinhood Chain testnet", "venue": "vibe/vibe curve", "usd": 50.0, "quote": 0.02,
                "quote_symbol": "AAPL", "mc_usd": 25_400.0, "progress": 0.5, "points": [], "supply": 1e9,
                "now": 1_800_000_000, "milestone": {"what": "mc", "level": 25_000.0}}
        png = card.render_still(spec)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertTrue(card.render_still(dict(spec, milestone={"what": "curve", "level": 0.75})))


class ProfileSync(unittest.IsolatedAsyncioTestCase):
    def test_texts_fit_telegram_limits(self):
        self.assertEqual(set(profile.DESCRIPTIONS), set(profile.SHORT_DESCRIPTIONS))
        for lang in profile.DESCRIPTIONS:
            with self.subTest(lang=lang):
                self.assertLessEqual(profile.utf16_len(profile.DESCRIPTIONS[lang]), profile.DESCRIPTION_MAX)
                self.assertLessEqual(profile.utf16_len(profile.SHORT_DESCRIPTIONS[lang]),
                                     profile.SHORT_DESCRIPTION_MAX)
                self.assertIn("Robinhood", profile.DESCRIPTIONS[lang])  # the not-affiliated line
                self.assertIn("Robinhood", profile.SHORT_DESCRIPTIONS[lang])  # the not-affiliated line
                for gone in ("Pro", "Free forever", "Бесплатно навсегда", "ad line", "рекламной строки"):
                    self.assertNotIn(gone, profile.DESCRIPTIONS[lang] + profile.SHORT_DESCRIPTIONS[lang])

    def test_command_menus_sell_nothing(self):
        from buybot import main
        names = {c for c, _, _ in main.GROUP_COMMANDS + main.PRIVATE_COMMANDS}
        self.assertFalse(names & {"pro", "advertise", "paysupport"})
        self.assertIn("support", names)
        self.assertFalse([d for _, en, ru in main.GROUP_COMMANDS for d in (en, ru) if "Pro" in d])

    async def test_sets_missing_texts_once(self):
        session = FakeSession()
        bot = Bot("42:TEST", session=session)
        changed = await profile.sync_profile(bot)
        self.assertEqual(len(changed), 2 * len(profile.DESCRIPTIONS))
        self.assertEqual(session.profile[("d", "ru")], profile.DESCRIPTIONS["ru"])
        self.assertEqual(session.profile[("s", None)], profile.SHORT_DESCRIPTIONS[None])
        self.assertEqual(await profile.sync_profile(bot), [])  # a restart changes nothing

    async def test_english_only_removes_the_russian_texts(self):
        session = FakeSession()
        bot = Bot("42:TEST", session=session)
        await profile.sync_profile(bot)
        self.assertEqual(await profile.sync_profile(bot, ("en",)), ["description ru", "about ru"])
        self.assertEqual((session.profile[("d", "ru")], session.profile[("s", "ru")]), ("", ""))
        self.assertEqual(session.profile[("d", None)], profile.DESCRIPTIONS[None])


class FakeRates:
    def current(self, kind):
        return 2.0


class HolderMilestones(unittest.TestCase):
    """Posts at 50, 100, 250 … holders: one per level, the highest of several, never twice."""

    def setUp(self):
        from tests import vibe_token
        self.dir = tempfile.TemporaryDirectory()
        self.db = Db(os.path.join(self.dir.name, "t.db"))
        self.token = vibe_token()
        self.db.upsert_token(self.token)
        for chat_id in (-1, -2):
            self.db.ensure_chat(chat_id)
            self.db.update_chat(chat_id, token=self.token.address)
        self.db.add_trades([(self.token.address, "a", int(time.time()) - 60, 1e-8, 0.01, 1)])

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def levels(self, *counts):
        from buybot import holders
        return [holders.check(self.db, FakeRates(), self.token, n) for n in counts]

    def test_levels(self):
        from buybot import holders
        self.assertEqual([holders.level_reached(n) for n in (0, 49, 50, 99, 260, 4999, 5000, 9999)],
                         [None, None, 50, 50, 250, 2500, 5000, 5000])

    def test_one_post_per_level_the_highest_and_never_twice(self):
        self.assertEqual(self.levels(120), [None])          # the first read only sets the base
        self.assertEqual(self.db.outbox_size(), 0)
        self.assertEqual(self.levels(130, None, 260), [None, None, 250])  # 100 was passed before tracking
        self.assertEqual(self.levels(1200), [1000])          # 500 skipped over: only 1000
        self.assertEqual(self.levels(400, 1100, 900, 1300), [None, None, None, None])  # down and back: no repeat
        self.assertEqual(self.levels(2600), [2500])
        rows = [r for r in self.db.pending_outbox() if r.kind == "milestone"]
        self.assertEqual(sorted((r.chat_id, r.payload["level"]) for r in rows),
                         [(-2, 250), (-2, 1000), (-2, 2500), (-1, 250), (-1, 1000), (-1, 2500)])
        p = rows[-1].payload
        self.assertEqual((p["what"], p["holders"], p["symbol"], p["launchpad"]), ("holders", 2600, "UG", "vibe/vibe"))
        self.assertAlmostEqual(p["mc_quote"], 10.0)   # the last trade's price × the supply
        self.assertAlmostEqual(p["mc_usd"], 20.0)

    def test_holder_count_is_vibe_vibes(self):
        from buybot import holders, promo
        token = self.token.address
        answers = {f"/launches/{token}": {"tokenAddress": token, "holderCount": 321}}

        async def api_get(session, path, bot=""):
            return next((v for k, v in answers.items() if path.endswith(k)), None)

        real, promo.api_get = promo.api_get, api_get
        try:
            self.assertEqual(asyncio.run(holders.holder_count(None, token)), 321)  # a curve-factory launch
            answers = {"/v6/launches?limit=1&q=" + token: {"items": [{"tokenAddress": token,
                                                                      "market": {"holderCount": 77}}]}}
            self.assertEqual(asyncio.run(holders.holder_count(None, token)), 77)   # a V6 launch
            answers = {}
            self.assertIsNone(asyncio.run(holders.holder_count(None, token)))      # the API is down
        finally:
            promo.api_get = real

    def test_post_text_share_and_card(self):
        from buybot import card
        p = {"token": self.token.address, "symbol": "UG", "what": "holders", "level": 1000, "holders": 1012}
        self.assertEqual(render.milestone_text(p), "👥👥👥\n<b>UG</b> has <b>1,000</b> holders!")
        self.assertIn("уже <b>1,000</b> держателей", render.milestone_text(p, "ru"))
        self.assertEqual(render.share_text(p, "milestone"), "👥 $UG has 1,000 holders")
        spec = {"theme": "vibe", "side": 1, "symbol": "UG", "name": "Vibe Token",
                "network": "Robinhood Chain testnet", "venue": "vibe/vibe curve", "usd": None, "quote": None,
                "quote_symbol": "ETH", "mc_usd": 20.0, "points": [], "supply": 1e9,
                "now": 1_800_000_000, "milestone": {"what": "holders", "level": 1000}}
        self.assertTrue(card.render_still(spec).startswith(b"\x89PNG"))

    def test_a_holder_post_never_swallows_a_market_cap_one(self):
        """Milestones waiting together: the highest of each kind, one kind a post."""
        from buybot import holders
        sender = Sender(None, self.db, trade_url="https://x/{token}", explorer="https://e")
        self.db.add_outbox(-1, "milestone", "level:mc:25000", {"token": self.token.address, "symbol": "UG",
                                                              "what": "mc", "level": 25000.0, "tx": "0x1"})
        self.levels(10)
        self.levels(60)
        chat = self.db.get_chat(-1)

        def mine():
            return [r for r in self.db.pending_outbox() if r.chat_id == -1]

        batch, text, pairs, _, (p, side) = sender._compose(chat, mine())   # the latest kind first
        self.assertIn("<b>50</b> holders", text)
        self.assertEqual(p["chart_label"], "50")
        self.assertEqual(len(mine()), 2)   # the market cap one still waits
        self.db.delete_outbox([r.id for r in batch])
        share = dict((x[0], x[1]) for x in pairs if x)["𝕏 Share"]
        batch, text, pairs, _, _ = sender._compose(chat, mine())
        self.assertIn("market cap", text)
        self.assertEqual(pairs[-1][0], "𝕏 Share")
        self.assertIn("50%20holders", share)


class SenderReliability(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests import fixture_token
        self.db = Db(":memory:")
        self.db.upsert_token(fixture_token())
        self.session = FakeSession()
        self.bot = Bot("42:TEST", session=self.session,
                       default=DefaultBotProperties(parse_mode="HTML"))
        self.sender = Sender(self.bot, self.db, trade_url="https://x/{token}",
                             explorer="https://e", chat_interval=0, global_rate=10000)

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.db.close()

    def buy(self, chat_id, uid="a"):
        self.db.ensure_chat(chat_id)
        self.db.update_chat(chat_id, token=TOKEN)
        self.db.add_outbox(chat_id, "buy", uid,
                           dict(render.sample_payload("TST", "ETH", 3000), token=TOKEN))

    def alert(self, uid="a"):
        self.db.add_outbox(ADMIN, "alert", uid, {
            "token": TOKEN, "symbol": "TST", "chat": "Chat", "lang": "en",
            "drop": 0.5, "mc_high": 2, "mc_now": 1, "quote_symbol": "ETH", "rate": 3000})

    async def test_slow_card_does_not_delay_other_chats(self):
        from buybot import sender
        started, release, others_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
        for chat_id in range(-20, 0):
            self.buy(chat_id)

        async def slow(*args, **kwargs):
            started.set()
            await release.wait()
            return b"card", "animation"

        original = self.session.make_request

        async def observe(bot, method, timeout=None):
            answer = await original(bot, method, timeout)
            if len(self.session.sent("SendMessage")) == 19:
                others_sent.set()
            return answer

        with patch.object(sender, "_make_chart", side_effect=slow) as draw, \
                patch.object(self.session, "make_request", side_effect=observe):
            job = asyncio.create_task(self.sender.flush())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(others_sent.wait(), 2)
                self.assertFalse(job.done())
                self.assertEqual(draw.call_count, 1)  # no extra renderer or render queue
            finally:
                release.set()
                self.assertEqual(await job, 20)
        self.assertEqual(self.db.outbox_size(), 0)
        self.assertEqual(len(self.session.sent("SendAnimation")), 1)

    async def test_posts_do_not_wait_for_a_promo_render(self):
        from buybot import heavy, sender
        self.buy(-1)
        with patch.object(sender, "_make_chart", new_callable=AsyncMock) as draw:
            async with heavy.slot():
                self.assertEqual(await asyncio.wait_for(self.sender.flush(), 2), 1)
            draw.assert_not_called()
        self.assertEqual(len(self.session.sent("SendMessage")), 1)

    async def test_identical_completed_card_is_reused(self):
        from buybot import sender
        with patch.object(sender, "_make_chart", new_callable=AsyncMock,
                          return_value=(b"card", "animation")) as draw:
            self.buy(-1)
            await self.sender.flush()
            self.buy(-2)
            await self.sender.flush()
            self.assertEqual(draw.call_count, 1)
        self.assertEqual(len(self.session.sent("SendAnimation")), 2)

    async def test_cancelled_card_keeps_the_memory_slot_until_finished(self):
        from buybot import heavy, sender
        started, release = asyncio.Event(), asyncio.Event()

        async def draw(*args, **kwargs):
            started.set()
            await release.wait()
            return b"card", "animation"

        with patch.object(sender, "_make_chart", side_effect=draw):
            task = asyncio.create_task(sender.make_chart(self.db, {"token": TOKEN}, 1))
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)  # let make_chart handle cancellation
                async with heavy.slot(wait=False) as acquired:
                    self.assertFalse(acquired)
                self.assertFalse(task.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            async with heavy.slot(wait=False) as acquired:
                self.assertTrue(acquired)

    async def test_untrack_during_render_prevents_the_post(self):
        from buybot import sender
        self.buy(-1)

        async def untrack(*args, **kwargs):
            self.db.update_chat(-1, token=None)
            return b"card", "animation"

        with patch.object(sender, "_make_chart", side_effect=untrack):
            self.assertEqual(await self.sender.flush(), 0)
            self.assertEqual(await self.sender.flush(), 0)
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.db.outbox_size(), 0)

    async def test_expiry_during_render_keeps_fresh_trades(self):
        from buybot import sender
        self.buy(-1, "old")
        self.buy(-1, "fresh")
        with self.db.conn:
            self.db.conn.execute("UPDATE outbox SET created_at=CASE dedupe WHEN 'old' THEN 400 ELSE 999 END")
        with patch("buybot.sender.time.time", return_value=1000) as clock:
            async def draw(*args, **kwargs):
                clock.return_value = 1001
                return b"card", "animation"
            with patch.object(sender, "_make_chart", side_effect=draw):
                self.assertEqual(await self.sender.flush(), 0)
                self.assertEqual(self.db.outbox_size(), 1)
                self.assertEqual(self.db.pending_outbox()[0].created_at, 999)
                self.assertEqual(await self.sender.flush(), 1)
        self.assertEqual(self.db.outbox_size(), 0)

    async def test_alert_network_failure_is_retried(self):
        from aiogram.exceptions import TelegramNetworkError
        self.alert()
        self.session.fail[ADMIN] = TelegramNetworkError(method=None, message="offline")
        await self.sender.flush()
        self.assertEqual(self.db.outbox_size(), 1)
        self.assertEqual(self.db.pending_outbox(), [])  # deadline persisted in SQLite
        del self.session.fail[ADMIN]
        with self.db.conn:
            self.db.conn.execute("UPDATE outbox SET not_before=0")
        self.sender._next_chat.clear()
        await self.sender.flush()
        self.assertEqual(self.db.outbox_size(), 0)
        self.assertIsNotNone(self.sender.last_sent)

    async def test_alert_flood_wait_survives_new_sender(self):
        from aiogram.exceptions import TelegramRetryAfter
        self.alert()
        self.session.fail[ADMIN] = TelegramRetryAfter(method=None, message="wait", retry_after=30)
        await self.sender.flush()
        deadline = self.db.conn.execute("SELECT not_before FROM outbox").fetchone()[0]
        self.assertGreater(deadline, time.time() + 29)
        replacement = Sender(self.bot, self.db, trade_url="", explorer="")
        del self.session.fail[ADMIN]
        n = len(self.session.calls)
        await replacement.flush()
        self.assertEqual(len(self.session.calls), n)
        self.assertEqual(self.db.outbox_size(), 1)

    async def test_blocked_user_drops_private_alerts(self):
        self.alert("a")
        self.alert("b")
        self.session.fail[ADMIN] = TelegramForbiddenError(method=None, message="blocked")
        await self.sender.flush()
        self.assertEqual(self.db.outbox_size(), 0)
        self.assertEqual(self.sender.dropped, 2)

    async def test_alerts_use_the_shared_rate_limiter(self):
        self.alert()
        with patch.object(self.sender, "_throttle", new_callable=AsyncMock) as throttle:
            await self.sender.flush()
            throttle.assert_awaited_once_with(ADMIN)

    async def test_digest_failure_does_not_consume_the_day(self):
        import sqlite3
        from types import SimpleNamespace
        from buybot import digest
        self.buy(-1)
        self.db.clear_chat_outbox(-1)
        at = int(time.time()) // 86400 * 86400 + 18 * 3600
        self.db.add_trades([(TOKEN, "t", at - 60, 1e-8, 1, 1)])
        rates = SimpleNamespace(get=AsyncMock(return_value=3000))
        self.db.conn.execute("CREATE TEMP TRIGGER fail_digest BEFORE INSERT ON outbox "
                             "WHEN NEW.kind='digest' BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await digest.queue(self.db, rates, now=at)
        self.assertIsNone(self.db.get_meta("digest:-1"))
        self.db.conn.execute("DROP TRIGGER fail_digest")
        self.assertEqual(await digest.queue(self.db, rates, now=at), 1)
        self.assertEqual(await digest.queue(self.db, rates, now=at), 0)

    async def test_holder_failure_rolls_back_all_chats_and_the_level(self):
        import sqlite3
        from buybot import holders
        from tests import vibe_token
        token = vibe_token()
        self.db.upsert_token(token)
        for chat_id in (-1, -2):
            self.db.ensure_chat(chat_id)
            self.db.update_chat(chat_id, token=token.address)
        holders.check(self.db, FakeRates(), token, 1)
        key = f"level:holders:{token.address}"
        self.db.conn.execute("CREATE TEMP TRIGGER fail_holders BEFORE INSERT ON outbox "
                             "WHEN NEW.chat_id=-2 BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            holders.check(self.db, FakeRates(), token, 60)
        self.assertEqual(self.db.get_meta(key), "0")
        self.assertEqual(self.db.outbox_size(), 0)
        self.db.conn.execute("DROP TRIGGER fail_holders")
        self.assertEqual(holders.check(self.db, FakeRates(), token, 60), 50)
        self.assertEqual(self.db.outbox_size(), 2)
        self.assertEqual(self.db.pending_outbox()[0].payload["network"], "Robinhood Chain testnet")

    async def test_health_detects_static_head_sender_and_queue(self):
        from types import SimpleNamespace
        from buybot import health
        w = SimpleNamespace(last_ok=1000, last_rpc_ok=1000, last_head_change=600, head=123)
        self.sender.last_ok = 600
        self.db.set_last_block(120)
        self.buy(-1)
        with self.db.conn:
            self.db.conn.execute("UPDATE outbox SET created_at=600")
        app = SimpleNamespace(watcher=w, sender=self.sender, db=self.db)
        self.assertEqual(health.issues(app, now=1000), {"head", "sender", "queue"})
        self.assertEqual(health.snapshot(app, now=1000)["lag"], 3)
        self.assertEqual(health.snapshot(app, now=1000, chat_id=-2)["outbox"], 0)
        w.last_head_change = self.sender.last_ok = 1000
        self.db.clear_chat_outbox(-1)
        self.assertEqual(health.issues(app, now=1000), set())

    async def test_health_monitors_daemons_but_allows_completed_backfill(self):
        from types import SimpleNamespace
        from buybot import health
        app = SimpleNamespace(watcher=None, sender=None, db=self.db)
        task = asyncio.create_task(asyncio.sleep(0), name="backfill")
        await task
        self.assertEqual(health.issues(app, tasks=[task]), set())
        task.set_name("sender")
        self.assertEqual(health.issues(app, tasks=[task]), {"task"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
