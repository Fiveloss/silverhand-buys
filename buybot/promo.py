"""/promo <token>: a short video of a vibe/vibe launch that its creator can post on X.

gather() reads the facts (the chain through the bot's own code, plus vibe/vibe's public read
API for holders, buys and the logo) into a spec for promo_video.render(). Promos keeps the
queue, the cache and the rate limits. docs/PROMO-PLAN.md has the reasons.

Everything from the launch is untrusted: its name, ticker and logo are set by whoever made
it. Texts lose control, format and bidi characters and anything the video's font cannot
draw (emoji, RTL scripts: this Pillow has no text shaping), and are clipped. The logo is
fetched from vibe/vibe's own media path only (built from a checked IPFS id), at most
LOGO_MAX bytes, only WebP, PNG or JPEG by their first bytes (never SVG), its size checked
before it is decoded, and only its first frame is used.
"""

import asyncio
import io
import json
import logging
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field

import aiohttp

from . import chain, heavy, pools, quotes, tokens, vibe

log = logging.getLogger(__name__)

API = "https://testnet.vibevibe.fun"
CHAIN_ID = 46630
HTTP_TIMEOUT = 10          # seconds, every request to vibe/vibe
JSON_MAX = 256 * 1024      # bytes: their answers here are 1-12 KB
LOGO_MAX = 512 * 1024      # bytes: their 410 px render is ~30 KB
LOGO_SIDE = 2048           # pixels: a bigger picture is not decoded
LOGO_KEEP = 512            # pixels: what the video keeps of it
LOGO_RENDERS = ("sq410", "thumb")  # vibe/vibe's own renders, biggest first
CID = re.compile(r"^ipfs://([a-z0-9]{8,200})$")  # the same check as their site's

FACTS_TTL = 120            # seconds the facts of a token are read once for
CACHE_TTL = 3600           # seconds a rendered video is sent again for the same facts
CACHE_MAX = 200
MAX_WAITING = 3            # promos waiting for their turn; more get "busy"
PICKER_TTL = 1800          # seconds a style picker's buttons work
# the video's looks (promo_styles.STYLES draws them), in the picker's order
STYLES = ("dashboard", "kinetic", "terminal", "ticket", "gauge", "lime")
STYLE_NAMES = {"dashboard": "Dashboard", "kinetic": "Kinetic", "terminal": "Terminal",
               "ticket": "Ticket", "gauge": "Gauge", "lime": "Lime"}

# (seconds, at most this many) per user and per chat
USER_LIMITS = ((60, 1), (3600, 6))
CHAT_LIMITS = ((60, 1), (3600, 10))


class PromoError(Exception):
    """Why there is no video: `key` names the answer (i18n)."""

    def __init__(self, key: str, **kw):
        super().__init__(key)
        self.key = key
        self.kw = kw


# ----------------------------------------------------------------------------- text
_DROP = {"Cc", "Cf", "Cs", "Co", "Cn"}  # controls, format (bidi, zero width), surrogates, private, unassigned
_glyphs: dict = {}


def _drawable(ch: str) -> bool:
    """Whether the video's font has a glyph for ch (not its "missing" box)."""
    if ch not in _glyphs:
        from PIL import Image, ImageDraw
        from .card import font
        f = font("ui", 40)

        def drawn(c):
            im = Image.new("L", (96, 64), 0)
            ImageDraw.Draw(im).text((8, 48), c, font=f, fill=255, anchor="ls")
            return im
        if "notdef" not in _glyphs:
            _glyphs["notdef"] = drawn("􏿽").tobytes()
        im = drawn(ch)
        _glyphs[ch] = im.getbbox() is not None and im.tobytes() != _glyphs["notdef"]
    return _glyphs[ch]


def clean(text: str | None, limit: int) -> str:
    """Untrusted text made safe to draw: NFKC, one line, no control/format/bidi
    characters, only what the font can draw, at most `limit` characters."""
    text = unicodedata.normalize("NFKC", text or "")[: limit * 4]
    out = []
    for ch in text:
        if ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch) not in _DROP and _drawable(ch):
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()[:limit].rstrip()


def clean_symbol(text: str | None, limit: int = 12) -> str:
    return clean(text, limit).replace(" ", "").lstrip("$")


# ----------------------------------------------------------------------------- vibe/vibe's API
def _headers(bot: str) -> dict:
    # a plain client's User-Agent: their Cloudflare challenges browser-like ones
    return {"User-Agent": f"SilverhandBuys (+https://t.me/{bot.lstrip('@')})" if bot else "SilverhandBuys",
            "Accept": "application/json"}


async def _read(r: aiohttp.ClientResponse, limit: int) -> bytes | None:
    if r.content_length is not None and r.content_length > limit:
        return None
    data = bytearray()
    async for chunk in r.content.iter_chunked(16384):
        data += chunk
        if len(data) > limit:
            return None
    return bytes(data)


