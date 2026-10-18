"""The Silverhand custom emoji: animated badges that stand in for the plain emoji of a post
and its buttons.

A bot may use custom emoji when its owner has Telegram Premium, in private chats, groups and
supergroups; in a channel it would need a username bought on Fragment, so channels keep the
plain emoji. The pack is drawn by tools/make_emoji.py into assets/emoji/*.webm and uploaded by
the owner's /emojipack command (the bot creates a sticker set of the custom emoji type); the ids
Telegram gives the emoji are kept in the meta table. Without them every post is plain.

Posts are written with plain emoji as always; brand() swaps the ones the pack draws for
<tg-emoji> tags, which keep the plain emoji inside for clients that cannot show the badge.
"""

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

DIR = Path(__file__).resolve().parents[1] / "assets" / "emoji"
META_KEY = "emoji_ids"
SET_TITLE = "Silverhand Buys"

# name -> the plain emoji it stands in for (and the emoji its sticker is listed under)
PACK = {
    "buy": "🟢", "sell": "🔴", "whale": "🐋", "dev": "💻", "cash": "💵", "sold": "💰",
    "coin": "🪙", "wallet": "👤", "mc": "📊", "curve": "🎓", "new": "🆕", "bag": "💼",
    "exit": "🚪", "dex": "💱", "chart": "📈", "tx": "🔍", "day": "🕐", "rocket": "🚀",
    "more": "➕",
}
# other spellings in the posts that one of the badges stands in for
ALIASES = {"👨‍💻": "dev"}
_BY_EMOJI = {v: k for k, v in PACK.items()} | ALIASES
_RE = re.compile("|".join(re.escape(e) for e in sorted(_BY_EMOJI, key=len, reverse=True)))


def set_name(bot_username: str) -> str:
    """Telegram wants a bot's sticker set to end in _by_<bot username>."""
    return f"silverhand_by_{bot_username}"


def allowed(chat) -> bool:
    """Custom emoji go to private chats, groups and supergroups, not channels (and not to a
    chat whose kind was never seen)."""
    return getattr(chat, "kind", None) in ("private", "group", "supergroup")


def brand(html: str, ids: dict[str, str]) -> str:
    """The post with every emoji the pack draws swapped for its badge."""
    if not ids:
        return html

    def swap(m: re.Match) -> str:
        cid = ids.get(_BY_EMOJI[m.group(0)])
        return f'<tg-emoji emoji-id="{cid}">{m.group(0)}</tg-emoji>' if cid else m.group(0)
    return _RE.sub(swap, html)


def split_icon(label: str, ids: dict[str, str] | None) -> tuple[str, str | None]:
    """A button label that starts with an emoji the pack draws: the label without it and the
    badge's id, which Telegram shows as the button's icon."""
    if ids:
        m = _RE.match(label)
        if m and ids.get(_BY_EMOJI[m.group(0)]):
            return label[m.end():].lstrip(), ids[_BY_EMOJI[m.group(0)]]
    return label, None


def load(db) -> dict[str, str]:
    raw = db.get_meta(META_KEY)
    try:
        ids = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return {k: str(v) for k, v in ids.items() if k in PACK and v}


async def upload(bot, user_id: int, bot_username: str, db) -> tuple[int, int]:
    """Create the pack's sticker set (owned by `user_id`, who must have started the bot), or
    add the badges it lacks, or replace the ones drawn again; then keep the ids Telegram gave
    them. Returns (badges in the set, badges uploaded now)."""
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import BufferedInputFile, InputSticker

    files = {name: DIR / f"{name}.webm" for name in PACK if (DIR / f"{name}.webm").exists()}
    if not files:
        raise FileNotFoundError(f"no emoji in {DIR}: run tools/make_emoji.py")
    name = set_name(bot_username)

    def sticker(n: str) -> InputSticker:
        return InputSticker(sticker=BufferedInputFile(files[n].read_bytes(), f"{n}.webm"),
                            format="video", emoji_list=[PACK[n]])

    try:
        current = await bot.get_sticker_set(name)
    except TelegramBadRequest:
        current = None
    sent = 0
    if current is None:
        names = list(files)
        await bot.create_new_sticker_set(user_id=user_id, name=name, title=SET_TITLE,
                                         stickers=[sticker(n) for n in names[:50]],
                                         sticker_type="custom_emoji")
        sent = min(50, len(names))
        for n in names[50:]:
            await bot.add_sticker_to_set(user_id=user_id, name=name, sticker=sticker(n))
            sent += 1
    else:
        have = {s.emoji: s for s in current.stickers}
        for n in files:
            old = have.get(PACK[n])
            if old is None:
                await bot.add_sticker_to_set(user_id=user_id, name=name, sticker=sticker(n))
            else:
                await bot.replace_sticker_in_set(user_id=user_id, name=name,
                                                 old_sticker=old.file_id, sticker=sticker(n))
            sent += 1
    final = await bot.get_sticker_set(name)
    ids = {_BY_EMOJI[s.emoji]: s.custom_emoji_id for s in final.stickers
           if s.emoji in _BY_EMOJI and s.custom_emoji_id}
    db.set_meta(META_KEY, json.dumps(ids))
    log.info("custom emoji set %s: %d badges", name, len(ids))
    return len(ids), sent
