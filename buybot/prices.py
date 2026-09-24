"""ETH/USD from free public tickers, with fallbacks and an honest "unknown".

The price is cached for `ttl` seconds. If every source fails, the last good
value is used for up to `max_age` seconds; after that `get()` returns None and
messages show amounts in ETH only instead of a stale dollar figure.
"""

import asyncio
import logging
import time

import aiohttp

log = logging.getLogger(__name__)

SOURCES = (
    ("coinbase", "https://api.coinbase.com/v2/prices/ETH-USD/spot",
     lambda j: float(j["data"]["amount"])),
    ("kraken", "https://api.kraken.com/0/public/Ticker?pair=ETHUSD",
     lambda j: float(next(iter(j["result"].values()))["c"][0])),
    ("coingecko", "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
     lambda j: float(j["ethereum"]["usd"])),
)


class EthUsd:
    def __init__(self, session: aiohttp.ClientSession, ttl: float = 60, max_age: float = 900):
        self.session = session
        self.ttl = ttl
        self.max_age = max_age
        self._value: float | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    @property
    def current(self) -> float | None:
        if self._value is not None and time.monotonic() - self._at < self.max_age:
            return self._value
        return None

    async def get(self) -> float | None:
        if self._value is not None and time.monotonic() - self._at < self.ttl:
            return self._value
        if self._lock.locked():
            return self.current
        async with self._lock:
            for name, url, parse in SOURCES:
                try:
                    async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                        if r.status != 200:
                            continue
                        value = parse(await r.json(content_type=None))
                except Exception as e:  # any source may change shape; try the next one
                    log.debug("price source %s failed: %s", name, e)
                    continue
                if 50 < value < 1_000_000:
                    self._value, self._at = value, time.monotonic()
                    return value
            log.warning("all ETH/USD sources failed")
            return self.current