async def api_get(session, path: str, bot: str = "") -> dict | None:
    """vibe/vibe's answer's `data`, or None (not found, an error, too big, not JSON)."""
    try:
        async with session.get(API + path, headers=_headers(bot), allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as r:
            if r.status != 200:
                return None
            body = await _read(r, JSON_MAX)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        log.info("vibe/vibe %s failed: %s", path, e)
        return None
    if body is None:
        return None
    try:
        data = json.loads(body).get("data")
    except (ValueError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


def _int(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 0 <= n < 10 ** 12 else None


def api_facts(curve_launch: dict | None, v6_launch: dict | None, v6_item: dict | None,
              candles: dict | None, token: str) -> dict:
    """{holders, buys_24h, image} from vibe/vibe's answers, each only when it is there and
    names this token. A curve-factory launch: /launches/<token> and its hourly candles; a
    V6 launch: /v6/launches/<token> and its item in /v6/launches?q=<token>."""
    out = {"holders": None, "buys_24h": None, "image": None}
    token = token.lower()
    if curve_launch and str(curve_launch.get("tokenAddress", "")).lower() == token:
        out["holders"] = _int(curve_launch.get("holderCount"))
        out["image"] = ((curve_launch.get("content") or {}).get("image") or {}).get("uri")
        items = (candles or {}).get("items")
        if isinstance(items, list):
            counts = [_int(c.get("buyCount")) for c in items if isinstance(c, dict)]
            if all(c is not None for c in counts):
                out["buys_24h"] = sum(counts)
        return out
    launch = (v6_launch or {}).get("launch")
    if isinstance(launch, dict) and str(launch.get("tokenAddress", "")).lower() == token:
        out["image"] = ((launch.get("content") or {}).get("image") or {}).get("uri")
    if v6_item and str(v6_item.get("tokenAddress", "")).lower() == token:
        market = v6_item.get("market") or {}
        out["holders"] = _int(market.get("holderCount"))
        out["buys_24h"] = _int(market.get("buyCount24h"))
    return out


async def read_api(session, token: str, bot: str = "") -> dict:
    """The API's facts for a launch of either factory (api_facts); empty when it is down."""
    base = f"/api/v1/chains/{CHAIN_ID}"
    curve_launch = await api_get(session, f"{base}/launches/{token}", bot)
    if curve_launch is not None:
        candles = await api_get(session, f"{base}/launches/{token}/candles?interval=1h", bot)
        return api_facts(curve_launch, None, None, candles, token)
    v6_launch = await api_get(session, f"{base}/v6/launches/{token}", bot)
    found = await api_get(session, f"{base}/v6/launches?limit=1&q={token}", bot)
    items = (found or {}).get("items")
    item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
    return api_facts(None, v6_launch, item, None, token)


# ----------------------------------------------------------------------------- the logo
def logo_urls(uri) -> list[str]:
    """vibe/vibe's renders of the launch image, from its ipfs:// id only."""
    m = CID.match(uri) if isinstance(uri, str) else None
    return [f"{API}/media/{m.group(1)}.{r}.webp" for r in LOGO_RENDERS] if m else []


def _raster(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def open_logo(data: bytes | None):
    """A checked RGBA logo at most LOGO_KEEP px, or None: WebP/PNG/JPEG by its first bytes,
    at most LOGO_MAX bytes and LOGO_SIDE px a side (read from the header, before decoding),
    the first frame only."""
    if not data or len(data) > LOGO_MAX:
        return None
    kind = _raster(data)
    if kind is None:
        return None
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data), formats=[kind])
        w, h = im.size
        if not (0 < w <= LOGO_SIDE and 0 < h <= LOGO_SIDE):
            return None
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        if kind == "JPEG":
            im.draft("RGB", (LOGO_KEEP, LOGO_KEEP))
        im.load()
        out = im.convert("RGBA")
        out.thumbnail((LOGO_KEEP, LOGO_KEEP), Image.LANCZOS)
        im.close()
        return out
    except Exception as e:  # Pillow raises many kinds on broken data
        log.info("logo unreadable: %s", e)
        return None


async def fetch_logo(session, uri, bot: str = ""):
    for url in logo_urls(uri):
        try:
            async with session.get(url, headers={"User-Agent": _headers(bot)["User-Agent"]}, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as r:
                if r.status != 200 or r.content_type not in ("image/webp", "image/png", "image/jpeg"):
                    continue
                data = await _read(r, LOGO_MAX)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.info("logo %s failed: %s", url, e)
            continue
        logo = open_logo(data)
        if logo is not None:
            return logo
    return None


# ----------------------------------------------------------------------------- the facts
async def gather(rpc, session, rates, address: str, *, token=None, bot: str = "",
                 network: str = "Robinhood Chain testnet", testnet: bool = True):
    """(spec for promo_video, logo or None) of a vibe/vibe launch. The chain gives the
    name, ticker, pair, curve and market cap; the API holders, buys and the logo (left
    out when it is down). Raises PromoError."""
    address = address.lower()
    if token is None:
        try:
            token, _ = await tokens.load_token(rpc, address, rates)
        except tokens.TokenError as e:
            raise PromoError("promo_not_vibe") from e
    if token.kind != "vibe":
        raise PromoError("promo_not_vibe")
    st = await vibe.state(rpc, token.curve)
    if st is None or st.token != address:
        raise PromoError("promo_failed")
    graduated = st.complete or token.graduated
    price = None
    if not graduated:
        price = st.price(token.decimals, token.quote_decimals)
    else:
        got = await pools.v4_state(rpc, token.pool_id)
        if got and got[0]:
            price = chain.v4_token_price(got[0], address, token.pair, token.decimals, token.quote_decimals)
    mc_quote = price * token.total_supply / 10 ** token.decimals if price else None
    rate = await rates.get(token.quote_kind)
    api = await read_api(session, address, bot) if session is not None else {}
    logo = await fetch_logo(session, api.get("image"), bot) if api.get("image") else None
    symbol = clean_symbol(token.symbol) or address[2:8].upper()
    by_tokens = st.virtual_tokens is not None  # a curve-factory curve (vibe.State.progress)
    spec = {
        "token": address, "symbol": symbol, "name": clean(token.name, 40) or symbol,
        "pair": clean_symbol(token.quote_symbol) or "?", "graduated": graduated,
        "progress": 1.0 if graduated else st.progress, "dex": "Uniswap v4",
        "target": st.target / 10 ** token.quote_decimals if not graduated else None,
        # what State.progress counts, as vibe/vibe's site does: tokens sold on a curve-factory
        # curve, the quote raised of the target on a V6 curve
        "sold": st.tokens_sold / 10 ** token.decimals if not graduated and by_tokens else None,
        "curve_tokens": st.supply / 10 ** token.decimals if not graduated and by_tokens else None,
        "raised": st.real_quote / 10 ** token.quote_decimals if not graduated and not by_tokens else None,
        "mc_quote": mc_quote, "mc_usd": mc_quote * rate if mc_quote is not None and rate else None,
        "mc_approx": token.quote_kind.startswith(quotes.APPROX_KINDS),
        "holders": api.get("holders"), "buys_24h": api.get("buys_24h"),
        "launchpad": "vibe/vibe", "network": network, "bot": bot, "testnet": testnet,
    }
    return spec, logo


def cache_key(spec: dict, logo, style: str = "dashboard") -> str:
    """The style and the facts as drawn, rounded as the video shows them: the same key, the
    same video."""
    def r3(x):
        return None if x is None else float(f"{x:.3g}")
    parts = [style, spec["token"], spec["symbol"], spec["name"], spec["pair"], spec["graduated"],
             round(spec["progress"] * 1000), r3(spec.get("mc_usd")), r3(spec.get("mc_quote")),
             spec.get("holders"), spec.get("buys_24h"), logo is not None]
    return "|".join(str(p) for p in parts)


# ----------------------------------------------------------------------------- limits, queue, cache
class Limiter:
    """Sliding windows: allow(key) -> 0 when allowed (and counted), else seconds to wait."""

    def __init__(self, rules):
        self.rules = rules
        self.hits: dict = {}

    def allow(self, key, now: float | None = None) -> float:
        now = time.time() if now is None else now
        span = max(s for s, _ in self.rules)
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] >= span:
            q.popleft()
        for seconds, most in self.rules:
            recent = [x for x in q if now - x < seconds]
            if len(recent) >= most:
                return max(1.0, recent[0] + seconds - now)
        q.append(now)
        if len(self.hits) > 5000:  # old keys of people long gone
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] >= span]:
                del self.hits[k]
        return 0.0


