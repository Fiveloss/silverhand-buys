"""The Mini App: a page inside Telegram where a chat's admin sees the token's day and
week and changes the chat's settings, instead of commands.

The bot serves it itself, on 127.0.0.1:WEBAPP_PORT (a small aiohttp server in the same
process); an HTTPS tunnel publishes it at WEBAPP_URL, which the bot's menu button opens.
Without WEBAPP_URL and WEBAPP_PORT none of this runs.

Every API call carries Telegram's initData, signed with the bot's token: the server checks
the signature and its age, and takes the user from it. A user sees the chats they added
the bot to (the bot's owners see all), and a change is made only by a current admin of
that chat (asked from Telegram each time).

    GET  /                 the page (webapp/index.html)
    GET  /api/info         help, terms, support, the promo styles, the user's role
    GET  /api/chats        the user's chats
    GET  /api/chat/<id>    a chat: its token, the day and the week, top buyers, last trades, settings
    POST /api/chat/<id>    change its settings: {min_usd, whale_usd, emoji, sells, chart, digest,
                           alerts, lang, media_off}
    POST /api/chat/<id>/do run a chat command: {action: track|untrack|test, args: [...]}
    GET  /api/scan?address=0x…   the /scan of a token
    POST /api/promo        {address, style}: the /promo video, sent to the user's chat with the bot
    GET  /api/owner        the owners' panel (the bot's state)

Every write and command goes through the same code as the chat's commands (app.hooks, set by
bot.build_router) and its limits: /scan's per-person cooldown, /promo's queue and limits.
"""

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from pathlib import Path

from aiohttp import web

from . import __version__, abi, chain, health, i18n, promo, render, vibe
from . import scan as scanner
from .quotes import APPROX_KINDS
from .stats import token_day

log = logging.getLogger(__name__)

PAGE = Path(__file__).resolve().parent / "webapp" / "index.html"
SAMPLES = Path(__file__).resolve().parent.parent / "assets" / "promo-styles"   # the promo styles' clips
INIT_MAX_AGE = 24 * 3600   # seconds an initData stays good
SETTINGS = {"min_usd": (0, 1_000_000), "whale_usd": (0, 100_000_000)}
SWITCHES = ("sells", "digest", "alerts")
CHAT_ACTIONS = ("track", "untrack", "test")   # the commands the app's buttons run
WINDOWS = (("1h", 3600), ("24h", 86400), ("7d", 7 * 86400))
SERIES_POINTS = 240   # the chart's points at most: the last market cap of each slice of the week


