"""One memory-heavy step at a time.

The bot lives under a 300 MB cap, and at rest it already holds ~180 MB (aiogram and
pydantic alone ~155 MB). An animated card costs ~25 MB in Python plus ~70 MB for its
ffmpeg, and two of them at once (a test post while the sender draws, two chats right
after /track) went over the cap. So:

- `async with heavy.slot():` one card being drawn (sender.make_chart). Another card
  waits its turn, a second or so, instead of running alongside.
- `async with heavy.backfill():` one history backfill at a time (Watcher.backfill):
  two /track right after each other, or the backfills after a restart, load in turn.
  It does not take the slot: a backfill window is only a megabyte or two.

After a card, `release()` hands the freed memory back to the system: glibc keeps freed
blocks of worker threads for reuse, and the bot's resident size crept up with every card.
"""

import asyncio
import ctypes
import ctypes.util
import gc
from contextlib import asynccontextmanager

_locks: dict = {}  # (loop, name) -> lock: made inside the running loop, one set per loop
_trim = None


def _lock(name: str, make):
    key = (asyncio.get_running_loop(), name)
    if key not in _locks:
        if len(_locks) > 8:  # loops of tests long gone
            _locks.clear()
        _locks[key] = make()
    return _locks[key]


def _libc_trim():
    global _trim
    if _trim is None:
        try:
            _trim = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6").malloc_trim
        except (OSError, AttributeError, TypeError):  # not glibc (Windows, musl, macOS)
            _trim = False
    return _trim


def release() -> None:
    """Collect cycles and return freed heap to the system (glibc only)."""
    gc.collect()
    trim = _libc_trim()
    if trim:
        trim(0)


@asynccontextmanager
async def slot():
    async with _lock("slot", lambda: asyncio.Semaphore(1)):
        yield


@asynccontextmanager
async def backfill():
    async with _lock("backfill", asyncio.Lock):
        yield
