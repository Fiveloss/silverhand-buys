"""Telegram handlers: group and channel setup, the private chat's menu, Stars
payments, owner tools.

The bot only needs commands, so it works with privacy mode ON: it never reads
a group's conversation. Settings commands are for chat admins; in a channel
every post comes from an admin. A channel is set up either with commands posted
in it or from the private chat, where commands apply to the person's current
chat (picked in /chats). Every command gets an answer saying what was done.

Languages: a person's language is chosen on the first /start and changed with
/lang; a chat's language (/lang in the chat) sets its posts and the answers
there. Texts live in i18n.py. The private chat is a menu: each screen is an
animation (assets/menu/<lang>/<screen>.mp4) with its text as the caption.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import (JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter, Command,
                             CommandObject, CommandStart)
from aiogram.types import (CallbackQuery, ChatMemberUpdated, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaAnimation, LabeledPrice, Message,
                           PreCheckoutQuery, User)

from . import abi, i18n, render
from .config import Config
from .db import Chat, Db
from .i18n import t
from .sender import CHART_ANIMATED, CHART_OFF, CHART_STILL, make_chart, send_post, wants_chart
from .stats import token_day
from .tokens import TokenError, load_token, read_curve_constants

log = logging.getLogger(__name__)

GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
MENU_DIR = Path(__file__).resolve().parent.parent / "assets" / "menu"
CAPTION_MAX = 1024
ADMINS = (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
FOREVER = 4_102_444_800  # 2100-01-01: Pro that does not run out


@dataclass
class App:
    cfg: Config
    db: Db
    rpc: object
    eth_usd: object
    username: str
    watcher: object = None
    sender: object = None
    started: float = field(default_factory=time.time)
    note_ttl: float = 60.0  # seconds a command and its answer stay in a channel
    tasks: set = field(default_factory=set)


def _date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def until(ts: float, lang: str) -> str:
    """When Pro ends, or "forever"."""
    return t(lang, "forever") if ts >= FOREVER else _date(ts)


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def cb_btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def _kb(rows: list[list[tuple[str, str]]], callback: bool = False) -> InlineKeyboardMarkup:
    make = cb_btn if callback else url_btn
    return kb(*[[make(a, b) for a, b in row] for row in rows])


def plans_text(cfg: Config, lang: str) -> str:
    return " · ".join(f"{i18n.weeks(w, lang)}: {s} ⭐" for w, s in sorted(cfg.pro_plans.items()))


def plain_len(html: str) -> int:
    return len(re.sub(r"<[^>]+>", "", html).encode("utf-16-le")) // 2


async def is_admin(message: Message, bot: Bot) -> bool:
    if message.chat.type == ChatType.CHANNEL:
        return True  # only admins can post in a channel
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True  # an anonymous group admin
    if not message.from_user:
        return False
    return await is_chat_admin(bot, message.chat.id, message.from_user.id)


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        return False
    return member.status in ADMINS


def process_mb() -> int | str:
    """The bot's own memory (resident set), in MB; "?" where /proc is missing."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return "?"


def last_backup(app: App) -> str | None:
    folder = Path(app.cfg.db_path).parent / "backups"
    copies = sorted(folder.glob("bot-*.db")) if folder.exists() else []
    return copies[-1].stem[4:] if copies else None


def is_owner(app: App, user_id: int | None) -> bool:
    return user_id is not None and user_id in app.cfg.owner_ids


async def notify_owners(bot: Bot, app: App, text: str, markup=None) -> None:
    for uid in app.cfg.owner_ids:
        try:
            await bot.send_message(uid, text, reply_markup=markup)
        except TelegramAPIError as e:
            log.warning("cannot message owner %s: %s", uid, e)


# --------------------------------------------------------------------------- texts

def user_lang(app: App, user: User | None) -> str:
    if user is None:
        return "en"
    return i18n.norm(app.db.user_lang(user.id) or i18n.guess(user.language_code))


def chat_title(chat: Chat) -> str:
    return escape(chat.title or str(chat.chat_id))


def home_text(app: App, lang: str) -> str:
    return t(lang, "home", plans=plans_text(app.cfg, lang)) + "\n" + t(lang, "not_affiliated")


def terms_text(app: App, lang: str) -> str:
    source = t(lang, "terms_source", url=escape(app.cfg.source_url)) if app.cfg.source_url else ""
    return (t(lang, "terms", plans=plans_text(app.cfg, lang), ad_price=app.cfg.ad_day_stars)
            + source + "\n" + t(lang, "not_affiliated"))


def support_text(app: App, lang: str) -> str:
    contact = t(lang, "support_contact", c=escape(app.cfg.support_contact)) if app.cfg.support_contact else ""
    return t(lang, "support", contact=contact)


def ads_text(app: App, lang: str) -> str:
    return t(lang, "ads_info", chats=app.db.stats()["chats_tracking"], price=app.cfg.ad_day_stars,
             max_days=i18n.days(app.cfg.ad_max_days, lang))


def help_text(lang: str, private: bool = False) -> str:
    return (t(lang, "group_help") + ("\n" + t(lang, "help_private") + "\n" if private else "")
            + t(lang, "not_affiliated"))


