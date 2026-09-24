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
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import (BotDescription, BotShortDescription, CallbackQuery, Chat, ChatMemberLeft,
                           ChatMemberMember, ChatMemberOwner, ChatMemberUpdated, Message, MessageEntity,
                           MessageOriginChannel, PreCheckoutQuery, SuccessfulPayment, Update, User,
                           Animation)

from buybot import abi, chain, profile, render
from buybot.bot import App, build_router
from buybot.config import Config
from buybot.db import Db
from buybot.sender import Sender

FIX = json.loads((Path(__file__).parent / "fixtures" / "pons_v2_mainnet.json").read_text())
TOKEN = FIX["launch"]["token"]
GROUP, CHANNEL = -1001, -1002
ADMIN, MEMBER, OWNER, ADVERTISER = 10, 11, 99, 12
BOT_ID = 42


def now():
    return datetime.now(timezone.utc)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail: dict[int, Exception] = {}
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
        if name.startswith("Send") or name == "EditMessageText":
            return Message(message_id=len(self.calls), date=now(),
                           chat=Chat(id=chat_id or 1, type="private"))
        return True

    def sent(self, name=None):
        return [c for c in self.calls if name is None or type(c).__name__ == name]


class FakeRpc:
    calls = errors = 0

    async def eth_call(self, to, data, block="latest"):
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
        self.cfg = Config(bot_token="x", owner_ids=frozenset({OWNER}),
                          trade_url="https://gmgn.ai/robinhood/token/REF_{token}")
        self.app = App(cfg=self.cfg, db=self.db, rpc=FakeRpc(), eth_usd=FakeEthUsd(),
                       username="testbuybot")
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.app))
        self.sender = Sender(self.bot, self.db, "testbuybot", trade_url=self.cfg.trade_url,
                             explorer="https://explorer", chat_interval=0)
        self.uid = 0

    async def asyncTearDown(self):
        self.db.close()
        self.dir.cleanup()

    async def feed(self, **kind):
        self.uid += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.uid, **kind))

    def last_text(self, name="SendMessage"):
        calls = self.session.sent(name)
        return (calls[-1].text if name == "SendMessage" else calls[-1].caption) if calls else None

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
        self.assertEqual(self.db.get_chat(GROUP).min_usd, 0)  # non-admins are ignored
        self.assertEqual(len(self.session.sent("SendMessage")),
                         len([c for c in self.session.calls[:before] if type(c).__name__ == "SendMessage"]))

        await self.feed(message=message(GROUP, ADMIN, "/minbuy 50"))
        self.assertEqual(self.db.get_chat(GROUP).min_usd, 50)
        await self.feed(message=message(GROUP, ADMIN, "/emoji 🚀"))
        await self.feed(message=message(GROUP, ADMIN, "/emoji abc"))
        self.assertEqual(self.db.get_chat(GROUP).emoji, "🚀")
        await self.feed(message=message(GROUP, ADMIN, "/settings"))
        self.assertIn("Min buy: $50.00", self.last_text())

    async def test_not_a_pons_token(self):
        await self.join(GROUP)
        await self.feed(message=message(GROUP, ADMIN, "/track 0x" + "12" * 20))
        self.assertIn("not a Pons v2 token", self.last_text())
        self.assertIsNone(self.db.get_chat(GROUP).token)

    async def test_preview_and_real_posts(self):
        await self.track()
        await self.feed(message=message(GROUP, ADMIN, "/test"))
        self.assertIn("TEST POST", self.last_text())
        self.assertIn("via @testbuybot", self.last_text())

        self.buy_row(1)
        await self.sender.flush()
        text = self.last_text()
        self.assertIn("TST Buy!", text)
        self.assertIn("$300", text)
        markup = self.session.sent("SendMessage")[-1].reply_markup
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

    async def test_pro_purchase_and_media(self):
        await self.track()
        await self.feed(message=message(ADMIN, ADMIN, f"/start pro_{GROUP}", chat_type="private"))
        offer = self.session.sent("SendMessage")[-1]
        self.assertIn("Pro for", offer.text)
        data = offer.reply_markup.inline_keyboard[0][0].callback_data
        self.assertEqual(data, f"pro:{GROUP}:1")

        await self.feed(callback_query=CallbackQuery(id="1", from_user=user(ADMIN), chat_instance="c",
                                                     data=data))
        invoice = self.session.sent("SendInvoice")[-1]
        self.assertEqual((invoice.currency, invoice.prices[0].amount), ("XTR", 500))

        await self.feed(pre_checkout_query=PreCheckoutQuery(
            id="p1", from_user=user(ADMIN), currency="XTR", total_amount=499,
            invoice_payload=invoice.payload))
        self.assertFalse(self.session.sent("AnswerPreCheckoutQuery")[-1].ok)
        await self.feed(pre_checkout_query=PreCheckoutQuery(
            id="p2", from_user=user(ADMIN), currency="XTR", total_amount=500,
            invoice_payload=invoice.payload))
        self.assertTrue(self.session.sent("AnswerPreCheckoutQuery")[-1].ok)

        paid = SuccessfulPayment(currency="XTR", total_amount=500, invoice_payload=invoice.payload,
                                 telegram_payment_charge_id="ch1", provider_payment_charge_id="")
        for _ in range(2):  # Telegram may deliver the same payment twice
            await self.feed(message=message(ADMIN, ADMIN, "", chat_type="private").model_copy(
                update={"text": None, "entities": None, "successful_payment": paid}))
        chat = self.db.get_chat(GROUP)
        self.assertTrue(chat.is_pro())
        self.assertAlmostEqual(chat.pro_until - time.time(), 7 * 86400, delta=60)

        gif = message(GROUP, ADMIN, "x").model_copy(update={
            "text": None, "entities": None,
            "animation": Animation(file_id="GIF1", file_unique_id="u", width=1, height=1, duration=1)})
        await self.feed(message=message(GROUP, ADMIN, "/media", reply_to_message=gif))
        self.assertEqual(self.db.get_chat(GROUP).media_file_id, "GIF1")
        self.buy_row(1)
        await self.sender.flush()
        caption = self.last_text("SendAnimation")
        self.assertIn("TST Buy!", caption)
        self.assertNotIn("via @", caption)  # Pro: no ad line

    async def test_ad_flow(self):
        await self.track()
        await self.feed(message=message(ADVERTISER, ADVERTISER,
                                        "/advertise 2 https://t.me/other Join the OTHER chat",
                                        chat_type="private"))
        review = [c for c in self.session.sent("SendMessage") if c.chat_id == OWNER][-1]
        self.assertIn("Join the OTHER chat", review.text)
        ok = review.reply_markup.inline_keyboard[0][0].callback_data
        await self.feed(callback_query=CallbackQuery(id="2", from_user=user(MEMBER), chat_instance="c",
                                                     data=ok))
        self.assertEqual(self.db.get_ad(1).status, "pending")  # only the owner can approve
        await self.feed(callback_query=CallbackQuery(id="3", from_user=user(OWNER), chat_instance="c",
                                                     data=ok))
        invoice = self.session.sent("SendInvoice")[-1]
        self.assertEqual((invoice.chat_id, invoice.prices[0].amount), (ADVERTISER, 1500))
        paid = SuccessfulPayment(currency="XTR", total_amount=1500, invoice_payload=invoice.payload,
                                 telegram_payment_charge_id="ch2", provider_payment_charge_id="")
        await self.feed(message=message(ADVERTISER, ADVERTISER, "", chat_type="private").model_copy(
            update={"text": None, "entities": None, "successful_payment": paid}))
        self.assertEqual(self.db.get_ad(1).status, "paid")
        self.buy_row(1)
        await self.sender.flush()
        self.assertIn("Join the OTHER chat", self.last_text())

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
        sample, confirm = self.session.sent("SendMessage")[-2:]
        self.assertEqual((sample.chat_id, confirm.chat_id), (CHANNEL, ADMIN))
        self.assertIn("TEST POST", sample.text)
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
        self.assertIn("TST Buy!", self.last_text())  # buys and sells are posted apiece
        await self.sender.flush()
        self.assertIn("TST Sell!", self.last_text())

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
            self.assertIn("TST Buy!", anim.caption)
            self.assertEqual(anim.animation.data[4:8], b"ftyp")
            buttons = [b.callback_data for b in anim.reply_markup.inline_keyboard[0] if b.callback_data]
            self.assertEqual(buttons, [f"s:{TOKEN}"])

        await self.feed(message=message(GROUP, ADMIN, "/chart still"))
        self.assertIn("still picture", self.last_text())
        self.buy_row(2)
        await self.sender.flush()
        photo = self.session.sent("SendPhoto")[-1]
        self.assertIn("TST Buy!", photo.caption)
        self.assertTrue(photo.photo.data.startswith(b"\x89PNG"))

        await self.feed(message=message(GROUP, ADMIN, "/chart off"))
        self.assertIn("Chart off", self.last_text())
        self.buy_row(3)
        await self.sender.flush()
        self.assertEqual(len(self.session.sent("SendPhoto")), 1)
        self.assertIn("TST Buy!", self.last_text())

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
        self.assertIn("Source code (AGPL-3.0): https://example.org/src", self.last_text("SendAnimation"))

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

    async def test_chat_language_changes_posts(self):
        await self.track()
        await self.feed(message=message(GROUP, ADMIN, "/lang ru"))
        self.assertIn("на русском", self.last_text())
        self.buy_row(1)
        await self.sender.flush()
        self.assertIn("TST Покупка!", self.last_text())
        self.assertIn("Бесплатные уведомления", self.last_text())
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
        self.assertEqual(self.db.dm_chat(ADMIN), GROUP)
        await self.callback(ADMIN, f"c:sells:{GROUP}")
        self.assertTrue(self.db.get_chat(GROUP).sells)
        self.assertIn("Sells are on", self.session.sent("AnswerCallbackQuery")[-1].text)
        await self.callback(ADMIN, f"c:test:{GROUP}")
        self.assertIn("TEST POST", self.last_text())
        await self.callback(MEMBER, f"c:sells:{GROUP}")  # not an admin there
        self.assertTrue(self.session.sent("AnswerCallbackQuery")[-1].show_alert)
        self.assertTrue(self.db.get_chat(GROUP).sells)

    async def test_owner_panel_and_pro_control(self):
        from buybot.bot import FOREVER, claim_for_owners
        await self.track()
        self.db.set_user_lang(OWNER, "en")
        await self.feed(message=message(OWNER, OWNER, "/admin", chat_type="private"))
        self.assertIn("Control panel", self.last_caption())
        self.assertIn("1</b> active", self.last_caption())

        await self.callback(OWNER, f"a:grant:{GROUP}:30")
        self.assertAlmostEqual(self.db.get_chat(GROUP).pro_until - time.time(), 30 * 86400, delta=60)
        self.assertIn("Pro until", self.session.sent("AnswerCallbackQuery")[-1].text)
        await self.callback(OWNER, "a:pro")
        self.assertIn("Chats with Pro</b> (1)", self.last_caption())
        await self.callback(OWNER, f"a:grant:{GROUP}:0")
        self.assertEqual(self.db.get_chat(GROUP).pro_until, FOREVER)
        self.assertIn("forever", self.last_caption())
        await self.callback(OWNER, f"a:revoke:{GROUP}")
        self.assertFalse(self.db.get_chat(GROUP).is_pro())

        await self.callback(ADMIN, f"a:grant:{GROUP}:0")  # not the owner
        self.assertTrue(self.session.sent("AnswerCallbackQuery")[-1].show_alert)
        self.assertFalse(self.db.get_chat(GROUP).is_pro())
        await self.feed(message=message(ADMIN, ADMIN, "/admin", chat_type="private"))
        self.assertNotIn("Control panel", self.last_caption() or "")

        # a chat from before the bot recorded who added it: the owner admins it, so it is
        # the owner's, and the owner's chats are Pro for good
        self.db.ensure_chat(-1003, "Old channel")
        await claim_for_owners(self.bot, self.app)
        old = self.db.get_chat(-1003)
        self.assertEqual((old.added_by, old.pro_until), (OWNER, FOREVER))

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

    async def test_owner_stats(self):
        await self.track()
        await self.feed(message=message(OWNER, OWNER, "/botstats", chat_type="private"))
        self.assertIn("chats_tracking: 1", self.last_text())
        count = len(self.session.sent("SendMessage"))
        await self.feed(message=message(MEMBER, MEMBER, "/botstats", chat_type="private"))
        self.assertEqual(len(self.session.sent("SendMessage")), count)  # strangers get nothing