def _rss_mb() -> str:
    """The bot's peak resident memory (Linux: VmHWM), for the render log line."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return str(int(line.split()[1]) // 1024)
    except OSError:
        pass
    return "?"


@dataclass
class Promos:
    """One promo rendered at a time, after the buy cards' turn (heavy.slot); at most
    MAX_WAITING waiting; a video for the same facts is sent again from its file_id."""
    users: Limiter = field(default_factory=lambda: Limiter(USER_LIMITS))
    chats: Limiter = field(default_factory=lambda: Limiter(CHAT_LIMITS))
    facts: dict = field(default_factory=dict)    # token -> (time, spec, logo)
    videos: dict = field(default_factory=dict)   # cache_key -> (time, Telegram file_id)
    pickers: dict = field(default_factory=dict)  # (chat, message) -> (time, token, user id, lang)
    preview: str | None = None                   # the styles picture's Telegram file_id
    samples: dict = field(default_factory=dict)  # style -> its sample clip's Telegram file_id
    waiting: int = 0
    _lock: asyncio.Lock | None = None

    def cached_facts(self, token: str):
        hit = self.facts.get(token)
        return hit[1:] if hit and time.time() - hit[0] < FACTS_TTL else None

    def keep_facts(self, token: str, spec: dict, logo) -> None:
        now = time.time()
        for k in [k for k, v in self.facts.items() if now - v[0] >= FACTS_TTL]:
            del self.facts[k]
        self.facts[token] = (now, spec, logo)

    def cached_video(self, key: str) -> str | None:
        hit = self.videos.get(key)
        return hit[1] if hit and time.time() - hit[0] < CACHE_TTL else None

    def keep_video(self, key: str, file_id: str) -> None:
        now = time.time()
        for k in [k for k, v in self.videos.items() if now - v[0] >= CACHE_TTL]:
            del self.videos[k]
        while len(self.videos) >= CACHE_MAX:
            del self.videos[min(self.videos, key=lambda k: self.videos[k][0])]
        self.videos[key] = (now, file_id)

    def keep_picker(self, chat_id: int, message_id: int, token: str, user_id: int, lang: str) -> None:
        now = time.time()
        for k in [k for k, v in self.pickers.items() if now - v[0] >= PICKER_TTL]:
            del self.pickers[k]
        self.pickers[(chat_id, message_id)] = (now, token, user_id, lang)

    def picker(self, chat_id: int, message_id: int):
        """(token, user id, lang) of a style picker still open, else None."""
        hit = self.pickers.get((chat_id, message_id))
        return hit[1:] if hit and time.time() - hit[0] < PICKER_TTL else None

    def busy(self) -> bool:
        return self.waiting >= MAX_WAITING

    async def render(self, spec: dict, logo, style: str = "dashboard") -> bytes:
        """The MP4 in a style, rendered in a thread when its turn comes. Raises PromoError("promo_busy")
        when too many wait already."""
        if self.busy():
            raise PromoError("promo_busy")
        if self._lock is None:
            self._lock = asyncio.Lock()
        self.waiting += 1
        try:
            async with self._lock:           # one promo at a time, so buy cards never queue behind many
                async with heavy.slot():     # and never alongside a buy card
                    try:
                        from . import promo_video
                        t0, c0 = time.perf_counter(), time.process_time()
                        data = await asyncio.to_thread(promo_video.render, spec, logo, style=style)
                        log.info("promo $%s (%s) rendered: %.1f s, %.1f s of the bot's CPU (ffmpeg apart), %d KB, "
                                 "rss %s MB", spec.get("symbol"), style, time.perf_counter() - t0,
                                 time.process_time() - c0, len(data) // 1024, _rss_mb())
                        return data
                    finally:
                        heavy.release()
        finally:
            self.waiting -= 1


def caption(spec: dict, lang: str, now: float | None = None) -> str:
    """The video's caption (HTML): the facts drawn, when they were read, and a factual text
    to post with it, which a tap copies."""
    from html import escape

    from .i18n import t
    from .promo_video import compact, pct, usd
    facts, post = [], []
    if spec["graduated"]:
        facts.append(t(lang, "promo_graduated"))
        post.append("graduated to Uniswap v4")
    else:
        facts.append(t(lang, "promo_curve", pct=pct(spec["progress"])))
        post.append(f"{pct(spec['progress'])} of the bonding curve filled")
    if spec.get("mc_usd") is not None:
        facts.append(t(lang, "promo_mc", mc=usd(spec["mc_usd"], spec.get("mc_approx", False))))
    elif spec.get("mc_quote") is not None:
        facts.append(t(lang, "promo_mc", mc=f"{compact(spec['mc_quote'])} {escape(spec['pair'])}"))
    if spec.get("holders") is not None:
        facts.append(t(lang, "promo_holders", n=f"{spec['holders']:,}"))
        post.append(f"{spec['holders']:,} holders")
    if spec.get("buys_24h") is not None:
        facts.append(t(lang, "promo_buys", n=f"{spec['buys_24h']:,}"))
    when = time.strftime("%H:%M", time.gmtime(time.time() if now is None else now))
    text = t(lang, "promo_post", symbol=spec["symbol"], facts=", ".join(post), token=spec["token"])
    return t(lang, "promo_caption", symbol=escape(spec["symbol"]), facts="\n".join(facts), time=when,
             post=escape(text))


def wait_text(seconds: float, lang: str) -> str:
    s = max(1, round(seconds))
    if s < 90:
        return f"{s} s" if lang != "ru" else f"{s} с"
    m = round(s / 60)
    return f"{m} min" if lang != "ru" else f"{m} мин"