def settings_text(app: App, chat_id: int, lang: str) -> str:
    chat = app.db.ensure_chat(chat_id)
    token = app.db.get_token(chat.token) if chat.token else None
    lines = [t(lang, "settings_title")]
    if token:
        venue = t(lang, "venue_v4" if token.graduated else "venue_curve")
        lines.append(t(lang, "set_token", symbol=escape(token.symbol), address=token.address, venue=venue))
        w = app.watcher
        if w is not None:
            age = time.time() - w.last_ok
            lines.append(t(lang, "set_live", s=int(age)) if age < 120 else t(lang, "set_stale", m=int(age // 60)))
    else:
        lines.append(t(lang, "set_no_token"))
    if chat.thread_id:
        lines.append(t(lang, "set_topic"))
    lines.append(t(lang, "set_minbuy", v=render.fmt_usd(chat.min_usd)))
    lines.append(t(lang, "set_emoji", e=chat.emoji))
    if chat.whale_usd:
        lines.append(t(lang, "set_whale", v=render.fmt_usd(chat.whale_usd)))
    lines.append(t(lang, "set_sells_on" if chat.sells else "set_sells_off"))
    lines.append(t(lang, {0: "set_chart_off", 2: "set_chart_still"}.get(chat.chart, "set_chart_on")))
    lines.append(t(lang, "set_lang", lang=i18n.LANG_NAMES[i18n.norm(chat.lang)]))
    lines.append(t(lang, "set_media", m=chat.media_kind or t(lang, "media_none"))
                 + ("" if chat.is_pro() else " (Pro)"))
    lines.append(t(lang, "set_pro", date=until(chat.pro_until, lang)) if chat.is_pro() else t(lang, "set_free"))
    return "\n".join(lines)


async def stats_text(app: App, chat: Chat, lang: str) -> str:
    token = app.db.get_token(chat.token) if chat.token else None
    if token is None:
        return t(lang, "stats_no_token")
    now = time.time()
    supply = token.total_supply / 10 ** token.decimals
    day = token_day(app.db.trades_since(token.address, int(now - 86400)), supply, now)
    rate = None
    if token.quote_kind == "usd":
        rate = 1.0
    elif token.quote_kind == "eth":
        rate = await app.eth_usd.get()
    sym = escape(token.quote_symbol)

    def money(q: float) -> str:
        return f"≈{render.fmt_usd(q * rate)}" if rate else f"{render.fmt_num(q)} {sym}"

    def cap(q: float) -> str:
        return render.fmt_usd(q * rate) if rate else f"{render.fmt_compact(q)} {sym}"

    lines = [t(lang, "stats_title", title=chat_title(chat), symbol=escape(token.symbol))]
    if day is None:
        lines.append(t(lang, "stats_no_trades"))
    else:
        lines.append(t(lang, "stats_window", h=max(1, round(day.hours))))
        lines.append(t(lang, "stats_buys", n=day.buys, usd=money(day.buy_volume),
                       q=f"{render.fmt_num(day.buy_volume)} {sym}",
                       word=i18n.plural(day.buys, lang, ("buy", "buys"), ("покупка", "покупки", "покупок"))))
        lines.append(t(lang, "stats_sells", n=day.sells, usd=money(day.sell_volume),
                       q=f"{render.fmt_num(day.sell_volume)} {sym}",
                       word=i18n.plural(day.sells, lang, ("sell", "sells"), ("продажа", "продажи", "продаж"))))
        net = day.buy_volume - day.sell_volume
        lines.append(t(lang, "stats_net", v=("+" if net >= 0 else "−") + money(abs(net)).lstrip("≈")))
        lines.append(t(lang, "stats_mc", now=cap(day.mc_now), hi=cap(day.mc_high), lo=cap(day.mc_low)))
        if day.change is not None:
            lines.append(t(lang, "stats_change", v=f"{'▲' if day.change >= 0 else '▼'} {abs(day.change) * 100:.1f}%"))
        if day.biggest_buy:
            lines.append(t(lang, "stats_whale", v=money(day.biggest_buy)))
    posted = app.db.chat_posted(chat.chat_id)
    lines += ["", t(lang, "stats_posted", d=posted["today"], w=posted["week"], a=posted["all"])]
    if rate and token.quote_kind == "eth":
        lines.append(t(lang, "stats_usd_note"))
    return "\n".join(lines)


async def top_text(app: App, chat: Chat, lang: str) -> str:
    """🏆 The wallets that bought the token most in the last 24 hours."""
    token = app.db.get_token(chat.token) if chat.token else None
    if token is None:
        return t(lang, "stats_no_token")
    rows, wallets = app.db.top_buyers(token.address, int(time.time() - 86400))
    if not rows:
        return t(lang, "top_none", symbol=escape(token.symbol))
    rate = 1.0 if token.quote_kind == "usd" else (await app.eth_usd.get() if token.quote_kind == "eth" else None)
    medals = ["🥇", "🥈", "🥉"]
    lines = [t(lang, "top_title", symbol=escape(token.symbol)), ""]
    for n, (wallet, quote, count) in enumerate(rows):
        who = render.short_addr(wallet)
        if app.cfg.explorer_url:
            who = f'<a href="{app.cfg.explorer_url}/address/{wallet}">{who}</a>'
        money = f"<b>{render.fmt_num(quote)} {escape(token.quote_symbol)}</b>"
        if rate:
            money += f" (≈{render.fmt_usd(quote * rate)})"
        word = i18n.plural(count, lang, ("buy", "buys"), ("покупка", "покупки", "покупок"))
        lines.append(f"{medals[n] if n < 3 else f'{n + 1}.'} {who} — {money} · {count} {word}")
    lines += ["", t(lang, "top_wallets", n=wallets)]
    return "\n".join(lines)


async def popup_text(app: App, token, lang: str) -> str:
    """The 📊 24h pop-up: plain text, within Telegram's 200 characters."""
    now = time.time()
    supply = token.total_supply / 10 ** token.decimals
    day = token_day(app.db.trades_since(token.address, int(now - 86400)), supply, now)
    if day is None:
        return t(lang, "pop_none", symbol=token.symbol)[:200]
    rate = 1.0 if token.quote_kind == "usd" else (await app.eth_usd.get() if token.quote_kind == "eth" else None)

    def money(q: float) -> str:
        return render.fmt_usd(q * rate) if rate else f"{render.fmt_num(q)} {token.quote_symbol}"

    change = "" if day.change is None else f"{'▲' if day.change >= 0 else '▼'} {abs(day.change) * 100:.1f}%"
    text = t(lang, "pop", symbol=token.symbol, h=max(1, round(day.hours)),
             buys=day.buys, bw=i18n.plural(day.buys, lang, ("buy", "buys"), ("покупка", "покупки", "покупок")),
             bv=money(day.buy_volume), sells=day.sells,
             sw=i18n.plural(day.sells, lang, ("sell", "sells"), ("продажа", "продажи", "продаж")),
             sv=money(day.sell_volume), mc=money(day.mc_now), change=change,
             hi=money(day.mc_high), lo=money(day.mc_low))
    return text[:200]


# --------------------------------------------------------------------------- router

def adopt(app: App, chat_id: int, user_id: int) -> None:
    """Record who manages a chat. The bot owner's own chats get Pro for good."""
    app.db.update_chat(chat_id, added_by=user_id)
    if is_owner(app, user_id) and app.db.get_chat(chat_id).pro_until < FOREVER:
        app.db.set_pro_until(chat_id, FOREVER)


async def claim_for_owners(bot: Bot, app: App) -> None:
    """On start: chats joined before the bot recorded who added it, where an owner
    is an admin, become the owner's (and so Pro)."""
    for c in app.db.unclaimed_chats():
        for uid in app.cfg.owner_ids:
            if await is_chat_admin(bot, c.chat_id, uid):
                adopt(app, c.chat_id, uid)
                log.info("chat %s (%s) claimed for owner %s", c.chat_id, c.title, uid)
                break


def build_router(app: App) -> Router:
    router = Router(name="buybot")

    # ---- settings actions, shared by group commands, channel posts and the private chat.
    # Each takes the language of whoever reads the answer.
    async def act_track(bot, chat_id, args, reply_to, lang):
        if not args or not abi.is_address(args[0]):
            return t(lang, "track_usage"), None
        address = args[0].lower()
        token = app.db.get_token(address)
        if token and not token.graduated and token.curve_k is None:
            # the curve could not be read last time: try again, so price and progress appear
            token.curve_k, token.phantom = await read_curve_constants(app.rpc, token.curve)
            app.db.upsert_token(token)
        if token is None:
            try:
                token = await load_token(app.rpc, address)
            except TokenError as e:
                return f"❌ {escape(str(e))}", None
            except Exception:
                log.exception("load_token %s failed", address)
                return t(lang, "chain_down"), None
            app.db.upsert_token(token)
        app.db.update_chat(chat_id, token=token.address)
        if app.watcher is not None and app.watcher.needs_backfill(token):
            # the first chart and /stats need history: load the last day in the background
            spawn(app.watcher.backfill(token))
        app.db.clear_chat_outbox(chat_id)
        chat = app.db.get_chat(chat_id)
        return t(lang, "tracking", symbol=escape(token.symbol), name=escape(token.name),
                 venue=t(lang, "track_v4" if token.graduated else "track_curve"),
                 sells=t(lang, "tracking_sells_on" if chat.sells else "tracking_sells_off"),
                 minbuy=render.fmt_usd(chat.min_usd)), None

    async def act_untrack(bot, chat_id, args, reply_to, lang):
        app.db.update_chat(chat_id, token=None)
        app.db.clear_chat_outbox(chat_id)
        return t(lang, "untracked"), None

    def _number(args, low, high):
        try:
            value = float((args[0] if args else "").lstrip("$"))
        except ValueError:
            return None
        return value if low <= value <= high else None

    async def act_minbuy(bot, chat_id, args, reply_to, lang):
        value = _number(args, 0, 1_000_000)
        if value is None:
            return t(lang, "minbuy_usage"), None
        app.db.update_chat(chat_id, min_usd=value)
        return t(lang, "minbuy_set", v=render.fmt_usd(value)), None

    async def act_whale(bot, chat_id, args, reply_to, lang):
        value = _number(args, 0, 100_000_000)
        if value is None:
            return t(lang, "whale_usage", v=render.fmt_usd(app.db.get_chat(chat_id).whale_usd)), None
        app.db.update_chat(chat_id, whale_usd=value)
        return (t(lang, "whale_set", v=render.fmt_usd(value)) if value else t(lang, "whale_off")), None

    async def act_emoji(bot, chat_id, args, reply_to, lang):
        value = "".join(args)
        if not value or len(value) > 10 or any(c.isascii() and (c.isalnum() or c in "<>&\"'")
                                               for c in value):
            return t(lang, "emoji_usage"), None
        app.db.update_chat(chat_id, emoji=value)
        return t(lang, "emoji_set", v=value), None

    async def act_media(bot, chat_id, args, reply_to, lang):
        chat = app.db.get_chat(chat_id)
        if args and args[0].lower() == "off":
            app.db.update_chat(chat_id, media_kind=None, media_file_id=None)
            return t(lang, "media_removed"), None
        if not chat.is_pro():
            return t(lang, "media_pro"), None
        kind = file_id = None
        if reply_to and reply_to.animation:
            kind, file_id = "animation", reply_to.animation.file_id
        elif reply_to and reply_to.video:
            kind, file_id = "video", reply_to.video.file_id
        elif reply_to and reply_to.photo:
            kind, file_id = "photo", reply_to.photo[-1].file_id
        if not file_id:
            return t(lang, "media_usage"), None
        app.db.update_chat(chat_id, media_kind=kind, media_file_id=file_id)
        return t(lang, "media_set", kind=kind), None

    def _switch(args) -> str | None:
        value = args[0].lower() if args else ""
        return value if value in ("on", "off") else None

    async def act_sells(bot, chat_id, args, reply_to, lang):
        value = _switch(args)
        if value is None:
            state = t(lang, "on" if app.db.get_chat(chat_id).sells else "off")
            return t(lang, "sells_state", state=state), None
        app.db.update_chat(chat_id, sells=int(value == "on"))
        return t(lang, "sells_on" if value == "on" else "sells_off"), None

    async def act_chart(bot, chat_id, args, reply_to, lang):
        modes = {"on": CHART_ANIMATED, "anim": CHART_ANIMATED, "still": CHART_STILL, "off": CHART_OFF}
        value = args[0].lower() if args else ""
        if value not in modes:
            current = app.db.get_chat(chat_id).chart
            state = t(lang, {CHART_OFF: "off", CHART_STILL: "state_still"}.get(current, "state_animated"))
            return t(lang, "chart_state", state=state), None
        app.db.update_chat(chat_id, chart=modes[value])
        return t(lang, {CHART_OFF: "chart_off", CHART_STILL: "chart_still"}.get(modes[value], "chart_on")), None

    async def act_lang(bot, chat_id, args, reply_to, lang):
        value = args[0].lower() if args else ""
        if value not in i18n.LANGS:
            current = i18n.LANG_NAMES[i18n.norm(app.db.get_chat(chat_id).lang)]
            return t(lang, "lang_chat_usage", current=current), None
        app.db.update_chat(chat_id, lang=value)
        return t(value, "lang_chat_set"), None

    async def act_settings(bot, chat_id, args, reply_to, lang):
        return settings_text(app, chat_id, lang), None

    async def act_stats(bot, chat_id, args, reply_to, lang):
        return await stats_text(app, app.db.get_chat(chat_id), lang), None

    async def act_top(bot, chat_id, args, reply_to, lang):
        return await top_text(app, app.db.get_chat(chat_id), lang), None

    async def act_test(bot, chat_id, args, reply_to, lang):
        chat = app.db.get_chat(chat_id)
        token = app.db.get_token(chat.token) if chat.token else None
        if not token:
            return t(lang, "test_first"), None
        eth_usd = await app.eth_usd.get() if token.quote_kind == "eth" else None
        p = render.sample_payload(token.symbol, token.quote_symbol, eth_usd)
        p["token"] = token.address
        text = render.buy_text(p, chat, explorer=app.cfg.explorer_url, test=True)
        text += render.footer(chat.is_pro(), None if chat.is_pro() else app.db.current_ad(),
                              app.username, lang=chat.lang)
        try:
            if wants_chart(chat, True) and app.watcher is not None \
                    and app.db.last_trade_ts(token.address) is None:
                try:  # right after /track the history may still be loading
                    await asyncio.wait_for(app.watcher.backfill(token), 20)
                except Exception as e:
                    log.info("no history for the test chart of %s: %s", token.symbol, e)
            chart = (await make_chart(app.db, p, 1, mode=chat.chart, lang=chat.lang)
                     if wants_chart(chat, True) else None)
            await send_post(bot, chat, text, render.buttons(p, app.cfg.trade_url, ""), True, chart)
        except TelegramAPIError as e:
            return t(lang, "test_failed", e=escape(str(e))), None
        return None, None

    async def act_pro(bot, chat_id, args, reply_to, lang):
        chat = app.db.get_chat(chat_id)
        status = t(lang, "pro_until", date=until(chat.pro_until, lang)) if chat.is_pro() else t(lang, "pro_free")
        url = f"https://t.me/{app.username}?start=pro_{chat_id}"
        return (t(lang, "pro_text", plans=plans_text(app.cfg, lang), status=status),
                _kb([[(t(lang, "btn_get_pro"), url)]]))

    async def act_help(bot, chat_id, args, reply_to, lang):
        return help_text(lang), None

    actions = {"track": act_track, "untrack": act_untrack, "minbuy": act_minbuy,
               "emoji": act_emoji, "whale": act_whale, "media": act_media, "sells": act_sells,
               "chart": act_chart, "lang": act_lang, "stats": act_stats, "top": act_top,
               "settings": act_settings, "test": act_test, "pro": act_pro, "help": act_help}

    def spawn(coro):
        task = asyncio.create_task(coro)
        app.tasks.add(task)
        task.add_done_callback(app.tasks.discard)

    # ---- joining and leaving chats
    @router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
    async def joined(event: ChatMemberUpdated, bot: Bot):
        if event.chat.type == ChatType.PRIVATE:
            return
        lang = user_lang(app, event.from_user)
        app.db.ensure_chat(event.chat.id, event.chat.title or "")
        app.db.update_chat(event.chat.id, lang=lang, kind=event.chat.type)
        adopt(app, event.chat.id, event.from_user.id)
        try:
            if event.chat.type == ChatType.CHANNEL:
                # Never post setup chatter into a channel; tell whoever added the bot instead.
                app.db.set_dm_chat(event.from_user.id, event.chat.id)
                await bot.send_message(event.from_user.id, t(lang, "channel_welcome",
                                                             title=escape(event.chat.title or ""),
                                                             chat_id=event.chat.id))
            else:
                await bot.send_message(event.chat.id, t(lang, "group_welcome") + t(lang, "not_affiliated"))
        except TelegramAPIError:
            pass  # the person never opened the bot, or the bot cannot post yet

    @router.my_chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
    async def left(event: ChatMemberUpdated):
        if event.chat.type != ChatType.PRIVATE:
            app.db.deactivate_chat(event.chat.id)

    @router.message(F.migrate_to_chat_id)
    async def migrated(message: Message):
        app.db.migrate_chat(message.chat.id, message.migrate_to_chat_id)

    def seen(message: Message) -> Chat:
        chat = app.db.ensure_chat(message.chat.id, message.chat.title or "")
        if chat.kind != message.chat.type:
            app.db.update_chat(chat.chat_id, kind=message.chat.type)
        return app.db.get_chat(chat.chat_id)

    # ---- group commands (admins only; everyone else is ignored so nobody can spam the chat)
    @router.message(Command(*actions), F.chat.type.in_(GROUPS))
    async def group_command(message: Message, bot: Bot, command: CommandObject):
        if not await is_admin(message, bot):
            return
        chat = seen(message)
        if chat.added_by is None and message.from_user:
            adopt(app, chat.chat_id, message.from_user.id)
        name = command.command.lower()
        if name == "track":
            # /track sent in a forum topic sends the posts there; sent outside one, to the chat
            app.db.update_chat(chat.chat_id, thread_id=message.message_thread_id
                               if message.is_topic_message else None)
        text, markup = await actions[name](
            bot, message.chat.id, (command.args or "").split(), message.reply_to_message, chat.lang)
        if text:
            await message.reply(text, reply_markup=markup)

    # ---- commands posted in a channel: only its admins can post there. The answer goes
    # out silently and is removed together with the command after app.note_ttl seconds,
    # so subscribers are not left with setup chatter.
    async def tidy_later(bot: Bot, chat_id: int, message_ids: list[int]):
        await asyncio.sleep(app.note_ttl)
        try:
            await bot.delete_messages(chat_id, message_ids)
        except TelegramAPIError as e:
            log.info("cannot tidy up %s in %s: %s", message_ids, chat_id, e)

    @router.channel_post(Command(*actions))
    async def channel_post_command(message: Message, bot: Bot, command: CommandObject):
        chat = seen(message)
        text, markup = await actions[command.command.lower()](
            bot, message.chat.id, (command.args or "").split(), message.reply_to_message, chat.lang)
        lang = app.db.get_chat(chat.chat_id).lang  # /lang may have just changed it
        ids = [message.message_id]
        if text:
            try:
                note = await bot.send_message(message.chat.id, text + "\n\n" + t(lang, "channel_note"),
                                              reply_markup=markup, disable_notification=True)
                ids.append(note.message_id)
            except TelegramAPIError as e:
                log.warning("cannot answer in channel %s: %s", message.chat.id, e)
        spawn(tidy_later(bot, message.chat.id, ids))

    # ---- chats managed from the private chat
    async def resolve_chat_id(bot: Bot, ref: str) -> int | None:
        """A chat as people write it: -1002350230407, 2350230407 (the form in
        t.me/c/ links and some apps), @name or a t.me link."""
        ref = ref.strip()
        for prefix in ("https://", "http://", "t.me/", "c/"):
            if ref.startswith(prefix):
                ref = ref[len(prefix):]
        ref = ref.split("/")[0]
        if ref.lstrip("-").isdigit():
            n = int(ref)
            return n if n < 0 else int(f"-100{n}")
        name = ref.lstrip("@")
        if not name:
            return None
        try:
            return (await bot.get_chat(f"@{name}")).id
        except TelegramAPIError:
            return None

    async def manageable(bot: Bot, user_id: int, chat_id: int, lang: str) -> tuple[Chat | None, str | None]:
        """The chat if this person may manage it, else the reason why not."""
        chat = app.db.get_chat(chat_id)
        if not chat or not chat.active:
            return None, t(lang, "not_in_chat", chat_id=chat_id)
        if not await is_chat_admin(bot, chat_id, user_id):
            return None, t(lang, "only_admins")
        if chat.added_by is None:
            adopt(app, chat_id, user_id)  # joined before the bot recorded who added it
        app.db.set_dm_chat(user_id, chat_id)
        return chat, None

    async def run_in_chat(message: Message, bot: Bot, chat_id: int, name: str, args: list[str]):
        lang = user_lang(app, message.from_user)
        chat, why = await manageable(bot, message.from_user.id, chat_id, lang)
        if chat is None:
            await message.answer(why)
            return
        text, markup = await actions[name](bot, chat_id, args, message.reply_to_message, lang)
        if text is None and name == "test":
            text = t(lang, "test_sent")
        await message.answer(f"{kind_icon(chat)} <b>{chat_title(chat)}</b>\n{text or t(lang, 'done')}",
                             reply_markup=markup)

    def kind_icon(chat: Chat) -> str:
        return "📢" if chat.kind == ChatType.CHANNEL else "👥" if chat.kind else "💬"

    async def my_chats(bot: Bot, user_id: int) -> list[Chat]:
        """Chats this person added the bot to; older chats they admin are claimed on the way."""
        chats = {c.chat_id: c for c in app.db.chats_of_user(user_id)}
        for c in app.db.unclaimed_chats():
            if await is_chat_admin(bot, c.chat_id, user_id):
                adopt(app, c.chat_id, user_id)
                chats[c.chat_id] = app.db.get_chat(c.chat_id)
        return sorted(chats.values(), key=lambda c: c.created_at)

    @router.message(Command("channel"), F.chat.type == ChatType.PRIVATE)
    async def channel_command(message: Message, bot: Bot, command: CommandObject):
        args = (command.args or "").split()
        lang = user_lang(app, message.from_user)
        if len(args) < 2 or args[1].lower() not in actions:
            await message.answer(t(lang, "channel_usage"))
            return
        chat_id = await resolve_chat_id(bot, args[0])
        if chat_id is None:
            await message.answer(t(lang, "channel_usage"))
            return
        await run_in_chat(message, bot, chat_id, args[1].lower(), args[2:])

    # ---- the private chat: a menu of animated screens
    private = F.chat.type == ChatType.PRIVATE

    def anim(lang: str, key: str):
        """(file_id or file, cache key) of a screen's animation, or None."""
        for folder in (lang, "en"):
            path = MENU_DIR / folder / f"{key}.mp4"
            if path.exists():
                cache = f"anim:{folder}:{key}:{path.stat().st_size}"
                return app.db.get_meta(cache) or FSInputFile(path), cache
        return None

    def remember(cache: str, msg) -> None:
        animation = getattr(msg, "animation", None)
        if animation and animation.file_id:
            app.db.set_meta(cache, animation.file_id)

    async def send_screen(bot: Bot, chat_id: int, lang: str, key: str, text: str, markup=None):
        media = anim(lang, key) if plain_len(text) <= CAPTION_MAX else None
        if media:
            try:
                msg = await bot.send_animation(chat_id, media[0], caption=text, reply_markup=markup)
                remember(media[1], msg)
                return msg
            except TelegramAPIError as e:
                log.warning("screen %s without animation: %s", key, e)
        return await bot.send_message(chat_id, text, reply_markup=markup)

    async def edit_screen(cb: CallbackQuery, lang: str, key: str, text: str, markup=None):
        msg = cb.message
        media = anim(lang, key) if plain_len(text) <= CAPTION_MAX else None
        try:
            if media and msg is not None and getattr(msg, "animation", None):
                res = await msg.edit_media(InputMediaAnimation(media=media[0], caption=text),
                                           reply_markup=markup)
                remember(media[1], res)
                return
            if not media and msg is not None and getattr(msg, "text", None) is not None:
                await msg.edit_text(text, reply_markup=markup)
                return
        except TelegramBadRequest as e:
            if "not modified" in e.message:
                return
            log.info("screen %s sent anew: %s", key, e.message)
        await send_screen(cb.bot, cb.from_user.id, lang, key, text, markup)

    def home_markup(lang: str, user_id: int | None = None) -> InlineKeyboardMarkup:
        panel = [cb_btn(t(lang, "btn_panel"), "a:home")] if is_owner(app, user_id) else []
        return kb(panel,
                  [url_btn(t(lang, "btn_add_group"), f"https://t.me/{app.username}?startgroup=true"),
                   url_btn(t(lang, "btn_add_channel"),
                           f"https://t.me/{app.username}?startchannel=true&admin=post_messages")],
                  [cb_btn(t(lang, "btn_chats"), "m:chats"), cb_btn(t(lang, "btn_stats"), "m:stats")],
                  [cb_btn(t(lang, "btn_pro"), "m:pro"), cb_btn(t(lang, "btn_ads"), "m:ads")],
                  [cb_btn(t(lang, "btn_help"), "m:help"), cb_btn(t(lang, "btn_lang"), "m:lang")],
                  [cb_btn(t(lang, "btn_terms"), "m:terms"), cb_btn(t(lang, "btn_support"), "m:support")])

    def back(lang: str) -> list[InlineKeyboardButton]:
        return [cb_btn(t(lang, "back"), "m:home")]

    def lang_markup() -> InlineKeyboardMarkup:
        return kb([cb_btn(i18n.LANG_NAMES[code], f"lang:{code}") for code in i18n.LANGS])

    async def screen(bot: Bot, user: User, name: str, arg: str = "") -> tuple[str, str, InlineKeyboardMarkup]:
        """(animation key, caption, buttons) of a menu screen, in the person's language."""
        lang = user_lang(app, user)
        if name == "lang":
            return "lang", t(lang, "lang_pick"), kb(*lang_markup().inline_keyboard, back(lang))
        if name == "chats":
            chats = await my_chats(bot, user.id)
            if not chats:
                return "chats", t(lang, "chats_none"), kb(back(lang))
            current = app.db.dm_chat(user.id)
            lines = [t(lang, "chats_title"), ""]
            rows = []
            for c in chats:
                token = app.db.get_token(c.token) if c.token else None
                marks = [escape(token.symbol) if token else t(lang, "no_token")]
                if c.sells:
                    marks.append("🔴")
                if c.chart:
                    marks.append("📈")
                if c.is_pro():
                    marks.append("⭐")
                pick = "▶️ " if c.chat_id == current else ""
                lines.append(pick + t(lang, "chat_line", kind=kind_icon(c), title=chat_title(c),
                                      token=" · ".join(marks)))
                rows.append([cb_btn(f"{pick}{kind_icon(c)} {c.title or c.chat_id}"[:60], f"m:chat:{c.chat_id}")])
            lines += ["", t(lang, "chats_hint")]
            return "chats", "\n".join(lines), kb(*rows, back(lang))
        if name == "chat":
            chat, why = await manageable(bot, user.id, int(arg), lang)
            if chat is None:
                return "chats", why, kb([cb_btn(t(lang, "back_chats"), "m:chats")])
            cid = chat.chat_id
            other = "ru" if chat.lang == "en" else "en"
            text = f"{kind_icon(chat)} <b>{chat_title(chat)}</b>\n\n" + settings_text(app, cid, lang)
            return "chats", text, kb(
                [cb_btn(t(lang, "btn_test"), f"c:test:{cid}"), cb_btn(t(lang, "btn_stats"), f"c:stats:{cid}"),
                 cb_btn(t(lang, "btn_top"), f"c:top:{cid}")],
                [cb_btn(t(lang, "btn_sells", state=t(lang, "off" if chat.sells else "on")), f"c:sells:{cid}"),
                 cb_btn(t(lang, "btn_chart", state=t(lang, "off" if chat.chart else "on")), f"c:chart:{cid}")],
                [cb_btn(t(lang, "btn_chat_lang", lang=i18n.LANG_NAMES[other]), f"c:lang:{cid}"),
                 cb_btn(t(lang, "btn_get_pro"), f"c:pro:{cid}")],
                [cb_btn(t(lang, "btn_refresh"), f"m:chat:{cid}"), cb_btn(t(lang, "back_chats"), "m:chats")])
        if name == "stats":
            cid = int(arg) if arg else app.db.dm_chat(user.id)
            chat = app.db.get_chat(cid) if cid else None
            if chat is None or not chat.active:
                return "stats", t(lang, "stats_pick"), kb([cb_btn(t(lang, "btn_chats"), "m:chats")], back(lang))
            return "stats", await stats_text(app, chat, lang), kb(
                [cb_btn(t(lang, "btn_refresh"), f"m:stats:{cid}"), cb_btn(t(lang, "back_chats"), f"m:chat:{cid}")],
                back(lang))
        if name == "pro":
            cid = app.db.dm_chat(user.id)
            chat = app.db.get_chat(cid) if cid else None
            rows = [[cb_btn(f"{t(lang, 'btn_get_pro')}: {chat.title or cid}"[:60], f"c:pro:{cid}")]] \
                if chat and chat.active else []
            return "pro", t(lang, "pro_menu", plans=plans_text(app.cfg, lang)), kb(*rows, back(lang))
        if name == "ads":
            return "ads", ads_text(app, lang), kb(back(lang))
        if name == "help":
            return "help", help_text(lang, private=True), kb(back(lang))
        if name == "terms":
            return "terms", terms_text(app, lang), kb(back(lang))
        if name == "support":
            return "support", support_text(app, lang), kb(back(lang))
        return "start", home_text(app, lang), home_markup(lang, user.id)

    async def show(message: Message, bot: Bot, name: str, arg: str = ""):
        key, text, markup = await screen(bot, message.from_user, name, arg)
        await send_screen(bot, message.chat.id, user_lang(app, message.from_user), key, text, markup)

    @router.callback_query(F.data.startswith("m:"))
    async def menu(cb: CallbackQuery, bot: Bot):
        _, name, *rest = cb.data.split(":", 2)
        key, text, markup = await screen(bot, cb.from_user, name, rest[0] if rest else "")
        await cb.answer()
        await edit_screen(cb, user_lang(app, cb.from_user), key, text, markup)

    @router.callback_query(F.data.startswith("s:"))
    async def token_popup(cb: CallbackQuery):
        """📊 24h under a post: the token's day, shown only to whoever tapped it."""
        lang = user_lang(app, cb.from_user)
        token = app.db.get_token(cb.data[2:])
        if token is None:
            await cb.answer()
            return
        await cb.answer(await popup_text(app, token, lang), show_alert=True)

    @router.callback_query(F.data.startswith("lang:"))
    async def pick_lang(cb: CallbackQuery, bot: Bot):
        code = cb.data.split(":", 1)[1]
        if code not in i18n.LANGS:
            await cb.answer()
            return
        app.db.set_user_lang(cb.from_user.id, code)
        await cb.answer(t(code, "lang_set"))
        key, text, markup = await screen(bot, cb.from_user, "home")
        await edit_screen(cb, code, key, text, markup)

    @router.callback_query(F.data.startswith("c:"))
    async def chat_button(cb: CallbackQuery, bot: Bot):
        """Buttons of a chat's screen: test post, stats, sells, chart, language, Pro."""
        lang = user_lang(app, cb.from_user)
        try:
            _, act, cid = cb.data.split(":")
            cid = int(cid)
        except ValueError:
            await cb.answer()
            return
        chat, why = await manageable(bot, cb.from_user.id, cid, lang)
        if chat is None:
            await cb.answer(re.sub(r"<[^>]+>", "", why)[:190], show_alert=True)
            return
        if act == "stats":
            await cb.answer()
            key, text, markup = await screen(bot, cb.from_user, "stats", str(cid))
            await edit_screen(cb, lang, key, text, markup)
            return
        if act == "top":
            await cb.answer()
            await edit_screen(cb, lang, "stats", await top_text(app, chat, lang),
                              kb([cb_btn(t(lang, "btn_refresh"), f"c:top:{cid}"),
                                  cb_btn(t(lang, "back_chats"), f"m:chat:{cid}")]))
            return
        if act == "pro":
            await cb.answer()
            await show_pro_offer(cb.message, cid, lang)
            return
        if act == "test":
            text, _ = await act_test(bot, cid, [], None, lang)
            note = t(lang, "test_sent") if text is None else text
        elif act == "sells":
            note, _ = await act_sells(bot, cid, ["off" if chat.sells else "on"], None, lang)
        elif act == "chart":
            note, _ = await act_chart(bot, cid, ["off" if chat.chart else "on"], None, lang)
        elif act == "lang":
            note, _ = await act_lang(bot, cid, ["ru" if chat.lang == "en" else "en"], None, lang)
        else:
            await cb.answer()
            return
        await cb.answer(re.sub(r"<[^>]+>", "", note)[:190])
        key, text, markup = await screen(bot, cb.from_user, "chat", str(cid))
        await edit_screen(cb, lang, key, text, markup)

    @router.message(CommandStart(deep_link=True), private)
    async def start_deep(message: Message, bot: Bot, command: CommandObject):
        arg = command.args or ""
        if arg.startswith("pro_"):
            try:
                chat_id = int(arg[4:])
            except ValueError:
                chat_id = 0
            await show_pro_offer(message, chat_id, user_lang(app, message.from_user))
        elif arg == "ads":
            await show(message, bot, "ads")
        else:
            await start(message, bot)

    @router.message(CommandStart(), private)
    async def start(message: Message, bot: Bot):
        if app.db.user_lang(message.from_user.id) is None:
            # first visit: the language first, in both languages at once
            await send_screen(bot, message.chat.id, user_lang(app, message.from_user), "lang",
                              t("en", "lang_pick"), lang_markup())
            return
        await show(message, bot, "home")

    @router.message(Command("lang"), private)
    async def lang_private(message: Message, bot: Bot, command: CommandObject):
        code = (command.args or "").strip().lower()
        if code in i18n.LANGS:
            app.db.set_user_lang(message.from_user.id, code)
            await message.answer(t(code, "lang_set"))
            await show(message, bot, "home")
            return
        await show(message, bot, "lang")

    @router.message(Command("chats"), private)
    async def chats_command(message: Message, bot: Bot):
        await show(message, bot, "chats")

    @router.message(Command("help"), private)
    async def help_private(message: Message, bot: Bot):
        await show(message, bot, "help")

    @router.message(Command("terms"), private)
    async def terms(message: Message, bot: Bot):
        await show(message, bot, "terms")

    @router.message(Command("paysupport"), private)
    async def paysupport(message: Message, bot: Bot, command: CommandObject):
        user = message.from_user
        if command.args and user:
            await notify_owners(bot, app, f"🆘 /paysupport from {user.id} "
                                          f"(@{escape(user.username or '-')}):\n{escape(command.args)}")
            await message.answer(t(user_lang(app, user), "support_sent"))
            return
        await show(message, bot, "support")

    # Group commands sent here apply to this person's current chat
    # (/help and /lang have their own answers above).
    @router.message(Command(*(a for a in actions if a not in ("help", "lang"))), private)
    async def private_action(message: Message, bot: Bot, command: CommandObject):
        name = command.command.lower()
        chat_id = app.db.dm_chat(message.from_user.id)
        if chat_id is None:
            await message.answer(t(user_lang(app, message.from_user), "no_current_chat", name=name))
            return
        await run_in_chat(message, bot, chat_id, name, (command.args or "").split())

    # A post forwarded from a channel picks that channel for the commands here.
    @router.message(private, F.forward_origin.chat.id.as_("origin_id"))
    async def forwarded_from_channel(message: Message, bot: Bot, origin_id: int):
        await run_in_chat(message, bot, origin_id, "settings", [])

    async def show_pro_offer(message: Message, chat_id: int, lang: str):
        chat = app.db.get_chat(chat_id)
        if not chat or not chat.active:
            await message.answer(t(lang, "pro_not_in_chat"))
            return
        rows = [[(f"{i18n.weeks(w, lang)} — {s} ⭐", f"pro:{chat_id}:{w}")]
                for w, s in sorted(app.cfg.pro_plans.items())]
        status = t(lang, "pro_until", date=until(chat.pro_until, lang)) if chat.is_pro() else t(lang, "pro_status_free")
        await message.answer(t(lang, "pro_offer", title=chat_title(chat), status=status),
                             reply_markup=_kb(rows, callback=True))

    @router.callback_query(F.data.startswith("pro:"))
    async def buy_pro(cb: CallbackQuery, bot: Bot):
        lang = user_lang(app, cb.from_user)
        try:
            _, chat_id, weeks = cb.data.split(":")
            chat_id, weeks = int(chat_id), int(weeks)
        except ValueError:
            await cb.answer(t(lang, "try_again"), show_alert=True)
            return
        stars = app.cfg.pro_plans.get(weeks)
        chat = app.db.get_chat(chat_id)
        if not stars or not chat:
            await cb.answer(t(lang, "offer_invalid"), show_alert=True)
            return
        label = f"Pro · {i18n.weeks(weeks, lang)}"
        await bot.send_invoice(cb.from_user.id, title=label,
                               description=t(lang, "invoice_pro_desc", title=chat.title or chat_id)[:255],
                               payload=f"pro:{chat_id}:{weeks}", currency="XTR",
                               prices=[LabeledPrice(label=label, amount=stars)])
        await cb.answer()

    @router.message(Command("advertise"), private)
    async def advertise(message: Message, bot: Bot, command: CommandObject):
        lang = user_lang(app, message.from_user)
        parts = (command.args or "").split(maxsplit=2)
        if len(parts) < 3:
            await show(message, bot, "ads")
            return
        days_s, url, text = parts
        text = " ".join(text.split())
        try:
            days = int(days_s)
        except ValueError:
            days = 0
        if not 1 <= days <= app.cfg.ad_max_days:
            await message.answer(t(lang, "ads_days", n=app.cfg.ad_max_days))
            return
        if not url.startswith("https://") or len(url) > 200 or any(c in url for c in "<>\"' "):
            await message.answer(t(lang, "ads_link"))
            return
        if not 3 <= len(text) <= 60:
            await message.answer(t(lang, "ads_text"))
            return
        if not app.cfg.owner_ids:
            await message.answer(t(lang, "ads_closed"))
            return
        price = days * app.cfg.ad_day_stars
        ad_id = app.db.create_ad(message.from_user.id, text, url, days, price)
        await message.answer(t(lang, "ads_thanks", id=ad_id, days=i18n.days(days, lang), price=price))
        user = message.from_user
        await notify_owners(
            bot, app,
            f"📣 Ad #{ad_id} from {user.id} (@{escape(user.username or '-')}), {days} d, {price} ⭐\n"
            f"Text: {escape(text)}\nLink: {escape(url)}",
            _kb([[("✅ Approve", f"ad:ok:{ad_id}"), ("❌ Reject", f"ad:no:{ad_id}")]], callback=True))

    @router.callback_query(F.data.startswith("ad:"))
    async def review_ad(cb: CallbackQuery, bot: Bot):
        if not is_owner(app, cb.from_user.id):
            await cb.answer()
            return
        try:
            _, verdict, ad_id = cb.data.split(":")
            ad = app.db.get_ad(int(ad_id))
        except ValueError:
            ad = None
        if not ad or ad.status != "pending":
            await cb.answer("Already handled.", show_alert=True)
            return
        if verdict == "ok":
            app.db.set_ad_status(ad.id, "approved")
            note = "✅ approved, invoice sent"
            try:
                await bot.send_invoice(ad.user_id, title=f"Ad slot · {ad.days} day{'s' if ad.days > 1 else ''}",
                                       description=ad.text, payload=f"ad:{ad.id}", currency="XTR",
                                       prices=[LabeledPrice(label="Ad slot", amount=ad.price)])
            except TelegramAPIError as e:
                note = f"✅ approved, but the invoice failed: {escape(str(e))}"
        else:
            app.db.set_ad_status(ad.id, "rejected")
            note = "❌ rejected"
            try:
                await bot.send_message(ad.user_id, t(app.db.user_lang(ad.user_id), "ads_rejected", id=ad.id))
            except TelegramAPIError:
                pass
        await cb.answer()
        if cb.message:
            await cb.message.edit_text(f"{cb.message.html_text}\n\n{note}")

    # ---- payments
    def check_payload(payload: str, amount: int, currency: str, lang: str) -> str | None:
        """None if the invoice can still be honoured, else the reason shown to the payer."""
        if currency != "XTR":
            return t(lang, "pay_currency")
        kind, _, rest = payload.partition(":")
        if kind == "pro":
            try:
                chat_id, weeks = (int(x) for x in rest.split(":"))
            except ValueError:
                return t(lang, "pay_invalid")
            chat = app.db.get_chat(chat_id)
            if not chat or not chat.active:
                return t(lang, "pay_left")
            if app.cfg.pro_plans.get(weeks) != amount:
                return t(lang, "pay_prices")
            return None
        if kind == "ad":
            ad = app.db.get_ad(int(rest)) if rest.isdigit() else None
            if not ad or ad.status != "approved" or ad.price != amount:
                return t(lang, "pay_ad_invalid")
            return None
        return t(lang, "pay_invalid")

    @router.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery):
        reason = check_payload(query.invoice_payload, query.total_amount, query.currency,
                               user_lang(app, query.from_user))
        await query.answer(ok=reason is None, error_message=reason)

    @router.message(F.successful_payment)
    async def paid(message: Message, bot: Bot):
        sp = message.successful_payment
        user_id = message.from_user.id if message.from_user else 0
        lang = user_lang(app, message.from_user)
        if not app.db.record_payment(sp.telegram_payment_charge_id, user_id, sp.total_amount,
                                     sp.invoice_payload):
            return  # Telegram repeated the update
        kind, _, rest = sp.invoice_payload.partition(":")
        if kind == "pro":
            chat_id, weeks = (int(x) for x in rest.split(":"))
            ends = app.db.extend_pro(chat_id, weeks * 7 * 86400)
            chat = app.db.get_chat(chat_id)
            await message.answer(t(lang, "pro_paid", title=chat_title(chat), date=_date(ends)))
            try:
                await bot.send_message(chat_id, t(chat.lang, "pro_paid_chat"))
            except TelegramAPIError:
                pass
        elif kind == "ad":
            starts, ends = app.db.schedule_paid_ad(int(rest), sp.telegram_payment_charge_id)
            await message.answer(t(lang, "ad_paid", starts=_date(starts), ends=_date(ends)))
        await notify_owners(bot, app, f"💰 {sp.total_amount} ⭐ from {user_id}: "
                                      f"{escape(sp.invoice_payload)}\n"
                                      f"charge <code>{sp.telegram_payment_charge_id}</code>")

    # ---- owner tools
    owner_only = F.from_user.id.in_(app.cfg.owner_ids)

    def panel_back(lang: str) -> list[InlineKeyboardButton]:
        return [cb_btn(t(lang, "adm_btn_back"), "a:home")]

    def chat_rows(chats: list[Chat], lang: str) -> tuple[list[str], list[list[InlineKeyboardButton]]]:
        lines, rows = [], []
        for c in chats:
            token = app.db.get_token(c.token) if c.token else None
            pro = f" · ⭐ {until(c.pro_until, lang)}" if c.is_pro() else ""
            lines.append(f"{kind_icon(c)} <b>{chat_title(c)}</b> — "
                         f"{escape(token.symbol) if token else t(lang, 'no_token')}{pro}")
            rows.append([cb_btn(f"{'⭐ ' if c.is_pro() else ''}{kind_icon(c)} {c.title or c.chat_id}"[:60],
                                f"a:chat:{c.chat_id}")])
        return lines, rows

    async def panel(user: User, name: str, arg: str = "") -> tuple[str, InlineKeyboardMarkup]:
        lang = user_lang(app, user)
        if name == "pro":
            chats = app.db.pro_chats()
            lines, rows = chat_rows(chats, lang)
            text = "\n".join([t(lang, "adm_pro_title", n=len(chats)), ""] + (lines or [t(lang, "adm_none")]))
            return text, kb(*rows, panel_back(lang))
        if name == "chats":
            page = int(arg or 0)
            chats, total = app.db.active_chats(page * 20, 20)
            lines, rows = chat_rows(chats, lang)
            text = "\n".join([t(lang, "adm_chats_title", n=total, page=page + 1), ""]
                             + (lines or [t(lang, "adm_none")]) + ["", t(lang, "adm_chat_hint")])
            nav = ([cb_btn("‹", f"a:chats:{page - 1}")] if page else []) + \
                  ([cb_btn("›", f"a:chats:{page + 1}")] if (page + 1) * 20 < total else [])
            return text, kb(*rows, nav, panel_back(lang))
        if name == "chat":
            c = app.db.get_chat(int(arg))
            if c is None:
                return t(lang, "adm_none"), kb(panel_back(lang))
            token = app.db.get_token(c.token) if c.token else None
            posted = app.db.chat_posted(c.chat_id)
            added = (f'<a href="tg://user?id={c.added_by}">{c.added_by}</a>' if c.added_by
                     else t(lang, "adm_unknown"))
            text = t(lang, "adm_chat", kind=kind_icon(c), title=chat_title(c), chat_id=c.chat_id,
                     added_by=added, token=escape(token.symbol) if token else t(lang, "no_token"),
                     lang=i18n.LANG_NAMES[i18n.norm(c.lang)], sells=t(lang, "on" if c.sells else "off"),
                     chart=t(lang, "on" if c.chart else "off"), d=posted["today"], w=posted["week"],
                     a=posted["all"],
                     pro=t(lang, "adm_pro_on", date=until(c.pro_until, lang)) if c.is_pro()
                     else t(lang, "adm_pro_off"))
            cid = c.chat_id
            return text, kb([cb_btn(t(lang, "adm_btn_days", n=n), f"a:grant:{cid}:{n}") for n in (7, 30, 365)],
                            [cb_btn(t(lang, "adm_btn_forever"), f"a:grant:{cid}:0"),
                             cb_btn(t(lang, "adm_btn_revoke"), f"a:revoke:{cid}")],
                            [cb_btn(t(lang, "adm_btn_pro"), "a:pro"), cb_btn(t(lang, "adm_btn_chats"), "a:chats:0")],
                            panel_back(lang))
        if name == "ads":
            lines = [t(lang, "adm_ads_title"), ""]
            rows = []
            for status in ("pending", "approved", "paid"):
                for ad in app.db.ads_by_status(status):
                    when = f" {_date(ad.starts_at)} → {_date(ad.ends_at)}" if ad.starts_at else ""
                    lines.append(f"#{ad.id} {status} · {ad.days} d · {ad.price} ⭐{when}\n{escape(ad.text)}")
                    if status == "pending":
                        rows.append([cb_btn(f"✅ #{ad.id}", f"ad:ok:{ad.id}"), cb_btn(f"❌ #{ad.id}", f"ad:no:{ad.id}")])
            if len(lines) == 2:
                lines.append(t(lang, "adm_none"))
            return "\n".join(lines), kb(*rows, panel_back(lang))
        if name == "pay":
            rows = app.db.recent_payments()
            lines = [t(lang, "adm_pay_title", total=app.db.stats()["stars_total"]), ""]
            for r in rows:
                note = f" · {t(lang, 'adm_refunded')}" if r["refunded"] else ""
                lines.append(f"{_date(r['created_at'])} · <b>{r['amount']} ⭐</b> · {r['user_id']} · "
                             f"<code>{escape(r['payload'])}</code>{note}")
            if not rows:
                lines.append(t(lang, "adm_none"))
            return "\n".join(lines), kb(panel_back(lang))
        # the panel's home: a summary of the bot
        s = app.db.stats()
        w = app.watcher
        if w is None:
            watcher = ""
        else:
            age = time.time() - w.last_ok
            last = app.db.last_block()
            lag = (w.head - last) if w.head and last else "?"
            watcher = (t(lang, "adm_watch_ok", s=int(age), lag=lag) if age < 120 else
                       t(lang, "adm_watch_bad", m=int(age // 60), err=escape(w.last_error or "-")))
        text = t(lang, "adm_title") + "\n\n" + t(
            lang, "adm_summary", active=s["chats_active"], tracking=s["chats_tracking"], pro=s["chats_pro"],
            tokens=s["tokens_tracked"], outbox=s["outbox"], stars=s["stars_total"], ads=s["ads_pending"],
            users=sum(app.db.users_by_lang().values()), watcher=watcher)
        text += "\n" + t(lang, "adm_health", mb=process_mb(), up=round((time.time() - app.started) / 3600, 1),
                          backup=last_backup(app) or "—")
        return text, kb([cb_btn(t(lang, "adm_btn_pro"), "a:pro"), cb_btn(t(lang, "adm_btn_chats"), "a:chats:0")],
                        [cb_btn(t(lang, "adm_btn_ads"), "a:ads"), cb_btn(t(lang, "adm_btn_pay"), "a:pay")],
                        [cb_btn(t(lang, "btn_refresh"), "a:home"), cb_btn(t(lang, "back"), "m:home")])

    @router.message(Command("admin", "panel"), private, owner_only)
    async def admin(message: Message, bot: Bot):
        text, markup = await panel(message.from_user, "home")
        await send_screen(bot, message.chat.id, user_lang(app, message.from_user), "stats", text, markup)

    @router.callback_query(F.data.startswith("a:"))
    async def admin_button(cb: CallbackQuery, bot: Bot):
        lang = user_lang(app, cb.from_user)
        if not is_owner(app, cb.from_user.id):
            await cb.answer(t(lang, "adm_not_owner"), show_alert=True)
            return
        parts = cb.data.split(":")
        name, arg = parts[1], parts[2] if len(parts) > 2 else ""
        note = None
        if name == "grant":
            cid, days = int(parts[2]), int(parts[3])
            chat = app.db.get_chat(cid)
            if chat is not None:
                ends = FOREVER if days == 0 else max(chat.pro_until, int(time.time())) + days * 86400
                app.db.set_pro_until(cid, ends)
                note = t(lang, "adm_granted", date=until(ends, lang))
            name, arg = "chat", str(cid)
        elif name == "revoke":
            app.db.set_pro_until(int(arg), int(time.time()))
            note = t(lang, "adm_revoked")
            name = "chat"
        await cb.answer(note or None)
        text, markup = await panel(cb.from_user, name, arg)
        await edit_screen(cb, lang, "stats", text, markup)

    @router.message(Command("botstats"), private, owner_only)
    async def botstats(message: Message):
        s = app.db.stats()
        w, snd, rpc = app.watcher, app.sender, app.rpc
        last = app.db.last_block()
        lag = (w.head - last) if w and w.head and last else None
        eth = app.eth_usd.current
        lines = [f"<b>Bot stats</b> (up {int((time.time() - app.started) / 3600)} h)"]
        lines += [f"{k}: {v}" for k, v in s.items()]
        langs = app.db.users_by_lang()
        lines.append("users by language: " + (", ".join(f"{k} {v}" for k, v in sorted(langs.items())) or "none"))
        if w:
            lines.append(f"block: {last} · head {w.head} · lag {lag} · skipped {w.skipped_blocks}")
            lines.append(f"last tick ok {int(time.time() - w.last_ok)} s ago"
                         + (f" · error: {escape(w.last_error)}" if w.last_error else ""))
        if snd:
            lines.append(f"sent: {snd.sent} · dropped: {snd.dropped}")
        lines.append(f"rpc calls: {getattr(rpc, 'calls', '?')} · errors: {getattr(rpc, 'errors', '?')}")
        lines.append(f"ETH: {render.fmt_usd(eth) if eth else 'unknown'}")
        await message.answer("\n".join(lines))

    @router.message(Command("grantpro"), private, owner_only)
    async def grantpro(message: Message, command: CommandObject):
        try:
            chat_id, days = (int(x) for x in (command.args or "").split())
        except ValueError:
            await message.answer("Usage: /grantpro <code>chat_id</code> <code>days</code>")
            return
        if not app.db.get_chat(chat_id):
            await message.answer("Unknown chat.")
            return
        ends = app.db.extend_pro(chat_id, days * 86400)
        await message.answer(f"Pro for {chat_id} until {_date(ends)}.")

    @router.message(Command("refund"), private, owner_only)
    async def refund(message: Message, bot: Bot, command: CommandObject):
        charge = (command.args or "").strip()
        row = app.db.get_payment(charge) if charge else None
        if not row or row["refunded"]:
            await message.answer("Usage: /refund <code>charge_id</code> (unknown or already refunded)")
            return
        try:
            await bot.refund_star_payment(row["user_id"], charge)
        except TelegramAPIError as e:
            await message.answer(f"Refund failed: {escape(str(e))}")
            return
        app.db.mark_refunded(charge)
        kind, _, rest = row["payload"].partition(":")
        if kind == "pro":
            chat_id, weeks = (int(x) for x in rest.split(":"))
            app.db.extend_pro(chat_id, -weeks * 7 * 86400)
        elif kind == "ad":
            app.db.set_ad_status(int(rest), "rejected")
        await message.answer(f"Refunded {row['amount']} ⭐ to {row['user_id']}.")

    @router.message(Command("ads"), private, owner_only)
    async def ads(message: Message):
        lines = ["<b>Ads</b>"]
        for status in ("pending", "approved", "paid"):
            for ad in app.db.ads_by_status(status):
                when = f" {_date(ad.starts_at)} → {_date(ad.ends_at)}" if ad.starts_at else ""
                lines.append(f"#{ad.id} {status} {ad.days} d {ad.price} ⭐{when}: {escape(ad.text)}")
        await message.answer("\n".join(lines) if len(lines) > 1 else "No ads.")

    return router