class ProfileSync(unittest.IsolatedAsyncioTestCase):
    def test_texts_fit_telegram_limits(self):
        self.assertEqual(set(profile.DESCRIPTIONS), set(profile.SHORT_DESCRIPTIONS))
        for lang in profile.DESCRIPTIONS:
            with self.subTest(lang=lang):
                self.assertLessEqual(profile.utf16_len(profile.DESCRIPTIONS[lang]), profile.DESCRIPTION_MAX)
                self.assertLessEqual(profile.utf16_len(profile.SHORT_DESCRIPTIONS[lang]),
                                     profile.SHORT_DESCRIPTION_MAX)
                self.assertIn("Robinhood", profile.DESCRIPTIONS[lang])  # the not-affiliated line
                self.assertIn("Pons", profile.SHORT_DESCRIPTIONS[lang])

    async def test_sets_missing_texts_once(self):
        session = FakeSession()
        bot = Bot("42:TEST", session=session)
        changed = await profile.sync_profile(bot)
        self.assertEqual(len(changed), 2 * len(profile.DESCRIPTIONS))
        self.assertEqual(session.profile[("d", "ru")], profile.DESCRIPTIONS["ru"])
        self.assertEqual(session.profile[("s", None)], profile.SHORT_DESCRIPTIONS[None])
        self.assertEqual(await profile.sync_profile(bot), [])  # a restart changes nothing


if __name__ == "__main__":
    unittest.main(verbosity=1)
