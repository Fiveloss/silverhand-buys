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
                           BotCommandScopeChat)

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
    ("sells", "Post sells too: on/off", "Публиковать и продажи: on/off"),
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
    ("lang", "Language / Язык", "Язык / Language"),
    ("help", "Commands", "Команды"),
    ("terms", "Terms", "Условия"),
    ("support", "Questions and problems", "Вопросы и проблемы"),
]


OWNER_COMMANDS = [("admin", "Control panel", "Панель управления")]


async def set_commands(bot: Bot, owner_ids=()) -> None:
    scopes = [(BotCommandScopeAllGroupChats(), GROUP_COMMANDS),
              (BotCommandScopeAllPrivateChats(), PRIVATE_COMMANDS)]
    # the owners' private chats also list the control panel; nobody else sees it
    scopes += [(BotCommandScopeChat(chat_id=uid), OWNER_COMMANDS + PRIVATE_COMMANDS) for uid in owner_ids]
    for scope, commands in scopes:
        await bot.set_my_commands([BotCommand(command=c, description=en) for c, en, _ in commands],
                                  scope=scope)
        await bot.set_my_commands([BotCommand(command=c, description=ru) for c, _, ru in commands],
                                  scope=scope, language_code="ru")


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


async def watchdog(bot: Bot, app: App, stale_after: float = 300) -> None:
    """Tell the owner when the chain watcher stops working, and when it recovers."""
    alerted = False
    while True:
        await asyncio.sleep(60)
        stale = time.time() - app.watcher.last_ok > stale_after
        if stale and not alerted:
            alerted = True
            await notify_owners(bot, app, "⚠️ No new blocks read for 5 minutes. "
                                          f"Last error: {app.watcher.last_error or 'none'}")
        elif not stale and alerted:
            alerted = False
            await notify_owners(bot, app, "✅ The chain watcher is working again.")


async def main() -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    cfg = load_config(root / ".env")
    logging.basicConfig(level=cfg.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)

    async with aiohttp.ClientSession(trust_env=True) as session:
        rpc = Rpc(cfg.rpc_url, session, min_interval=cfg.rpc_min_interval)
        eth_usd = EthUsd(session)
        rates = Rates(rpc, eth_usd)
        repriced = db.reprice_quotes(QUOTES)
        if repriced:
            log.info("%d tokens and pools now have a dollar rate", repriced)
        bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                              link_preview_is_disabled=True))
        me = await bot.get_me()
        log.info("starting as @%s", me.username)

        app = App(cfg=cfg, db=db, rpc=rpc, eth_usd=eth_usd, username=me.username, rates=rates)
        app.watcher = Watcher(rpc, db, eth_usd, rates=rates, poll_seconds=cfg.poll_seconds, head_lag=HEAD_LAG)
        app.sender = Sender(bot, db, trade_url=cfg.trade_url, explorer=cfg.explorer_url)

        dp = Dispatcher()
        dp.include_router(build_router(app))
        try:
            await set_commands(bot, cfg.owner_ids)
        except Exception as e:
            log.warning("set_my_commands failed: %s", e)
        try:
            await claim_for_owners(bot, app)
        except Exception as e:
            log.warning("claiming chats for the owners failed: %s", e)
        try:
            changed = await sync_profile(bot)
            if changed:
                log.info("profile updated: %s", ", ".join(changed))
        except Exception as e:
            log.warning("profile sync failed: %s", e)

        tasks = [asyncio.create_task(app.watcher.run(), name="watcher"),
                 asyncio.create_task(app.watcher.backfill_missing(), name="backfill"),
                 asyncio.create_task(app.watcher.pool_refresher(), name="pools"),
                 asyncio.create_task(app.sender.run(), name="sender"),
                 asyncio.create_task(watchdog(bot, app), name="watchdog"),
                 asyncio.create_task(backups(app, str(Path(cfg.db_path).parent / "backups")),
                                     name="backups")]
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await bot.session.close()
            db.close()


if __name__ == "__main__":
    asyncio.run(main())