def series(points: list[tuple], supply: float, n: int = SERIES_POINTS) -> list[list[float]]:
    """[time, market cap in the quote] from the kept trades, thinned to at most n points
    (the last trade of each of n equal slices), oldest first."""
    points = sorted(p for p in points if p[1] > 0)
    if not points:
        return []
    t0, t1 = points[0][0], points[-1][0]
    width = max(1.0, (t1 - t0) / n)
    out: dict[int, list[float]] = {}
    for ts, price, *_ in points:
        out[int((ts - t0) // width)] = [ts, price * supply]
    return [out[k] for k in sorted(out)]


def check_init_data(init_data: str, bot_token: str, now: float | None = None) -> dict | None:
    """The Telegram user of a Mini App's initData when its hash is the bot's and it is
    fresh, else None (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""
    try:
        data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    got = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not got or not hmac.compare_digest(want, got):
        return None
    try:
        if (time.time() if now is None else now) - int(data.get("auth_date", "0")) > INIT_MAX_AGE:
            return None
        user = json.loads(data.get("user") or "{}")
    except ValueError:
        return None
    return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None


def clean_settings(body: dict) -> dict | None:
    """The settings a POST may change, checked as the commands check them; None if any is wrong."""
    out = {}
    for key, (low, high) in SETTINGS.items():
        if key in body:
            try:
                v = float(body[key])
            except (TypeError, ValueError):
                return None
            if not low <= v <= high:
                return None
            out[key] = v
    for key in SWITCHES:
        if key in body:
            if not isinstance(body[key], bool):
                return None
            out[key] = int(body[key])
    if "chart" in body:
        if body["chart"] not in (0, 1, 2):
            return None
        out["chart"] = body["chart"]
    if "lang" in body:
        if body["lang"] not in i18n.LANGS:
            return None
        out["lang"] = body["lang"]
    if body.get("media_off") is True:
        out["media_kind"] = out["media_file_id"] = None
    if "emoji" in body:
        e = body["emoji"]
        if not isinstance(e, str) or not e or len(e) > 10 or any(
                c.isascii() and (c.isalnum() or c in "<>&\"'") for c in e):
            return None
        out["emoji"] = e
    return out


def build(app, bot) -> web.Application:
    """The aiohttp application; `app` is bot.App (db, rates, cfg)."""
    from .bot import (SCAN_COOLDOWN, cached_scan, get_scan, help_text, is_chat_admin,  # bot -> webapp
                      is_owner, support_text, terms_text)

    def scan_data(s) -> dict:
        """A scan.Scan as the app draws it: every part None when it could not be read."""
        short = render.short_addr
        return {
            "token": s.token, "symbol": s.symbol, "name": s.name, "complete": s.complete,
            "transfers": s.transfers, "holders": s.holders, "top_share": s.top_share,
            "top": [{"wallet": short(a), "share": share, "dev": a == s.dev} for a, share in s.top],
            "launched": s.launch_block is not None, "snipers": s.snipers, "sniper_share": s.sniper_share,
            "sniper_secs": round(scanner.SNIPER_BLOCKS / scanner.BLOCKS_PER_SECOND),
            "dev": short(s.dev) if s.dev else None, "dev_share": s.dev_share, "dev_sold": s.dev_sold,
            "dev_moved": s.dev_moved, "sellers": s.sellers, "made_ts": s.made_ts,
            "copies": [{"symbol": sym, "wallet": short(a)} for sym, a in s.copies[:3]],
            "explorer": app.cfg.explorer_url,
        }

    def lang_of(user: dict) -> str:
        """The person's language as the chat answers them (bot.user_lang)."""
        return i18n.norm(app.db.user_lang(user["id"]) or i18n.guess(user.get("language_code")))

    def user_of(request: web.Request) -> dict:
        user = check_init_data(request.headers.get("X-Init-Data", ""), app.cfg.bot_token)
        if user is None:
            raise web.HTTPUnauthorized(text="bad or old initData")
        return user

    def chat_of(request: web.Request, user: dict):
        try:
            chat_id = int(request.match_info["chat_id"])
        except ValueError:
            raise web.HTTPNotFound()
        chat = app.db.get_chat(chat_id)
        if chat is None or not (chat.added_by == user["id"] or is_owner(app, user["id"])):
            raise web.HTTPNotFound()
        return chat

    async def page(request):
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    async def chats(request):
        """The home page: the user's chats, each with its token's day (market cap, change,
        buys, a small chart), the day's totals and the latest trades of all their tokens."""
        user = user_of(request)
        mine = app.db.chats_of_user(user["id"])
        if is_owner(app, user["id"]):
            mine = app.db.active_chats(0, 100)[0]
        now = time.time()
        out, feed, seen = [], [], set()
        totals = {"tokens": 0, "buys": 0, "sells": 0, "volume_usd": 0.0, "posted_today": 0}
        for c in mine:
            totals["posted_today"] += app.db.chat_posted(c.chat_id)["today"]
            token = app.db.get_token(c.token) if c.token else None
            row = {"id": c.chat_id, "title": c.title, "kind": c.kind, "symbol": token.symbol if token else None}
            if token is not None:
                supply = token.total_supply / 10 ** token.decimals
                rate = await app.rates.get(token.quote_kind)
                points = app.db.trades_since(token.address, int(now - 86400))
                d = token_day(points, supply, now)
                row.update(name=token.name, graduated=token.graduated, rate=rate, quote_symbol=token.quote_symbol,
                           approx=token.quote_kind.startswith(APPROX_KINDS),
                           mc_now=d.mc_now if d else None, change=d.change if d else None,
                           buys=d.buys if d else 0, sells=d.sells if d else 0,
                           spark=[m for _, m in series(points, supply, 32)])
                if token.address not in seen:
                    seen.add(token.address)
                    totals["tokens"] += 1
                    if d:
                        totals["buys"] += d.buys
                        totals["sells"] += d.sells
                        totals["volume_usd"] += (d.buy_volume + d.sell_volume) * rate if rate else 0
                    feed += [{"symbol": token.symbol, "ts": ts, "side": side, "wallet": render.short_addr(w) if w else None,
                              "usd": q * rate if rate else None, "quote": q, "quote_symbol": token.quote_symbol}
                             for ts, q, side, w in app.db.recent_trades(token.address, 8)]
            out.append(row)
        feed.sort(key=lambda f: -f["ts"])
        return web.json_response({"user": user.get("first_name", ""), "chats": out, "totals": totals,
                                  "feed": feed[:8]})

    async def chat_get(request):
        user = user_of(request)
        chat = chat_of(request, user)
        token = app.db.get_token(chat.token) if chat.token else None
        settings = {"min_usd": chat.min_usd, "whale_usd": chat.whale_usd, "emoji": chat.emoji,
                    "sells": chat.sells, "chart": chat.chart, "digest": chat.digest, "alerts": chat.alerts,
                    "lang": chat.lang, "media": chat.media_kind}
        out = {"id": chat.chat_id, "title": chat.title, "settings": settings, "token": None,
               "health": health.snapshot(app, chat_id=chat.chat_id),
               "health_text": health.report(app, lang_of(user), chat.chat_id)}
        if token is None:
            return web.json_response(out)
        now = time.time()
        supply = token.total_supply / 10 ** token.decimals
        rate = await app.rates.get(token.quote_kind)
        approx = token.quote_kind.startswith(APPROX_KINDS)

        def day_of(window):
            d = token_day(app.db.trades_since(token.address, int(now - window)), supply, now, window=window)
            if d is None:
                return None
            return {"hours": d.hours, "buys": d.buys, "sells": d.sells, "buy_volume": d.buy_volume,
                    "sell_volume": d.sell_volume, "mc_now": d.mc_now, "mc_high": d.mc_high,
                    "mc_low": d.mc_low, "change": d.change}

        windows = {}
        for name, window in WINDOWS:   # the 1h / 24h / 7d switch: its numbers, buyers and top buyers
            d = day_of(window)
            if d is not None:
                d["buyers"] = app.db.day_counts(token.address, int(now - window))["buyers"]
                d["top"] = [{"wallet": render.short_addr(w), "quote": q, "count": n}
                            for w, q, n in app.db.top_buyers(token.address, int(now - window), limit=3)[0]]
            windows[name] = d
        counts = app.db.day_counts(token.address, int(now - 86400))
        top, wallets = app.db.top_buyers(token.address, int(now - 86400), limit=5)
        progress = None
        if token.kind == "vibe" and not token.graduated:  # the curve's %, as the posts and the site show it
            try:
                st = await vibe.state(app.rpc, token.curve)
                progress = st.progress if st else None
            except Exception as e:
                log.info("mini app: curve of %s unreadable: %s", token.symbol, e)
        out["token"] = {
            "address": token.address, "symbol": token.symbol, "name": token.name,
            "quote_symbol": token.quote_symbol, "rate": rate, "approx": approx,
            "graduated": token.graduated, "launchpad": "vibe/vibe" if token.kind == "vibe" else token.kind,
            "chart_url": app.cfg.trade_url.format(token=token.address) if app.cfg.trade_url else "",
            "day": day_of(86400), "week": day_of(7 * 86400), "buyers": counts["buyers"],
            "top": [{"wallet": render.short_addr(w), "quote": q, "count": n} for w, q, n in top],
            "recent": [{"ts": ts, "side": side, "quote": q, "wallet": render.short_addr(w) if w else None}
                       for ts, q, side, w in app.db.recent_trades(token.address, 12)],
            "windows": windows, "progress": progress,
            "series": series(app.db.trades_since(token.address, int(now - 7 * 86400)), supply),
        }
        return web.json_response(out)

    async def admin_of(request: web.Request):
        user = user_of(request)
        chat = chat_of(request, user)
        if not is_owner(app, user["id"]) and not await is_chat_admin(bot, chat.chat_id, user["id"]):
            raise web.HTTPForbidden(text="not an admin of this chat")
        return user, chat

    async def chat_do(request):
        """A chat command from the app's buttons, through the command's own code."""
        user, chat = await admin_of(request)
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="not JSON")
        name = body.get("action") if isinstance(body, dict) else None
        args = body.get("args") or [] if isinstance(body, dict) else []
        if name not in CHAT_ACTIONS or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise web.HTTPBadRequest(text="unknown action")
        text, _ = await app.hooks["actions"][name](bot, chat.chat_id, args[:2], None, lang_of(user))
        log.info("mini app: %s ran %s in %s", user["id"], name, chat.chat_id)
        return web.json_response({"ok": True, "text": text or ""})

    async def info(request):
        user = user_of(request)
        lang = lang_of(user)
        has_promo = app.cfg.chain_id == promo.CHAIN_ID
        return web.json_response({
            "owner": is_owner(app, user["id"]), "username": app.username, "langs": list(i18n.LANGS),
            "chain_id": app.cfg.chain_id, "network": chain.network(app.cfg.chain_id), "version": __version__,
            "promo": has_promo, "promo_unavailable": i18n.t(lang, "promo_testnet_only"),
            "help": help_text(lang), "terms": terms_text(app, lang), "support": support_text(app, lang),
            "styles": [[k, promo.STYLE_NAMES[k]] for k in promo.STYLES] if has_promo else []})

    async def scan(request):
        """/scan from the app: the same answer and the same one-fresh-scan-a-minute limit."""
        user = user_of(request)
        address = request.query.get("address", "")
        if not abi.is_address(address):
            raise web.HTTPBadRequest(text="not an address")
        address = address.lower()
        lang = lang_of(user)
        if cached_scan(app, address) is None and not is_owner(app, user["id"]):
            wait = app.scan_at.get(user["id"], 0) + SCAN_COOLDOWN - time.time()
            if wait > 0:
                return web.json_response({"wait": max(1, round(wait)),
                                          "text": i18n.t(lang, "scan_wait", s=max(1, round(wait)))})
            app.scan_at[user["id"]] = time.time()
        try:
            s = await get_scan(app, address)
        except scanner.ScanError as e:
            return web.json_response({"error": i18n.t(lang, e.key)})
        except Exception as e:
            log.warning("mini app: scan of %s failed: %s", address, e)
            return web.json_response({"error": i18n.t(lang, "chain_down")})
        return web.json_response({"scan": scan_data(s), "text": render.scan_text(s, lang, app.cfg.explorer_url)})

    async def promo_video(request):
        """/promo from the app: the video goes to the user's chat with the bot, in turn with the
        others (promo_make keeps the limits and says when it is the user's turn)."""
        user = user_of(request)
        if app.cfg.chain_id != promo.CHAIN_ID:
            raise web.HTTPBadRequest(text=i18n.t(lang_of(user), "promo_testnet_only"))
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="not JSON")
        address, style = (body.get("address"), body.get("style")) if isinstance(body, dict) else (None, None)
        if not isinstance(address, str) or not abi.is_address(address) or style not in promo.STYLES:
            raise web.HTTPBadRequest(text="an address and a style, please")
        from aiogram.enums import ChatType
        app.hooks["spawn"](app.hooks["promo_make"](bot, user["id"], ChatType.PRIVATE, None, user["id"],
                                                   lang_of(user), address.lower(), style))
        return web.json_response({"ok": True})

    async def owner(request):
        user = user_of(request)
        if not is_owner(app, user["id"]):
            raise web.HTTPNotFound()

        class U:  # panel() reads the person's id and language
            id, language_code = user["id"], user.get("language_code")
        text, _ = await app.hooks["panel"](U, "home")
        return web.json_response({"text": text})

    async def chat_post(request):
        user, chat = await admin_of(request)
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="not JSON")
        fields = clean_settings(body if isinstance(body, dict) else {})
        if fields is None:
            raise web.HTTPBadRequest(text="a setting is out of range")
        if fields:
            app.db.update_chat(chat.chat_id, **fields)
            log.info("mini app: %s changed %s in %s", user["id"], sorted(fields), chat.chat_id)
        return web.json_response({"ok": True})

    w = web.Application(client_max_size=16 * 1024)
    w.router.add_get("/", page)
    async def sample(request):
        style = request.match_info["style"]
        if style not in promo.STYLES or not (SAMPLES / f"{style}.mp4").exists():
            raise web.HTTPNotFound()
        return web.FileResponse(SAMPLES / f"{style}.mp4", headers={"Cache-Control": "max-age=86400"})

    w.router.add_get("/samples/{style}.mp4", sample)
    w.router.add_get("/api/info", info)
    w.router.add_get("/api/scan", scan)
    w.router.add_post("/api/promo", promo_video)
    w.router.add_get("/api/owner", owner)
    w.router.add_post("/api/chat/{chat_id}/do", chat_do)
    w.router.add_get("/api/chats", chats)
    w.router.add_get("/api/chat/{chat_id}", chat_get)
    w.router.add_post("/api/chat/{chat_id}", chat_post)
    return w


async def start(app, bot, port: int, host: str = "127.0.0.1") -> web.AppRunner:
    runner = web.AppRunner(build(app, bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("mini app on http://%s:%d", host, port)
    return runner
