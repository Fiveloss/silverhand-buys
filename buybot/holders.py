"""Holder milestones: a vibe/vibe token passing 50, 100, 250 … 5000 holders gets one post
in every chat that tracks it, on the milestone card with its own pill and 𝕏 Share.

The count is vibe/vibe's own (holderCount of their API, what the token's page shows), read
every HOLDERS_EVERY for each tracked vibe/vibe token: one request for a curve-factory
launch, two for a V6 one. Like the market cap and curve levels (watcher._milestones), only
the highest level reached is kept per token (meta "level:holders:<token>"): several
levels passed between two reads post the highest one, and a level is never posted twice,
even when holders leave and come back. The first read of a token only sets where it
stands, so levels passed before tracking are not posted.
"""

import asyncio
import logging
import time

from . import chain, promo
from .quotes import APPROX_KINDS

log = logging.getLogger(__name__)

HOLDER_LEVELS = [50, 100, 250, 500, 1000, 2500, 5000]
HOLDERS_EVERY = 300   # seconds between two reads of the counts


def level_reached(n: int, levels: list[int] = HOLDER_LEVELS) -> int | None:
    """The highest level at or below n."""
    below = [x for x in levels if x <= n]
    return max(below) if below else None


async def holder_count(session, token: str, bot: str = "") -> int | None:
    """vibe/vibe's holder count of a token (either factory), or None when unknown."""
    base = f"/api/v1/chains/{promo.CHAIN_ID}"
    launch = await promo.api_get(session, f"{base}/launches/{token}", bot)
    if launch is not None:
        return promo.api_facts(launch, None, None, None, token)["holders"]
    found = await promo.api_get(session, f"{base}/v6/launches?limit=1&q={token}", bot)
    items = (found or {}).get("items")
    item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
    return promo.api_facts(None, None, item, None, token)["holders"]


def payload(db, rates, token, level: int, holders: int) -> dict:
    """The post's payload: the level, and where the token trades for the card."""
    points = db.trades_since(token.address, int(time.time() - 86400))
    supply = token.total_supply / 10 ** token.decimals
    rate = rates.current(token.quote_kind)
    mc = max(points)[1] * supply if points else None   # the last trade's price
    p = {"token": token.address, "symbol": token.symbol, "what": "holders", "level": level,
         "holders": holders, "quote_symbol": token.quote_symbol, "launchpad": "vibe/vibe",
         "network": chain.network(promo.CHAIN_ID), "mc_quote": mc,
         "mc_usd": mc * rate if mc is not None and rate else None,
         "usd_approx": token.quote_kind.startswith(APPROX_KINDS)}
    if token.graduated:
        p.update(venue="v4", dex="Uniswap v4")
    return p


def check(db, rates, token, holders: int | None) -> int | None:
    """Queue the post when the token reached a new level; returns that level, else None."""
    if holders is None:
        return None
    key = f"level:holders:{token.address}"
    reached = level_reached(holders)
    seen = db.get_meta(key)
    if seen is None:
        db.set_meta(key, str(reached or 0))
        return None
    if reached is None or reached <= int(float(seen)):
        return None
    p = payload(db, rates, token, reached, holders)
    rows = [(chat.chat_id, "milestone", f"level:holders:{token.address}:{reached}", p)
            for chat in db.chats_for_token(token.address)]
    db.queue_events(rows, {key: str(reached)})
    return reached


async def queue(db, rates, session, bot: str = "") -> int:
    """Read the counts of the tracked vibe/vibe tokens and queue what is due."""
    n = 0
    for token in db.tracked_tokens():
        if token.kind != "vibe":
            continue
        try:
            got = check(db, rates, token, await holder_count(session, token.address, bot))
        except Exception as e:
            log.info("holders of %s unknown: %s", token.symbol, e)
            continue
        if got:
            log.info("%s passed %d holders", token.symbol, got)
            n += 1
    return n


async def run(db, rates, session, bot: str = "", every: float = HOLDERS_EVERY) -> None:
    while True:
        try:
            await queue(db, rates, session, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("holder milestones failed")
        await asyncio.sleep(every)
