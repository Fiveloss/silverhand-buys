"""Async JSON-RPC client that is polite to the free public endpoint.

- requests are spaced at least `min_interval` seconds apart, and the reads of one watcher
  tick go out as one JSON-RPC batch (one HTTP request);
- HTTP 429/5xx, network errors and "rate limit" JSON errors back off (and honour
  Retry-After);
- the public RPC sits behind Cloudflare, which answers an IP that asks too much with a
  "Just a moment..." page (HTTP 403): then every request pauses for CHALLENGE_PAUSE and the
  call fails at once, instead of retrying into a longer block;
- eth_getLogs asks in pieces when the node names its block limit, and splits its range in
  half when the node says the result is too big (it caps a response at 10,000 logs).
"""

import asyncio
import json
import logging
import re
import time

import aiohttp

log = logging.getLogger(__name__)

_TOO_BIG_HINTS = ("more than", "too many", "limit", "range", "timed out", "timeout",
                  "exceed", "response size", "10000")
_RATE_HINTS = ("rate", "too many requests", "capacity", "throttl")
CHALLENGE_PAUSE = 60.0


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message


def _challenged(status: int, text: str) -> bool:
    return status == 403 and ("Just a moment" in text or "cloudflare" in text.lower())


class Rpc:
    def __init__(self, url: str, session: aiohttp.ClientSession, min_interval: float = 0.3,
                 timeout: float = 12.0, max_retries: int = 6):
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
        self.challenges = 0

    async def _slot(self) -> None:
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._next_at)
            self._next_at = at + self.min_interval
        if at > now:
            await asyncio.sleep(at - now)

    def _push_back(self, seconds: float) -> None:
        self._next_at = max(self._next_at, time.monotonic() + seconds)

    async def _send(self, what: str, payload):
        """POST one JSON-RPC request or batch; the parsed answer. Retries transport trouble
        with short waits: the public RPC's failures are brief, and every second spent here
        holds up the posts."""
        delay = 0.5
        last_problem = "no attempt made"
        for _ in range(self.max_retries):
            await self._slot()
            self.calls += 1
            try:
                async with self.session.post(self.url, json=payload, timeout=self.timeout) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        retry_after = resp.headers.get("Retry-After", "")
                        wait = float(retry_after) if retry_after.isdigit() else delay
                        last_problem = f"HTTP {resp.status}"
                        self.errors += 1
                        self._push_back(wait)
                        await asyncio.sleep(wait)
                        delay = min(delay * 2, 8)
                        continue
                    text = await resp.text()
                    status = resp.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_problem = f"{type(e).__name__}: {e}"
                self.errors += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            if _challenged(status, text):
                self.errors += 1
                self.challenges += 1
                self._push_back(CHALLENGE_PAUSE)
                log.warning("RPC answered with a Cloudflare challenge: pausing %.0f s", CHALLENGE_PAUSE)
                raise RpcError(-1, f"{what}: Cloudflare challenge (HTTP 403), paused {CHALLENGE_PAUSE:.0f} s")
            try:
                return json.loads(text)
            except ValueError:
                last_problem = f"HTTP {status}, not JSON: {text[:80]!r}"
                log.info("%s: %s", what, last_problem)
                self.errors += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8)
        raise RpcError(-1, f"{what} failed after {self.max_retries} tries ({last_problem})")

    @staticmethod
    def _rate_limited(err) -> bool:
        code = err.get("code", 0) if isinstance(err, dict) else 0
        msg = str(err.get("message", err) if isinstance(err, dict) else err)
        return code == 429 or any(h in msg.lower() for h in _RATE_HINTS)

    async def call(self, method: str, params: list):
        delay = 0.5
        for _ in range(self.max_retries):
            self._id += 1
            body = await self._send(method, {"jsonrpc": "2.0", "id": self._id, "method": method,
                                             "params": params})
            if not isinstance(body, dict):
                raise RpcError(-1, f"{method}: non-object response")
            err = body.get("error")
            if err:
                if self._rate_limited(err):
                    self.errors += 1
                    self._push_back(delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 8)
                    continue
                code = err.get("code", 0) if isinstance(err, dict) else 0
                raise RpcError(code, str(err.get("message", err) if isinstance(err, dict) else err))
            return body.get("result")
        raise RpcError(-1, f"{method} rate limited {self.max_retries} times")

    async def batch(self, calls: list[tuple[str, list]]) -> list:
        """Several calls in one HTTP request. Each item comes back as its result, or as an
        RpcError for that item alone (the others still count)."""
        if not calls:
            return []
        first = self._id + 1
        payload = [{"jsonrpc": "2.0", "id": first + i, "method": m, "params": p}
                   for i, (m, p) in enumerate(calls)]
        self._id += len(calls)
        body = await self._send(f"batch of {len(calls)}", payload)
        if not isinstance(body, list):
            raise RpcError(-1, f"batch of {len(calls)}: not a batch answer")
        by_id = {item.get("id"): item for item in body if isinstance(item, dict)}
        out = []
        for i, (method, _) in enumerate(calls):
            item = by_id.get(first + i)
            if item is None:
                out.append(RpcError(-1, f"{method}: missing from the batch answer"))
            elif item.get("error"):
                err = item["error"]
                code = err.get("code", 0) if isinstance(err, dict) else 0
                out.append(RpcError(code, str(err.get("message", err) if isinstance(err, dict) else err)))
            else:
                out.append(item.get("result"))
        return out

    async def get_logs_many(self, queries: list[tuple]) -> list[list[dict]]:
        """eth_getLogs for several (from, to, address, topics) at once, in one request; a query
        the batch could not answer (too wide, too big, rate limited) is asked on its own."""
        if not queries:
            return []
        calls = []
        for frm, to, address, topics in queries:
            params: dict = {"fromBlock": hex(frm), "toBlock": hex(to)}
            if address:
                params["address"] = address
            if topics:
                params["topics"] = topics
            calls.append(("eth_getLogs", [params]))
        results = await self.batch(calls)
        out = []
        for q, r in zip(queries, results):
            out.append(await self.get_logs(*q) if isinstance(r, RpcError) else (r or []))
        return out

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

    # The public RPC takes at most this many blocks in one eth_getLogs (it said so in Oct 2026);
    # a wider range is asked for in consecutive pieces instead of being halved on errors.
    MAX_LOG_SPAN = 10_000_000

    async def get_logs(self, from_block: int, to_block: int, address=None, topics=None) -> list[dict]:
        if to_block - from_block + 1 > self.MAX_LOG_SPAN:
            out = []
            for start in range(from_block, to_block + 1, self.MAX_LOG_SPAN):
                out += await self.get_logs(start, min(to_block, start + self.MAX_LOG_SPAN - 1), address, topics)
            return out
        params: dict = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
        if address:
            params["address"] = address
        if topics:
            params["topics"] = topics
        try:
            return await self.call("eth_getLogs", [params]) or []
        except RpcError as e:
            # "query spans N blocks …, but only M are allowed for this request": the limit
            # is lower when a topic position holds several values; ask in pieces of M
            allowed = re.search(r"only (\d+) are allowed", e.message)
            if allowed and to_block > from_block and int(allowed.group(1)) < to_block - from_block + 1:
                span = max(1, int(allowed.group(1)))
                log.info("getLogs %d-%d over the RPC's %d-block limit, in pieces", from_block, to_block, span)
                out = []
                for start in range(from_block, to_block + 1, span):
                    out += await self.get_logs(start, min(to_block, start + span - 1), address, topics)
                return out
            if to_block > from_block and any(h in e.message.lower() for h in _TOO_BIG_HINTS):
                mid = (from_block + to_block) // 2
                log.info("getLogs %d-%d too big, splitting", from_block, to_block)
                left = await self.get_logs(from_block, mid, address, topics)
                return left + await self.get_logs(mid + 1, to_block, address, topics)
            raise
