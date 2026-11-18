"""Entry point: python -m buybot.main (run from the project directory)."""

import asyncio
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
                           BotCommandScopeChat, MenuButtonWebApp, WebAppInfo)

from . import __version__, chain, digest, health, holders, i18n, promo, webapp
from .bot import App, build_router, claim_for_owners, notify_owners
from .config import load_config
from .db import Db
from .prices import EthUsd
from .profile import sync_profile
from .quotes import QUOTES, Rates
from .rpc import Rpc
from .sender import Sender
from .watcher import HEAD_LAG, Watcher

log = logging.getLogger("buybot")

# (command, English, Russian): Telegram shows the Russian menu to Russian-language apps.
GROUP_COMMANDS = [
    ("track", "Post buys of a token: /track 0x…", "Публиковать покупки токена: /track 0x…"),
    ("test", "Preview a buy post", "Пример поста"),
    ("settings", "Current settings", "Текущие настройки"),
    ("stats", "Buys, sells and volume of 24 h", "Покупки, продажи и объём за 24 ч"),
    ("top", "The biggest buyers of 24 h", "Крупнейшие покупатели за 24 ч"),
    ("scan", "Holders, snipers, dev, ticker: /scan 0x…", "Держатели, снайперы, дев, тикер: /scan 0x…"),
    ("promo", "A 7 s video of the token for X", "7-секундное видео токена для X"),
    ("sells", "Post sells too: on/off", "Публиковать и продажи: on/off"),
    ("digest", "Daily digest at 18:00 UTC: on/off", "Ежедневная сводка в 18:00 UTC: on/off"),
    ("alerts", "A private alert on a 30% drop: on/off", "Личный алерт о падении на 30%: on/off"),
    ("chart", "Price chart on posts: on/off", "График в постах: on/off"),
    ("lang", "Language of posts: en/ru", "Язык постов: en/ru"),
    ("minbuy", "Skip buys under $N", "Не публиковать покупки меньше $N"),
    ("emoji", "The mark of a buy", "Значок покупки"),
    ("whale", "The 🐋 mark from $N", "Метка 🐋 от $N"),
    ("media", "Your GIF/photo/video on buys", "Своя гифка/фото/видео в постах"),
    ("untrack", "Stop posting", "Перестать публиковать"),
    ("help", "All commands", "Все команды"),
]
PRIVATE_COMMANDS = [
    ("start", "Menu", "Меню"),
    ("chats", "My groups and channels", "Мои группы и каналы"),
    ("stats", "Stats of the current chat", "Статистика текущего чата"),
    ("scan", "Check a token: /scan 0x…", "Проверить токен: /scan 0x…"),
    ("promo", "A 7 s video of a vibe/vibe token for X: /promo 0x…", "Видео токена vibe/vibe для X: /promo 0x…"),
    ("lang", "Language / Язык", "Язык / Language"),
    ("help", "Commands", "Команды"),
    ("terms", "Terms", "Условия"),
    ("support", "Questions and problems", "Вопросы и проблемы"),
]


OWNER_COMMANDS = [("admin", "Control panel", "Панель управления")]


async def set_commands(bot: Bot, owner_ids=(), langs=("en", "ru"), chain_id=chain.CHAIN_ID) -> None:
    scopes = [(BotCommandScopeAllGroupChats(), GROUP_COMMANDS),
              (BotCommandScopeAllPrivateChats(), PRIVATE_COMMANDS)]
    # the owners' private chats also list the control panel; nobody else sees it
    scopes += [(BotCommandScopeChat(chat_id=uid), OWNER_COMMANDS + PRIVATE_COMMANDS) for uid in owner_ids]
    for scope, commands in scopes:
        if chain_id != promo.CHAIN_ID:
            commands = [c for c in commands if c[0] != "promo"]
        if len(langs) == 1:
            commands = [c for c in commands if c[0] != "lang"]
        await bot.set_my_commands([BotCommand(command=c, description=en) for c, en, _ in commands],
                                  scope=scope)
        if "ru" in langs:
            await bot.set_my_commands([BotCommand(command=c, description=ru) for c, _, ru in commands],
                                      scope=scope, language_code="ru")
        else:
            await bot.delete_my_commands(scope=scope, language_code="ru")


