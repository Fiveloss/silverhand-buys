"""Async JSON-RPC client that is polite to the free public endpoint.

- requests are spaced at least `min_interval` seconds apart;
- HTTP 429/5xx, network errors and "rate limit" JSON errors back off
  exponentially (and honour Retry-After);
- eth_getLogs splits its block range in half when the node says the result is
  too big or the query timed out (the public RPC caps a response at 10,000 logs).
"""

import asyncio
import logging
import time

import aiohttp

log = logging.getLogger(__name__)

_TOO_BIG_HINTS = ("more than", "too many", "limit", "range", "timed out", "timeout",
                  "exceed", "response size", "10000")
_RATE_HINTS = ("rate", "too many requests", "capacity", "throttl")


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message


class Rpc:
    def __init__(self, url: str, session: aiohttp.ClientSession, min_interval: float = 0.3,
                 timeout: float = 20.0, max_retries: int = 6):
        self.url = url
        self.session = session
        self.min_interval = min_interval
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self._id = 0
        self.calls = 0
        self.errors = 0

    async def _slot(self) -> None:
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._next_at)
            self._next_at = at + self.min_interval
        if at > now:
            await asyncio.sleep(at - now)

    def _push_back(self, seconds: float) -> None:
        self._next_at = max(self._next_at, time.monotonic() + seconds)

    async def call(self, method: str, params: list):
        delay = 1.0
        last_problem = "no attempt made"
        for _ in range(self.max_retries):
            await self._slot()
            self._id += 1
            self.calls += 1
            payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
            try:
                async with self.session.post(self.url, json=payload, timeout=self.timeout) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        retry_after = resp.headers.get("Retry-After", "")
                        wait = float(retry_after) if retry_after.isdigit() else delay
                        last_problem = f"HTTP {resp.status}"
                        self.errors += 1
                        self._push_back(wait)
                        await asyncio.sleep(wait)
                        delay = min(delay * 2, 30)
                        continue
                    body = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                last_problem = f"{type(e).__name__}: {e}"
                self.errors += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if not isinstance(body, dict):
                last_problem = "non-object response"
                self.errors += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            err = body.get("error")
            if err:
                code = err.get("code", 0) if isinstance(err, dict) else 0
                msg = str(err.get("message", err) if isinstance(err, dict) else err)
                if code == 429 or any(h in msg.lower() for h in _RATE_HINTS):
                    last_problem = msg
                    self.errors += 1
                    self._push_back(delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise RpcError(code, msg)
            return body.get("result")
        raise RpcError(-1, f"{method} failed after {self.max_retries} tries ({last_problem})")

    async def block_number(self) -> int:
        return int(await self.call("eth_blockNumber", []), 16)

    async def block_time(self, block: int) -> int:
        r = await self.call("eth_getBlockByNumber", [hex(block), False])
        return int(r["timestamp"], 16)

    async def tx_sender(self, tx: str) -> str | None:
        """Who signed a transaction: the trader behind a router's swap."""
        r = await self.call("eth_getTransactionByHash", [tx])
        return r["from"].lower() if r and r.get("from") else None

    async def eth_call(self, to: str, data: str, block: str | int = "latest") -> str:
        tag = hex(block) if isinstance(block, int) else block
        return await self.call("eth_call", [{"to": to, "data": data}, tag])

    async def get_logs(self, from_block: int, to_block: int, address=None, topics=None) -> list[dict]:
        params: dict = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
        if address:
            params["address"] = address
        if topics:
            params["topics"] = topics
        try:
            return await self.call("eth_getLogs", [params]) or []
        except RpcError as e:
            if to_block > from_block and any(h in e.message.lower() for h in _TOO_BIG_HINTS):
                mid = (from_block + to_block) // 2
                log.info("getLogs %d-%d too big, splitting", from_block, to_block)
                left = await self.get_logs(from_block, mid, address, topics)
                return left + await self.get_logs(mid + 1, to_block, address, topics)
            raise
