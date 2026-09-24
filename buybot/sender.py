"""Outbox -> Telegram, within Telegram's limits.

- At most one post per chat every `chat_interval` seconds (Telegram allows about
  20 a minute in a group). Buys that pile up in between are merged: the biggest
  is posted in full and the rest are summed in one line.
- At most `global_rate` messages a second across all chats.
- A kicked bot, a group upgraded to a supergroup, a flood wait, a broken GIF:
  each is handled, never retried in a loop.
- Buys older than `max_age` seconds (after downtime) are dropped, not posted late.
- Each buy or sell goes out as a price chart with the text as its caption: animated
  (a looping MP4 Telegram plays like a GIF) or, with /chart still, a picture. A chat
  with charts off, or showing its own Pro media, gets no chart; without one, text.
"""

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramMigrateToChat, TelegramNetworkError,
                                TelegramRetryAfter, TelegramServerError)
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from . import render
from .db import Chat, Db, OutboxRow
from .i18n import t

CHART_HOURS = 6
CHART_OFF, CHART_ANIMATED, CHART_STILL = 0, 1, 2

log = logging.getLogger(__name__)

_GONE = ("chat not found", "bot was kicked", "group chat was deactivated", "chat_write_forbidden",
         "have no rights to send", "not enough rights to send", "need administrator rights")
_NO_TOPIC = ("message thread not found", "topic_deleted", "topic not found")
_BAD_MEDIA = ("wrong file identifier", "file_id", "wrong type", "failed to get http url",
              "wrong remote file")


def keyboard(pairs: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    """One row of buttons: a value starting with https:// is a link, anything else a
    callback (the 📊 24h pop-up)."""
    if not pairs:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, url=v) if v.startswith("https://") else
        InlineKeyboardButton(text=label, callback_data=v) for label, v in pairs]])


async def send_post(bot: Bot, chat: Chat, text: str, pairs: list[tuple[str, str]],
                    with_media: bool, chart: tuple[bytes, str] | None = None) -> None:
    """Send one post; raises aiogram exceptions for the caller to classify.
    Pro media wins over the chart; a chart Telegram refuses falls back to text.
    `chart` is (bytes, "animation" | "photo")."""
    markup = keyboard(pairs)
    topic = {"message_thread_id": chat.thread_id} if chat.thread_id else {}
    if with_media and chat.media_file_id and chat.is_pro():
        send = {"animation": bot.send_animation, "photo": bot.send_photo,
                "video": bot.send_video}.get(chat.media_kind or "")
        if send:
            await send(chat.chat_id, chat.media_file_id, caption=text, reply_markup=markup, **topic)
            return
    if chart:
        data, kind = chart
        try:
            if kind == "animation":
                from .chart import ANIM_H, ANIM_W, SECONDS
                await bot.send_animation(chat.chat_id, BufferedInputFile(data, "chart.mp4"), caption=text,
                                         reply_markup=markup, width=ANIM_W, height=ANIM_H,
                                         duration=int(SECONDS), **topic)
            else:
                await bot.send_photo(chat.chat_id, BufferedInputFile(data, "chart.png"), caption=text,
                                     reply_markup=markup, **topic)
            return
        except TelegramBadRequest as e:
            if any(h in e.message.lower() for h in _GONE + _NO_TOPIC):
                raise
            log.warning("chart refused in %s (%s), posting text", chat.chat_id, e.message)
    await bot.send_message(chat.chat_id, text, reply_markup=markup, **topic)


def wants_chart(chat: Chat, with_media: bool) -> bool:
    return bool(chat.chart) and not (with_media and chat.media_file_id and chat.is_pro())


def trade_amount(p: dict, side: int) -> str:
    """What rises above the candle: +$8.02 for a buy, −$3.79 for a sell."""
    sign = "+" if side > 0 else "−"
    if p.get("usd") is not None:
        return sign + render.fmt_usd(p["usd"])
    return f"{sign}{render.fmt_num(p['quote'])} {p.get('quote_symbol', '')}".strip()


