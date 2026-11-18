"""The daily digest: once a day (DIGEST_HOUR UTC, evening in Europe) every chat that
tracks a token, with /digest on (the default), gets its token's last 24 hours in one post:
buys and sells with their volume, how many wallets bought, the market cap and its change,
and the three biggest buyers. A day without trades sends nothing.

It is queued in the outbox like any post (kind "digest"), so it survives a restart and
goes out in turn with the buys; meta "digest:<chat>" keeps the day it was queued for.
"""

import asyncio
import logging
import time

from .quotes import APPROX_KINDS
from .stats import token_day

log = logging.getLogger(__name__)

DIGEST_HOUR = 18   # UTC


def due_day(now: float) -> str | None:
    """The UTC day a digest is due for at `now`, or None before DIGEST_HOUR."""
    g = time.gmtime(now)
    return time.strftime("%Y-%m-%d", g) if g.tm_hour >= DIGEST_HOUR else None


async def build(db, rates, token, now: float) -> dict | None:
    """The digest's payload for a token, or None for a day without trades."""
    since = int(now - 86400)
    supply = token.total_supply / 10 ** token.decimals
    day = token_day(db.trades_since(token.address, since), supply, now)
    if day is None:
        return None
    counts = db.day_counts(token.address, since)
    top, _ = db.top_buyers(token.address, since, limit=3)
    return {
        "token": token.address, "symbol": token.symbol, "quote_symbol": token.quote_symbol,
        "rate": await rates.get(token.quote_kind), "usd_approx": token.quote_kind.startswith(APPROX_KINDS),
        "buys": day.buys, "sells": day.sells, "buy_volume": day.buy_volume, "sell_volume": day.sell_volume,
        "buyers": counts["buyers"], "mc_now": day.mc_now, "change": day.change,
        "top": [[wallet, quote] for wallet, quote, _ in top],
    }


async def queue(db, rates, now: float | None = None) -> int:
    """Queue today's digests that are due and not queued yet; returns how many."""
    now = time.time() if now is None else now
    day = due_day(now)
    if day is None:
        return 0
    n = 0
    for token in db.tracked_tokens():
        payload = None
        for chat in db.chats_for_token(token.address):
            key = f"digest:{chat.chat_id}"
            if not chat.digest or db.get_meta(key) == day:
                continue
            if payload is None:
                payload = await build(db, rates, token, now) or {}
            rows = [(chat.chat_id, "digest", f"digest:{day}", payload)] if payload else []
            n += db.queue_events(rows, {key: day})
    return n


async def run(db, rates, every: float = 600.0) -> None:
    """Check every ten minutes; a digest goes out within ten minutes of DIGEST_HOUR."""
    while True:
        try:
            n = await queue(db, rates)
            if n:
                log.info("queued %d daily digests", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("daily digest failed")
        await asyncio.sleep(every)