async def backups(app: App, folder: str) -> None:
    """A copy of the database once a day, the last seven kept (data/backups)."""
    while True:
        try:
            made = app.db.backup(folder)
            if made:
                log.info("database backed up to %s", made)
        except Exception as e:
            log.warning("backup failed: %s", e)
        await asyncio.sleep(3600)


async def watchdog(bot: Bot, app: App, stale_after: float = 300, tasks=()) -> None:
    """Notify only on changed health problems or recovery, not on every poll."""
    previous = set()
    while True:
        await asyncio.sleep(60)
        problems = health.issues(app, stale_after=stale_after, tasks=tasks)
        if problems == previous:
            continue
        text = (i18n.t("en", "health_warning", issues="; ".join(
            i18n.t("en", "health_issue_" + p) for p in sorted(problems)))
            if problems else i18n.t("en", "health_recovered"))
        if await notify_owners(bot, app, text):
            previous = problems  # failed notices are retried next time


async def main() -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    cfg = load_config(root / ".env")
    i18n.LANGS = tuple(x for x in cfg.langs if x in i18n.LANGS) or ("en",)
    logging.basicConfig(level=cfg.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)

    async with aiohttp.ClientSession(trust_env=True) as session:
        rpc = Rpc(cfg.rpc_url, session, min_interval=cfg.rpc_min_interval)
        actual_chain = await rpc.chain_id()
        if actual_chain != cfg.chain_id:
            db.close()
            raise SystemExit(f"RPC is chain {actual_chain}, configured CHAIN_ID is {cfg.chain_id}")
        try:
            db.bind_chain(actual_chain, cfg.db_chain_id)
        except ValueError as e:
            db.close()
            raise SystemExit(str(e)) from e
        eth_usd = EthUsd(session)
        rates = Rates(rpc, eth_usd)
        repriced = db.reprice_quotes(QUOTES)
        if repriced:
            log.info("%d tokens and pools now have a dollar rate", repriced)
        bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                              link_preview_is_disabled=True))
        me = await bot.get_me()
        log.info("starting %s as @%s on chain %s", __version__, me.username, actual_chain)

        app = App(cfg=cfg, db=db, rpc=rpc, eth_usd=eth_usd, username=me.username, rates=rates)
        app.watcher = Watcher(rpc, db, eth_usd, rates=rates, poll_seconds=cfg.poll_seconds, head_lag=HEAD_LAG)
        app.sender = Sender(bot, db, trade_url=cfg.trade_url, explorer=cfg.explorer_url)

        dp = Dispatcher()
        dp.include_router(build_router(app))
        try:
            await set_commands(bot, cfg.owner_ids, i18n.LANGS, actual_chain)
        except Exception as e:
            log.warning("set_my_commands failed: %s", e)
        try:
            await claim_for_owners(bot, app)
        except Exception as e:
            log.warning("claiming chats for the owners failed: %s", e)
        try:
            changed = await sync_profile(bot, i18n.LANGS)
            if changed:
                log.info("profile updated: %s", ", ".join(changed))
        except Exception as e:
            log.warning("profile sync failed: %s", e)
        web = None
        if cfg.webapp_url and cfg.webapp_port:  # the Mini App (webapp.py), and the menu button to it
            try:
                web = await webapp.start(app, bot, cfg.webapp_port)
                await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                    text="Open", web_app=WebAppInfo(url=cfg.webapp_url + "/")))
            except Exception as e:
                log.warning("mini app not started: %s", e)

        tasks = [asyncio.create_task(app.watcher.run(), name="watcher"),
                 asyncio.create_task(app.watcher.backfill_missing(), name="backfill"),
                 asyncio.create_task(app.watcher.pool_refresher(), name="pools"),
                 asyncio.create_task(app.sender.run(), name="sender"),
                 asyncio.create_task(digest.run(app.db, app.rates), name="digest"),
                 asyncio.create_task(backups(app, str(Path(cfg.db_path).parent / "backups")),
                                     name="backups")]
        if actual_chain == promo.CHAIN_ID:
            tasks.append(asyncio.create_task(holders.run(app.db, app.rates, session, me.username), name="holders"))
        tasks.append(asyncio.create_task(watchdog(bot, app, tasks=tasks), name="watchdog"))
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            tasks += list(app.tasks)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if web is not None:
                await web.cleanup()
            await bot.session.close()
            db.close()


if __name__ == "__main__":
    asyncio.run(main())