def card_text(p: dict, side: int, symbol: str, lang: str) -> dict:
    """The animated card's words: the title, the big number and the line under it."""
    if p.get("what"):  # a milestone
        mc = p["what"] == "mc"
        return {"title": f"{symbol} {'MC' if mc else t(lang, 'card_curve')}", "amount": p["chart_label"],
                "detail": t(lang, "card_new_high" if mc else "card_curve_full")}
    word = t(lang, "post_sell" if side < 0 else "post_buy").upper()
    return {"title": f"{symbol} {word}", "amount": trade_amount(p, side),
            "detail": f"{render.fmt_num(p['quote'])} {p.get('quote_symbol', '')}".strip()}


async def make_chart(db: Db, p: dict, side: int, mode: int = CHART_ANIMATED,
                     lang: str = "en") -> tuple[bytes, str] | None:
    """The chart for a trade payload as (bytes, kind), or None (charts off, no Pillow,
    too few trades, a failure). An animation that cannot be made falls back to a
    picture. Drawing runs off the event loop."""
    token = db.get_token(p["token"]) if p.get("token") else None
    if token is None or mode == CHART_OFF:
        return None
    points = db.trades_since(token.address, int(time.time() - CHART_HOURS * 3600))
    usd_per_quote = p["usd"] / p["quote"] if p.get("usd") and p.get("quote") else None
    if usd_per_quote is None and token.quote_kind == "usd":
        usd_per_quote = 1.0
    args = dict(symbol=token.symbol, supply=token.total_supply / 10 ** token.decimals,
                usd_per_quote=usd_per_quote, quote_symbol=token.quote_symbol, side=side)
    try:
        from . import chart
        if mode == CHART_ANIMATED:
            try:
                words = card_text(p, side, token.symbol, lang)
                mp4 = await asyncio.to_thread(chart.animate, points, **words, **args)
                if mp4:
                    return mp4, "animation"
            except Exception:
                log.exception("animated chart for %s failed, sending a picture", token.symbol)
        png = await asyncio.to_thread(chart.render, points, **args)
        return (png, "photo") if png else None
    except Exception:
        log.exception("chart for %s failed", token.symbol)
        return None


class Sender:
    def __init__(self, bot: Bot, db: Db, username: str, *, trade_url: str, explorer: str,
                 chat_interval: float = 3.0, global_rate: float = 25.0, max_age: float = 600.0):
        self.bot = bot
        self.db = db
        self.username = username
        self.trade_url = trade_url
        self.explorer = explorer
        self.chat_interval = chat_interval
        self.global_interval = 1.0 / global_rate
        self.max_age = max_age
        self._next_chat: dict[int, float] = {}
        self._next_global = 0.0
        self._variant = 0
        self.sent = 0
        self.dropped = 0

    async def run(self) -> None:
        while True:
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sender flush failed")
            await asyncio.sleep(0.5)

    async def flush(self) -> int:
        now = time.time()
        rows = self.db.pending_outbox(2000)
        stale = [r.id for r in rows if now - r.created_at > self.max_age]
        if stale:
            log.warning("dropping %d stale outbox rows", len(stale))
            self.db.delete_outbox(stale)
            self.dropped += len(stale)
            rows = [r for r in rows if r.id not in set(stale)]
        by_chat: dict[int, list[OutboxRow]] = {}
        for r in rows:
            by_chat.setdefault(r.chat_id, []).append(r)
        sent = 0
        for chat_id, items in by_chat.items():
            if self._next_chat.get(chat_id, 0) > time.time():
                continue
            chat = self.db.get_chat(chat_id)
            if chat is None or not chat.active:
                self.db.delete_outbox([r.id for r in items])
                continue
            if await self._post(chat, items):
                sent += 1
        return sent

    def _compose(self, chat: Chat, items: list[OutboxRow]):
        grads = [r for r in items if r.kind == "grad"]
        if grads:
            batch = [grads[0]]
            p = grads[0].payload
            text = render.graduation_text(p, chat.lang)
            pairs = render.buttons({"token": p["token"], "tx": p.get("tx")},
                                   self.trade_url, self.explorer)
            return batch, text, pairs, False, None
        levels = [r for r in items if r.kind == "milestone" and r.payload.get("token") == chat.token]
        if levels:
            batch = [levels[-1]]  # several at once: the highest says it all
            self.db.delete_outbox([r.id for r in levels[:-1]])
            p = dict(batch[0].payload)
            text = render.milestone_text(p, chat.lang)
            pairs = render.buttons({"token": p["token"], "tx": p.get("tx")}, self.trade_url, self.explorer)
            # what rises over the chart (its font has no emoji): $50K, or 75% for the curve
            p["chart_label"] = (render.fmt_usd(p["level"]) if p["what"] == "mc"
                                else f"{round(p['level'] * 100)}%")
            return batch, text, pairs, False, (p, 1)
        token = chat.token
        stale = [r for r in items if r.payload.get("token") != token
                 or (r.kind == "sell" and not chat.sells)]
        if stale:  # a token the chat no longer tracks, or sells it turned off
            self.db.delete_outbox([r.id for r in stale])
        items = [r for r in items if r not in stale]
        if not items:
            return [], "", [], False, None
        # Buys and sells are never merged: one post per kind, oldest kind first.
        kind = items[0].kind
        batch = [r for r in items if r.kind == kind]
        main, more = render.pick_main([r.payload for r in batch])
        text = render.buy_text(main, chat, more, explorer=self.explorer, sell=kind == "sell")
        pairs = render.buttons(main, self.trade_url, self.explorer)
        return batch, text, pairs, True, (main, -1 if kind == "sell" else 1)

    async def _post(self, chat: Chat, items: list[OutboxRow]) -> bool:
        batch, text, pairs, with_media, trade = self._compose(chat, items)
        if not batch:
            return False
        chart = None
        if trade and wants_chart(chat, with_media):
            chart = await make_chart(self.db, *trade, mode=chat.chart, lang=chat.lang)
        ids = [r.id for r in batch]
        pro = chat.is_pro()
        ad = None if pro else self.db.current_ad()
        self._variant += 1
        text += render.footer(pro, ad, self.username, self._variant // 7, chat.lang)

        wait = self._next_global - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._next_global = time.monotonic() + self.global_interval
        self._next_chat[chat.chat_id] = time.time() + self.chat_interval
        try:
            await send_post(self.bot, chat, text, pairs, with_media, chart)
        except TelegramRetryAfter as e:
            self._next_chat[chat.chat_id] = time.time() + e.retry_after + 1
            log.warning("flood wait %ss in %s", e.retry_after, chat.chat_id)
            return False
        except TelegramMigrateToChat as e:
            log.info("chat %s migrated to %s", chat.chat_id, e.migrate_to_chat_id)
            self.db.migrate_chat(chat.chat_id, e.migrate_to_chat_id)
            return False
        except TelegramForbiddenError:
            log.info("removed from chat %s, deactivating", chat.chat_id)
            self.db.deactivate_chat(chat.chat_id)
            return False
        except TelegramBadRequest as e:
            msg = e.message.lower()
            if chat.thread_id and any(h in msg for h in _NO_TOPIC):
                log.info("topic %s gone in %s, posting to the chat itself", chat.thread_id, chat.chat_id)
                self.db.update_chat(chat.chat_id, thread_id=None)
            elif any(h in msg for h in _GONE):
                log.info("cannot post in %s (%s), deactivating", chat.chat_id, e.message)
                self.db.deactivate_chat(chat.chat_id)
            elif with_media and chat.media_file_id and any(h in msg for h in _BAD_MEDIA):
                log.info("media broken in %s, falling back to text", chat.chat_id)
                self.db.update_chat(chat.chat_id, media_kind=None, media_file_id=None)
            else:
                log.warning("bad request in %s: %s", chat.chat_id, e.message)
                self._drop_after_retries(batch)
            return False
        except (TelegramNetworkError, TelegramServerError) as e:
            log.warning("telegram unavailable: %s", e)
            self._next_chat[chat.chat_id] = time.time() + 5
            return False
        self.db.delete_outbox(ids)
        self.sent += 1
        kinds = {r.kind for r in batch}
        if kinds <= {"buy", "sell"}:
            self.db.add_chat_stat(chat.chat_id, batch[0].kind, len(batch),
                                  sum(r.payload.get("usd") or 0 for r in batch))
        return True

    def _drop_after_retries(self, batch: list[OutboxRow]) -> None:
        dead = [r.id for r in batch if r.attempts >= 2]
        self.db.delete_outbox(dead)
        self.dropped += len(dead)
        self.db.bump_attempts([r.id for r in batch if r.attempts < 2])
