"""Promo video of the testnet bot: Silverhand Buys on vibe/vibe launches, Robinhood Chain testnet.

    python tools/make_promo.py                     # assets/promo/promo-16x9.mp4 and promo-9x16.mp4
    python tools/make_promo.py --format 16x9       # one of them
    python tools/make_promo.py --preview           # half size, 15 fps: a quick look
    python tools/make_promo.py --stills 2,10,20    # PNG frames at these seconds, no video

Everything on screen is the bot's own work: the intro is tools/make_intro.py's scene, the
cards are drawn by buybot/card.py and the posts by buybot/render.py from a real vibe/vibe curve
on testnet (fixtures/vibe_v6_testnet.json, replayed from launch to graduation through the
bot's watcher), the badges are the bot's custom emoji (assets/emoji). The sound is synthesised
here (no third-party music): a quiet melodic bed under soft effects.

Needs Pillow, numpy and ffmpeg (pip install imageio-ffmpeg).
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from buybot import card as cardmod, chain, emoji, i18n, render, vibe  # noqa: E402
from buybot.db import Chat, Db, Token  # noqa: E402
from buybot.sender import card_spec  # noqa: E402
from buybot.watcher import Watcher, trade_rows  # noqa: E402
import make_intro  # noqa: E402
import promo_music  # noqa: E402

OUT = ROOT / "assets" / "promo"
FIXTURE = ROOT / "fixtures" / "vibe_v6_testnet.json"
BOT = "@siverhandbuysTNbot"
ETH_USD = 3000.0  # the dollar rate the posts and cards are drawn at

# colours: Silverhand green for the brand, vibe/vibe lime for the launchpad
BG = (6, 8, 8)
GREEN = (45, 245, 154)
LIME = (223, 249, 2)
INK = (238, 243, 241)
DIM = (140, 152, 147)
RED = (255, 77, 109)
TG_BG = (14, 22, 33)      # Telegram's dark chat
TG_BUBBLE = (24, 37, 51)
TG_ME = (43, 82, 120)
TG_NAME = (100, 181, 239)

SANS = ROOT / "assets" / "fonts" / "InstrumentSans.ttf"
SYMBOL_FONTS = ["C:/Windows/Fonts/seguisym.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
EMOJI_FONTS = ["C:/Windows/Fonts/seguiemj.ttf", "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"]


# ----------------------------------------------------------------------------- helpers
def ease(x):
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


def ease_io(x):
    x = min(1.0, max(0.0, x))
    return 0.5 - 0.5 * math.cos(math.pi * x)


def seg(t, a, b):
    return min(1.0, max(0.0, (t - a) / (b - a))) if b > a else float(t >= b)


def mix(a, b, k):
    return tuple(int(round(x * k + y * (1 - k))) for x, y in zip(a, b))


def font(role, px):
    return cardmod.font(role, px)


_fcache = {}


def _first(paths, px):
    key = (tuple(paths), px)
    if key not in _fcache:
        _fcache[key] = next((ImageFont.truetype(p, px) for p in paths if Path(p).exists()), None)
    return _fcache[key]


def ffmpeg():
    exe = make_intro.find_ffmpeg()
    if not exe:
        sys.exit("ffmpeg is needed: pip install imageio-ffmpeg")
    return exe


def fit_cover(img, w, h):
    k = max(w / img.width, h / img.height)
    img = img.resize((max(w, round(img.width * k)), max(h, round(img.height * k))), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def rounded(size, r):
    w, h = size
    m = Image.new("L", (w * 2, h * 2), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * 2 - 1, h * 2 - 1), r * 2, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def shadow(size, r, blur, alpha=150):
    w, h = size
    pad = blur * 3
    m = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    ImageDraw.Draw(m).rounded_rectangle((pad, pad, pad + w, pad + h), r, fill=alpha)
    return m.filter(ImageFilter.GaussianBlur(blur)), pad


def paste_alpha(dst, src, xy, alpha=1.0):
    """Paste an RGBA (or RGB) image with an extra opacity."""
    if alpha <= 0:
        return
    if src.mode != "RGBA":
        src = src.convert("RGBA")
    if alpha < 1:
        a = src.getchannel("A").point(lambda v: int(v * alpha))
        src = src.copy()
        src.putalpha(a)
    dst.alpha_composite(src, (int(xy[0]), int(xy[1])))


def text_w(d, s, f):
    return d.textlength(s, font=f)


# ----------------------------------------------------------------------------- badges
class Badges:
    """The bot's custom emoji, decoded from their WEBM (VP9 with alpha) into RGBA frames."""

    def __init__(self):
        self.frames = {}

    def get(self, name, t, px):
        if name not in self.frames:
            src = ROOT / "assets" / "emoji" / f"{name}.webm"
            raw = subprocess.run([ffmpeg(), "-v", "error", "-c:v", "libvpx-vp9", "-i", str(src),
                                  "-f", "rawvideo", "-pix_fmt", "rgba", "-"], capture_output=True, check=True).stdout
            n = len(raw) // (100 * 100 * 4)
            self.frames[name] = [Image.frombytes("RGBA", (100, 100), raw[i * 40000:(i + 1) * 40000]) for i in range(n)]
        frames = self.frames[name]
        img = frames[int(t * 30) % len(frames)]
        return img if px == 100 else img.resize((px, px), Image.LANCZOS)


BADGES = Badges()


# ----------------------------------------------------------------------------- the data
def build_data():
    """Replay one real vibe/vibe curve (launch to graduation) through the bot's watcher into a
    throwaway database: the trades for the chart and the payloads of the posts."""
    fx = json.loads(FIXTURE.read_text())
    launch = vibe.decode_launch(fx["launch"]["log"], fx["factory"])
    token = Token(address=launch.token, curve=launch.curve, deployer=launch.launcher, pair=chain.NATIVE,
                  quote_symbol="ETH", quote_decimals=18, quote_kind="eth", name=launch.name or launch.symbol,
                  symbol=launch.symbol or "UG", decimals=18, total_supply=10 ** 27, threshold=launch.target,
                  pool_id=vibe.pool_id(launch.token, chain.NATIVE, fx["hook"]),
                  curve_k=vibe.curve_k(launch.target, 8 * 10 ** 26), phantom=vibe.virtual_quote(launch.target),
                  graduated=False, added_at=0, kind="vibe")
    tmp = tempfile.mkdtemp()
    db = Db(os.path.join(tmp, "promo.db"))
    db.upsert_token(token)

    class NoRpc:
        pass

    class Eth:
        current = ETH_USD

        async def get(self):
            return ETH_USD

    w = Watcher(NoRpc(), db, Eth())
    w.chain_id = chain.TESTNET_CHAIN_ID
    logs = fx["curve"]["logs"]
    blocks = [int(lg["blockNumber"], 16) for lg in logs]
    b0, b1 = min(blocks), max(blocks)
    now = time.time()
    span = 5.5 * 3600

    def clock(b):  # the curve's life drawn over the last few hours
        return now - span - 300 + (b - b0) / max(1, b1 - b0) * span

    events = w._events(logs, {}, {token.address: token}, {}, {"eth": ETH_USD}, clock)
    db.add_trades(trade_rows(events))
    trades = [e for e in events if e[2] in ("buy", "sell")]
    grad = next(e[5] for e in events if e[2] == "grad")
    buys = [e[5] for e in trades if e[2] == "buy"]
    sells = [e[5] for e in trades if e[2] == "sell"]
    first = buys[0]
    whale = max(buys[1:-1], key=lambda p: p["usd"] or 0)
    # a buy around the middle of the curve, and the last buy before graduation
    mid = min(buys, key=lambda p: abs((p.get("progress") or 0) - 0.55))
    late = max((p for p in buys if (p.get("progress") or 0) < 0.97), key=lambda p: p.get("progress") or 0)
    sell = max(sells, key=lambda p: p["usd"] or 0)
    return {"token": token, "db": db, "first": first, "whale": whale, "mid": mid, "late": late,
            "sell": sell, "grad": grad, "now": now, "trades": [(e[2], e[5]) for e in trades]}


def make_card(data, p, side, size, seconds=6.0):
    """The bot's own trade card for this payload, as seen at the payload's moment."""
    db, token = data["db"], data["token"]
    spec = card_spec(db, token, p, side, ETH_USD, whale_usd=1000, now=int(p["ts"]) + 60)
    spec["now"] = int(data["now"])
    return cardmod.Card(spec, size=size, seconds=seconds, fps=30)


# ----------------------------------------------------------------------------- telegram posts
EMOJI_BADGE = dict(emoji._BY_EMOJI)


def parse_html(text):
    """Lines of (text, bold) runs from the bot's HTML (only <b> and <i> matter here)."""
    lines = []
    for raw in text.split("\n"):
        runs, bold, buf, i = [], False, "", 0
        while i < len(raw):
            if raw.startswith("<b>", i) or raw.startswith("</b>", i):
                if buf:
                    runs.append((buf, bold))
                buf = ""
                bold = raw.startswith("<b>", i)
                i += 3 if bold else 4
                continue
            if raw[i] == "<":
                j = raw.find(">", i)
                i = j + 1 if j > 0 else i + 1
                continue
            buf += raw[i]
            i += 1
        if buf:
            runs.append((buf, bold))
        lines.append(runs)
    return [[(r.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"), b) for r, b in ln]
            for ln in lines]


def _chunks(s):
    """Split a run into badge emoji, other emoji / symbols, and plain text."""
    out, buf = [], ""
    i = 0
    keys = sorted(EMOJI_BADGE, key=len, reverse=True)
    while i < len(s):
        hit = next((k for k in keys if s.startswith(k, i)), None)
        if hit:
            if buf:
                out.append(("t", buf))
                buf = ""
            out.append(("badge", EMOJI_BADGE[hit]))
            i += len(hit)
            continue
        ch = s[i]
        if ord(ch) >= 0x2190 and ch not in "…·—–":
            if buf:
                out.append(("t", buf))
                buf = ""
            j = i + 1
            while j < len(s) and s[j] in "\ufe0f\u200d":
                j += 1
            out.append(("sym", s[i:j]))
            i = j
            continue
        buf += ch
        i += 1
    if buf:
        out.append(("t", buf))
    return out


def draw_message(text, width, px, t, sender=None, me=False, time_s="12:00", badges=True):
    """A Telegram message bubble (RGBA) with the bot's text, its custom emoji animated at t.
    Long lines wrap at spaces, and a word longer than the bubble (an address) at any letter."""
    f_reg, f_bold = font("uir", px), font("ui", px)
    f_sym = _first(SYMBOL_FONTS, px)
    f_emo = _first(EMOJI_FONTS, int(px * 0.92))
    f_tree = font("mono", px * 1.05)
    pad, lh = int(px * 0.75), int(px * 1.42)
    head = int(px * 1.35) if sender else 0
    inner = width - 2 * pad
    bsz = int(px * 1.25)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))

    def is_emo(ch):
        return ord(ch[0]) >= 0x1F000 or ch[0] in "✅⚡❌"

    def items_of(runs):
        out = []  # (kind, s, bold, width)
        for run, bold in runs:
            for kind, s in _chunks(run):
                if kind == "badge" and badges:
                    out.append(("badge", s, bold, bsz + px * 0.12))
                elif kind == "sym" and s in "├└│":
                    out.append(("tree", s, bold, text_w(probe, s, f_tree)))
                elif kind == "sym" and s in "▰▱":
                    out.append(("bar", s, bold, px * 0.66))
                elif kind in ("sym", "badge"):
                    ch = s if kind == "sym" else emoji.PACK[s]
                    f = f_emo if is_emo(ch) and f_emo else f_sym
                    out.append(("sym", ch, bold, text_w(probe, ch, f) + (px * 0.1 if is_emo(ch) else 0)))
                else:
                    f = f_bold if bold else f_reg
                    for word in re.split("(?<= )", s):
                        if not word:
                            continue
                        ww = text_w(probe, word, f)
                        if ww <= inner:
                            out.append(("t", word, bold, ww))
                            continue
                        piece = ""
                        for ch in word:  # an address: break anywhere
                            if text_w(probe, piece + ch, f) > inner and piece:
                                out.append(("t", piece, bold, text_w(probe, piece, f)))
                                piece = ""
                            piece += ch
                        if piece:
                            out.append(("t", piece, bold, text_w(probe, piece, f)))
        return out

    lines = []
    for runs in parse_html(text):
        line, x = [], 0.0
        for it in items_of(runs):
            if line and x + it[3] > inner and not (it[0] == "t" and it[1] == " "):
                lines.append(line)
                line, x = [], 0.0
                if it[0] == "t" and it[1].startswith(" "):
                    it = ("t", it[1].lstrip(), it[2], text_w(probe, it[1].lstrip(), f_bold if it[2] else f_reg))
            line.append(it)
            x += it[3]
        lines.append(line)

    h = pad + head + lh * len(lines) + int(px * 0.9) + pad // 2
    img = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    bub = Image.new("RGBA", (width, h), (*(TG_ME if me else TG_BUBBLE), 255))
    img.paste(bub, (0, 0), rounded((width, h), int(px * 0.9)))
    d = ImageDraw.Draw(img)
    if sender:
        d.text((pad, pad - px * 0.1), sender, font=f_bold, fill=TG_NAME)
    y = pad + head
    for line in lines:
        x = pad
        for kind, s, bold, w in line:
            if kind == "badge":
                paste_alpha(img, BADGES.get(s, t, bsz), (x, y + (lh - bsz) // 2 - px * 0.12))
            elif kind == "tree":
                d.text((x, y - px * 0.02), s, font=f_tree, fill=(120, 140, 160))
            elif kind == "bar":
                bw_, bh_ = px * 0.52, px * 0.62
                y0 = y + (lh - bh_) / 2 - px * 0.1
                box = (x + px * 0.05, y0, x + px * 0.05 + bw_, y0 + bh_)
                if s == "▰":
                    d.rounded_rectangle(box, px * 0.1, fill=GREEN)
                else:
                    d.rounded_rectangle(box, px * 0.1, outline=(90, 110, 130), width=max(1, int(px * .07)))
            elif kind == "sym":
                f = f_emo if is_emo(s) and f_emo else f_sym
                d.text((x, y + px * 0.08), s, font=f, fill=INK, embedded_color=is_emo(s))
            else:
                d.text((x, y), s, font=f_bold if bold else f_reg, fill=INK)
            x += w
        y += lh
    ft = font("uir", px * 0.72)
    d.text((width - pad - text_w(d, time_s, ft), h - pad // 2 - px * 0.95), time_s, font=ft,
           fill=(156, 196, 230) if me else (109, 127, 147))
    return img


def post_text(p, sell=False):
    chat = Chat(chat_id=-1, title="", token=p["token"], min_usd=0, emoji="🟢", step_usd=10,
                media_kind=None, media_file_id=None, active=True, created_at=0)
    return render.buy_text(p, chat, sell=sell)


# ----------------------------------------------------------------------------- scenes
class Promo:
    MUSIC = "simple"   # the music style that goes with this cut
    XFADE = 0.3        # seconds of crossfade into each scene; 0 for hard cuts

    def __init__(self, W, H, fps, data):
        self.W, self.H, self.fps, self.data = W, H, fps, data
        self.vert = H > W
        self.u = min(W, H) / 1080          # 1 unit = 1 px at 1080
        self.events = []                    # (time, sound, params) for the soundtrack
        self.scenes = []
        self._cache = {}
        self._build()

    # -- timeline
    def _build(self):
        sc = self.scenes
        T = 0.0
        for name, dur in (("intro", 4.0), ("pitch", 4.6), ("track", 5.0), ("cards", 11.2),
                          ("feed", 6.4), ("grad", 4.6), ("outro", 5.0)):
            sc.append((name, T, dur))
            T += dur
        self.length = T
        self._sounds()

    def _sounds(self):
        ev = self.events
        start = {n: s for n, s, _ in self.scenes}
        ev += [(0.15, "impact", {"gain": .8}), (0.1, "riser", {"dur": .5, "gain": .25})]
        s = start["pitch"]
        ev += [(s - .25, "whoosh", {"dur": .5})]
        for i in range(3):
            ev += [(s + .25 + i * .85, "tick", {"gain": .7})]
        for i in range(6):
            ev += [(s + .45 + i * .32, "pop", {"pitch": 1 + i * .08, "gain": .45})]
        s = start["track"]
        ev += [(s - .25, "whoosh", {"dur": .5})]
        for i in range(16):
            ev += [(s + .7 + i * .07, "key", {"gain": .35})]
        ev += [(s + 2.0, "send", {}), (s + 2.6, "coin", {"gain": .6})]
        s = start["cards"]
        ev += [(s - .25, "whoosh", {"dur": .55})]
        for k, at in enumerate((s, s + 5.6)):
            for i in range(12):
                ev += [(at + .35 + i * .1, "tick", {"gain": .25, "pitch": 1.4 + i * .03})]
            ev += [(at + 1.55, "coin", {"gain": .8 if k else .6})]
        ev += [(s + 5.35, "whoosh", {"dur": .5})]
        s = start["feed"]
        ev += [(s - .25, "whoosh", {"dur": .5})]
        for i in range(4):
            ev += [(s + .35 + i * 1.15, "pop", {"pitch": .9 if i == 2 else 1.15, "gain": .7})]
        s = start["grad"]
        ev += [(s - .2, "whoosh", {"dur": .45}), (s + .2, "riser", {"dur": 2.2, "gain": .55}),
               (s + 2.45, "impact", {"gain": 1.0}), (s + 2.45, "coin", {"gain": .7, "pitch": 1.5})]
        s = start["outro"]
        ev += [(s - .25, "whoosh", {"dur": .55}), (s + .1, "chord", {"dur": 4.2, "gain": .5})]

    def frame(self, t):
        img = None
        for i, (name, s, dur) in enumerate(self.scenes):
            if s <= t < s + dur or (i == len(self.scenes) - 1 and t >= s):
                img = getattr(self, "s_" + name)(t - s, dur)
                break
        # a short crossfade into each scene
        for i, (name, s, dur) in enumerate(self.scenes[1:], 1):
            if self.XFADE and s <= t < s + self.XFADE:
                prev = self.scenes[i - 1]
                a = ease_io((t - s) / self.XFADE)
                img = Image.blend(getattr(self, "s_" + prev[0])(t - prev[1], prev[2]).convert("RGB"),
                                  img.convert("RGB"), a)
        img = img.convert("RGB")
        if t < 0.5:
            img = Image.blend(Image.new("RGB", img.size), img, ease(t / 0.5))
        if t > self.length - 0.7:
            img = Image.blend(img, Image.new("RGB", img.size), ease_io((t - self.length + 0.7) / 0.7))
        return img

    def music_scenes(self):
        """The sections the music is arranged to (intro, pitch, track, cards, feed, grad, outro)."""
        return self.scenes

    def grad_time(self):
        """When the curve is full: the music lands its downbeat there."""
        return next(s for name, s, _ in self.scenes if name == "grad") + GRAD_HIT

    # -- shared pieces
    def bg(self, accent=GREEN, k=1.0):
        key = ("bg", accent, round(k, 2))
        if key not in self._cache:
            W, H, u = self.W, self.H, self.u
            img = Image.new("RGB", (W, H), BG)
            glow = Image.new("L", (W // 8, H // 8), 0)
            gd = ImageDraw.Draw(glow)
            gw, gh = glow.size
            gd.ellipse((gw * .1, -gh * .5, gw * .9, gh * .55), fill=110)
            glow = glow.filter(ImageFilter.GaussianBlur(gw * .1)).resize((W, H), Image.BILINEAR)
            img = Image.composite(Image.new("RGB", (W, H), accent), img, glow.point(lambda v: v * .22 * k))
            d = ImageDraw.Draw(img)
            g = int(36 * u)
            dot = mix(accent, BG, .14)
            for x in range(g, W, g):
                for y in range(g, H, g):
                    d.rectangle((x - 1, y - 1, x + 1, y + 1), fill=dot)
            self._cache[key] = img
        return self._cache[key].copy().convert("RGBA")

    def tapes(self, img, t, color=GREEN):
        band = int(16 * self.u)
        tp = make_intro.tape(self.W, band, (t * .6) % 1)
        if color != GREEN:
            arr = np.array(tp)
            mask = (arr[:, :, 1] > 100)
            arr[mask] = color
            tp = Image.fromarray(arr)
        img.paste(tp, (0, 0))
        img.paste(tp, (0, self.H - band))

    def caption(self, img, t, eyebrow, title, y, accent=GREEN, appear=0.0, size=64):
        """An eyebrow and a title, centred, sliding up into place."""
        u = self.u
        d = ImageDraw.Draw(img)
        a = ease(seg(t, appear, appear + .5))
        if a <= 0:
            return
        fe, ft = font("num", 20 * u), font("hero", size * u)
        dy = (1 - a) * 30 * u
        ew = make_intro.spaced_width(d, eyebrow, fe, 5 * u)
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        make_intro.spaced(ld, ((self.W - ew) / 2, y + dy), eyebrow, fe, (*accent, 255), 5 * u)
        lines = title.split("\n")
        ty = y + 38 * u + dy
        for ln in lines:
            tw = text_w(ld, ln, ft)
            ld.text(((self.W - tw) / 2, ty), ln, font=ft, fill=(*INK, 255))
            ty += size * u * 1.12
        paste_alpha(img, layer, (0, 0), a)

    def letterbox(self, img, t):
        """A 16:9 scene as the whole frame, or as a band across the middle of a vertical one."""
        if not self.vert:
            return img if img.size == (self.W, self.H) else fit_cover(img, self.W, self.H)
        bw = self.W
        bh = round(bw * img.height / img.width)
        band = img.resize((bw, bh), Image.LANCZOS)
        out = self.bg(GREEN, .6)
        y = (self.H - bh) // 2
        sh, pad = shadow((bw, bh), 1, int(40 * self.u), 200)
        layer = Image.new("RGBA", sh.size, (0, 0, 0, 0))
        layer.putalpha(sh)
        paste_alpha(out, layer, (-pad, y - pad))
        out.paste(band, (0, y))
        self.tapes(out, t)
        return out.convert("RGB")

    # -- 1. intro: the bot's own intro scene, at full resolution
    def s_intro(self, t, dur):
        key = "intro_scene"
        if key not in self._cache:
            base_w, base_h = 640, 360  # the scene's own 16:9 layout; a band across the vertical frame
            sc = make_intro.Scene(base_w, base_h, 3, "SILVERHAND BUYS", "// BUY BOT · NOW ON VIBE/VIBE",
                                  "EVERY BUY · LIVE IN YOUR CHAT", str(ROOT / "assets" / "brand" / "avatar.png"))
            sc.W, sc.H = sc.w, sc.h
            self._cache[key] = sc
        sc = self._cache[key]
        img = sc.frame((t / 3.0) % 1.0)
        img = self.letterbox(img, t)
        k = 1 + 0.04 * ease_io(t / dur)       # a slow push in
        if k > 1:
            w, h = int(self.W / k), int(self.H / k)
            img = img.crop(((self.W - w) // 2, (self.H - h) // 2, (self.W + w) // 2, (self.H + h) // 2)).resize(
                (self.W, self.H), Image.LANCZOS)
        return img.convert("RGBA")

    # -- 2. the pitch: three lines and the badges around them
    def s_pitch(self, t, dur):
        u, W, H = self.u, self.W, self.H
        img = self.bg(LIME, .9)
        lines = [("Every vibe/vibe launch.", LIME), ("Every buy.", INK), ("Live in your chat.", GREEN)]
        size = 104 if not self.vert else 92
        f = font("hero", size * u)
        total = len(lines) * size * u * 1.18
        y0 = (H - total) / 2 - 10 * u
        for i, (ln, col) in enumerate(lines):
            a = ease(seg(t, .25 + i * .85, .75 + i * .85))
            if a <= 0:
                continue
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            lw = text_w(ld, ln, f)
            if self.vert and lw > W - 80 * u:
                f2 = font("hero", size * u * (W - 80 * u) / lw)
            else:
                f2 = f
            lw = text_w(ld, ln, f2)
            x = (W - lw) / 2
            y = y0 + i * size * u * 1.18 + (1 - a) * 40 * u
            if i == 0:  # "vibe/vibe" in lime, the rest white
                head = "Every "
                ld.text((x, y), head, font=f2, fill=(*INK, 255))
                ld.text((x + text_w(ld, head, f2), y), "vibe/vibe", font=f2, fill=(*LIME, 255))
                ld.text((x + text_w(ld, head + "vibe/vibe", f2), y), " launch.", font=f2, fill=(*INK, 255))
            else:
                ld.text((x, y), ln, font=f2, fill=(*col, 255))
            paste_alpha(img, layer, (0, 0), a)
        # badges float in around the words
        spots = ([(.13, .22), (.86, .2), (.08, .7), (.9, .74), (.3, .86), (.7, .1)] if not self.vert else
                 [(.18, .2), (.82, .17), (.12, .78), (.86, .8), (.5, .9), (.5, .1)])
        names = ["buy", "whale", "rocket", "curve", "dev", "mc"]
        for i, ((fx, fy), name) in enumerate(zip(spots, names)):
            a = ease(seg(t, .45 + i * .32, .85 + i * .32))
            if a <= 0:
                continue
            px = int((130 if i < 2 else 104) * u * (.6 + .4 * a))
            bob = math.sin(t * 2.2 + i) * 8 * u
            paste_alpha(img, BADGES.get(name, t + i * .3, px), (fx * W - px / 2, fy * H - px / 2 + bob), a)
        self.tapes(img, t, LIME)
        return img

    # -- 3. /track in a group
    def chat_window(self, t, msgs, title, subtitle, width, height, scroll=0.0):
        """A Telegram group window with messages: (appear_at, image, align)."""
        u = self.u
        win = Image.new("RGBA", (width, height), (*TG_BG, 255))
        d = ImageDraw.Draw(win)
        bar = int(84 * u)
        d.rectangle((0, 0, width, bar), fill=(23, 33, 43, 255))
        av = int(52 * u)
        logo, mask = make_intro.circle_logo(ROOT / "assets" / "brand" / "avatar.png", av)
        win.paste(logo, (int(22 * u), (bar - av) // 2), mask)
        d.text((int(90 * u), int(14 * u)), title, font=font("ui", 26 * u), fill=INK)
        d.text((int(90 * u), int(46 * u)), subtitle, font=font("uir", 20 * u), fill=(109, 127, 147))
        # messages stack from the bottom
        y = height - int(24 * u) + scroll
        for at, msg, align in reversed(msgs):
            a = ease(seg(t, at, at + .35))
            if a <= 0:
                continue
            y -= msg.height + int(14 * u)
            x = width - msg.width - int(20 * u) if align == "right" else int(20 * u)
            yy = y + (1 - a) * 30 * u
            if yy + msg.height < bar:
                break
            paste_alpha(win, msg, (x, yy), a)
        d.rectangle((0, 0, width, bar), fill=(23, 33, 43, 255))
        win.paste(logo, (int(22 * u), (bar - av) // 2), mask)
        d.text((int(90 * u), int(14 * u)), title, font=font("ui", 26 * u), fill=INK)
        d.text((int(90 * u), int(46 * u)), subtitle, font=font("uir", 20 * u), fill=(109, 127, 147))
        return win

    def window_frame(self, img, win, x, y, a=1.0):
        u = self.u
        sh, pad = shadow(win.size, int(26 * u), int(30 * u), 170)
        layer = Image.new("RGBA", (sh.width, sh.height), (0, 0, 0, 0))
        layer.putalpha(sh)
        paste_alpha(img, layer, (x - pad, y - pad + 14 * u), a)
        m = rounded(win.size, int(26 * u))
        w2 = win.copy()
        w2.putalpha(Image.composite(win.getchannel("A"), Image.new("L", win.size, 0), m))
        paste_alpha(img, w2, (x, y), a)

    def s_track(self, t, dur):
        u, W = self.u, self.W
        img = self.bg(GREEN, .8)
        data = self.data
        tok = data["token"]
        self.caption(img, t, "// STEP 1", "Add the bot. Send /track.", 70 * u if not self.vert else 170 * u,
                     GREEN, 0.05, 60 if not self.vert else 66)
        width = int(1000 * u) if not self.vert else int(940 * u)
        height = int(720 * u) if not self.vert else int(1100 * u)
        px = 27 * u if not self.vert else 30 * u
        typed = "/track " + tok.address
        n = int(len(typed) * ease_io(seg(t, .7, 1.9)))
        cmd = typed[:n] + ("▍" if t < 2.0 and int(t * 4) % 2 == 0 else "")
        reply = i18n.t("en", "tracking", symbol=tok.symbol, name=tok.name, venue=i18n.t("en", "track_curve"),
                       sells=i18n.t("en", "tracking_sells_on"), minbuy="$0")
        msgs = [(0.6, draw_message(cmd or " ", int(width * .86), px, t, me=True, time_s="12:00"), "right")]
        if t >= 2.5:
            msgs.append((2.55, draw_message(reply, int(width * .86), px, t, sender="Silverhand Buys",
                                            time_s="12:00"), "left"))
        win = self.chat_window(t, msgs, "Your community", "vibe/vibe launch · 1,204 members", width, height)
        x = (W - width) // 2
        y = int(200 * u) if not self.vert else int(400 * u)
        a = ease(seg(t, 0, .45))
        self.window_frame(img, win, x, y + (1 - a) * 40 * u, a)
        self.tapes(img, t)
        return img

    # -- 4. the live cards
    def s_cards(self, t, dur):
        u, W = self.u, self.W
        img = self.bg(LIME, .7)
        if not self.vert:
            cw = int(1440 * u)
            cy = int(205 * u)
        else:
            cw = int(1000 * u)
            cy = int(560 * u)
        ch = cw * 9 // 16
        cards = self._cache.get("cards")
        if cards is None:
            d = self.data
            cards = [make_card(d, d["mid"], 1, (cw, ch)), make_card(d, d["whale"], 1, (cw, ch))]
            self._cache["cards"] = cards
        half = dur / 2
        k = 0 if t < half else 1
        lt = t - k * half
        eyebrow = "// STEP 2" if k == 0 else "// WHALE ALERT"
        title = "A live card for every trade" if k == 0 else "Whales get noticed"
        if self.vert:
            title = title.replace("for every", "for\nevery").replace("get", "get\n")
        self.caption(img, lt, eyebrow, title, 60 * u if not self.vert else 170 * u, LIME, .05,
                     56 if not self.vert else 70)
        c = cards[k].frame(min(lt, cards[k].seconds - .01)).convert("RGBA")
        a_in = ease(seg(lt, 0, .5))
        a_out = 1 - ease_io(seg(lt, half - .45, half - .05)) if k == 0 else 1.0
        dx = (1 - a_in) * 90 * u - (1 - a_out) * 90 * u
        x = (W - cw) // 2 + dx
        sh, pad = shadow((cw, ch), int(20 * u), int(36 * u), 190)
        layer = Image.new("RGBA", sh.size, (0, 0, 0, 0))
        layer.putalpha(sh)
        paste_alpha(img, layer, (x - pad, cy - pad + 16 * u), a_in * a_out)
        c.putalpha(rounded((cw, ch), int(20 * u)))
        paste_alpha(img, c, (x, cy), a_in * a_out)
        if self.vert:  # the post under the card, as it lands in the chat
            p = self.data["mid" if k == 0 else "whale"]
            msg = draw_message(post_text(p), int(940 * u), 30 * u, lt, sender="Silverhand Buys", time_s="12:04")
            a = ease(seg(lt, .9, 1.4)) * a_out
            paste_alpha(img, msg, ((W - msg.width) // 2, cy + ch + 60 * u + (1 - a) * 30 * u), a)
        self.tapes(img, t, LIME)
        return img

    # -- 5. the feed: buys, a sell, a dev buy
    def s_feed(self, t, dur):
        u, W = self.u, self.W
        img = self.bg(GREEN, .8)
        d = self.data
        self.caption(img, t, "// IN YOUR CHAT", "Buys. Sells. Whales. Dev buys." if not self.vert else
                     "Buys. Sells.\nWhales. Dev buys.", 60 * u if not self.vert else 150 * u, GREEN, .05,
                     56 if not self.vert else 70)
        width = int(1000 * u) if not self.vert else int(960 * u)
        height = int(800 * u) if not self.vert else int(1380 * u)
        px = 26 * u if not self.vert else 30 * u
        posts = [(d["first"], False, "12:01"), (d["late"], False, "12:07"), (d["sell"], True, "12:09"),
                 (d["whale"], False, "12:12")]
        msgs = [(.35 + i * 1.15, draw_message(post_text(p, s), int(width * .9), px, t, sender="Silverhand Buys",
                                              time_s=ts), "left") for i, (p, s, ts) in enumerate(posts)]
        win = self.chat_window(t, msgs, "Your community", "vibe/vibe launch · 1,204 members", width, height)
        x = (W - width) // 2
        y = int(190 * u) if not self.vert else int(440 * u)
        a = ease(seg(t, 0, .45))
        self.window_frame(img, win, x, y + (1 - a) * 40 * u, a)
        self.tapes(img, t)
        return img

    # -- 6. graduation
    def s_grad(self, t, dur):
        u, W = self.u, self.W
        img = self.bg(LIME, 1.0)
        d = ImageDraw.Draw(img)
        g = self.data["grad"]
        tok = self.data["token"]
        p = ease_io(seg(t, .25, 2.4))
        done = t >= 2.45
        self.caption(img, t, "// VIBE/VIBE CURVE", f"${tok.symbol} fills its curve", 150 * u if not self.vert else 420 * u,
                     LIME, .05, 64 if not self.vert else 72)
        bw = int(1300 * u) if not self.vert else int(900 * u)
        bh = int(46 * u)
        bx = (W - bw) // 2
        by = int(470 * u) if not self.vert else int(820 * u)
        d.rounded_rectangle((bx, by, bx + bw, by + bh), bh // 2, fill=(30, 34, 38))
        fillw = int(bw * p)
        if fillw > bh:
            grad = Image.new("RGBA", (fillw, bh))
            gd = ImageDraw.Draw(grad)
            for x in range(fillw):
                gd.line((x, 0, x, bh), fill=(*mix(LIME, GREEN, .55 + .45 * x / max(1, bw)), 255))
            grad.putalpha(rounded((fillw, bh), bh // 2))
            img.alpha_composite(grad, (bx, by))
        pct = f"{p * 100:.0f}%"
        fp = font("num", 64 * u)
        d.text(((W - text_w(d, pct, fp)) / 2, by + bh + 26 * u), pct, font=fp, fill=LIME if done else INK)
        if done:
            k = ease(seg(t, 2.45, 2.85))
            flash = 1 - seg(t, 2.45, 2.9)
            if flash > 0:
                img = Image.blend(img.convert("RGB"), Image.new("RGB", img.size, LIME), .35 * flash).convert("RGBA")
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            stamp = "GRADUATED  →  UNISWAP V4"
            fs = font("hero", (78 if not self.vert else 66) * u * (1.25 - .25 * k))
            sw = text_w(ld, stamp, fs)
            sy = by + bh + 140 * u
            ld.text(((W - sw) / 2, sy), stamp, font=fs, fill=(*LIME, 255))
            line = f"{g['raised']:g} {g['quote_symbol']} raised · the bot follows it into the pool"
            fl = font("uir", (30 if not self.vert else 32) * u)
            if self.vert:
                line = line.replace(" · ", "\n")
            yy = sy + 110 * u
            for part in line.split("\n"):
                ld.text(((W - text_w(ld, part, fl)) / 2, yy), part, font=fl, fill=(*INK, 255))
                yy += 44 * u
            paste_alpha(img, layer, (0, 0), k)
        # the rocket rides the head of the bar, and grows when the curve is full
        bpx = int((96 + 40 * ease(seg(t, 2.45, 2.85))) * u)
        hx = bx + max(fillw, bh) - bpx / 2
        bob = math.sin(t * 9) * 3 * u * (1 - p)
        paste_alpha(img, BADGES.get("rocket", t, bpx), (hx, by - bpx - 14 * u + bob), ease(seg(t, .15, .45)))
        self.tapes(img, t, LIME)
        return img

    # -- 7. outro
    def s_outro(self, t, dur):
        u, W, H = self.u, self.W, self.H
        key = "outro_scene"
        if key not in self._cache:
            base_w, base_h = 640, 360  # the scene's own 16:9 layout; a band across the vertical frame
            sc = make_intro.Scene(base_w, base_h, 3, "SILVERHAND BUYS", "// LIVE ON ROBINHOOD CHAIN TESTNET",
                                  BOT.upper(), str(ROOT / "assets" / "brand" / "avatar.png"), seed=11)
            sc.W, sc.H = sc.w, sc.h
            self._cache[key] = sc
        img = self._cache[key].frame((t / 3.0) % 1.0)
        img = self.letterbox(img, t)
        img = img.convert("RGBA")
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        a = ease(seg(t, .6, 1.2))
        f = font("ui", 26 * u)
        lines = ["Built for vibe/vibe launches · try it on testnet now",
                 "Not affiliated with Robinhood · NFA · DYOR"]
        if self.vert:
            lines = ["Built for vibe/vibe launches", "Try it on testnet now", "Not affiliated with Robinhood · NFA · DYOR"]
        yy = H - (150 if not self.vert else 260) * u
        for i, ln in enumerate(lines):
            ff = f if i < len(lines) - 1 else font("uir", 22 * u)
            col = LIME if i == 0 else (INK if i < len(lines) - 1 else DIM)
            ld.text(((W - text_w(ld, ln, ff)) / 2, yy), ln, font=ff, fill=(*col, 255))
            yy += 40 * u
        paste_alpha(img, layer, (0, 0), a)
        return img


# ----------------------------------------------------------------------------- sound
SR = promo_music.SR


def _db(x):
    return 10 ** (x / 20)


GRAD_HIT = 2.45     # seconds into the graduation scene: the curve is full, the stamp lands
MUSIC_DB = -30.0   # the music bed's loudness (RMS, dBFS): barely there under the picture
SFX_DB = -20.0     # the effects' peaks (dBFS): soft accents, never louder than a notification


def synth(events, length, style="simple", scenes=(), grad_at=0.0, sfx_style="soft"):
    """The music bed and the effects, each at its own fixed level (no normalising to the
    loudest moment, which pulled everything up). sfx_style "notify" turns the posts' pops
    into a messenger's two-note notification."""
    if sfx_style != "soft":  # the posts' sound: notify, tg-pop, tg-chime or glass
        events = [(at, sfx_style if kind == "pop" else kind, p) for at, kind, p in events]
    n = int(SR * (length + 1))
    sfx = np.zeros((n, 2), np.float32)
    rnd = np.random.default_rng(5)

    def add(at, sig, pan=0.0):
        i = int(at * SR)
        if i >= n or i + len(sig) <= 0:
            return
        sig = sig[: n - i]
        sfx[i:i + len(sig), 0] += sig * (1 - pan) / 2 * 1.41
        sfx[i:i + len(sig), 1] += sig * (1 + pan) / 2 * 1.41

    def env(nn, a, dcy):
        t = np.arange(nn) / SR
        return np.minimum(1, t / max(a, 1e-4)) * np.exp(-t / dcy)

    for at, kind, p in events:
        g = p.get("gain", 1.0)
        pitch = p.get("pitch", 1.0)
        if kind == "tick":  # a soft wooden tick
            nn = int(.05 * SR)
            t = np.arange(nn) / SR
            sig = np.sin(2 * np.pi * 1100 * pitch * t) * env(nn, .002, .012)
            add(at, (sig * .12 * g).astype(np.float32), rnd.uniform(-.3, .3))
        elif kind == "key":
            nn = int(.05 * SR)
            t = np.arange(nn) / SR
            sig = np.sin(2 * np.pi * 900 * t) * env(nn, .002, .01)
            add(at, (sig * .1 * g).astype(np.float32), rnd.uniform(-.2, .2))
        elif kind == "pop":  # a round bubble
            nn = int(.16 * SR)
            t = np.arange(nn) / SR
            f = 520 * pitch * (1 + .5 * np.exp(-t / .03))
            sig = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(nn, .006, .05)
            add(at, (sig * .5 * g).astype(np.float32), rnd.uniform(-.3, .3))
        elif kind == "tg-pop":  # a message landing in an open chat: a soft bubble and a tiny click
            nn = int(.22 * SR)
            t = np.arange(nn) / SR
            f = 740 * (1 + .9 * np.exp(-t / .012))
            sig = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.minimum(1, t / .002) * np.exp(-t / .035)
            sig += .35 * np.sin(2 * np.pi * 2960 * t) * np.minimum(1, t / .0005) * np.exp(-t / .006)
            add(at, (sig * .55 * g).astype(np.float32), .05)
        elif kind == "tg-chime":  # three quick rising notes, bright and short
            nn = int(.55 * SR)
            t = np.arange(nn) / SR
            sig = np.zeros(nn)
            for i, f0 in enumerate((1760.0, 2217.5, 2637.0)):
                start = int(i * .055 * SR)
                tt = t[: nn - start]
                sig[start:] += (np.sin(2 * np.pi * f0 * tt) * np.minimum(1, tt / .002) * np.exp(-tt / (.07 + .05 * i))
                                * (.7 + .15 * i))
            add(at, (sig * .3 * g).astype(np.float32), .1)
        elif kind == "glass":  # one glassy ping: a bell partial set with a short tail
            nn = int(.9 * SR)
            t = np.arange(nn) / SR
            f0 = 1567.98
            sig = np.zeros(nn)
            for ratio, amp, dcy in ((1, 1, .3), (2.76, .35, .12), (5.4, .15, .05)):
                sig += amp * np.sin(2 * np.pi * f0 * ratio * t) * np.exp(-t / dcy)
            sig *= np.minimum(1, t / .001)
            add(at, (sig * .32 * g).astype(np.float32), -.05)
        elif kind == "chart":  # the chart drawing itself: rising data blips, then a soft landing chord
            dur = p.get("dur", 1.6)
            nn = int((dur + 1.4) * SR)
            t = np.arange(nn) / SR
            sig = np.zeros(nn)
            steps = 14
            scale = [0, 2, 4, 7, 9, 12, 14, 16, 19, 21, 24, 26, 28, 31]  # pentatonic, rising
            for i in range(steps):
                start = int(dur * (i / steps) ** 1.15 * SR)
                f0 = 440 * 2 ** (scale[i] / 12)
                tt = t[: nn - start]
                sig[start:] += (np.sin(2 * np.pi * f0 * tt) * np.minimum(1, tt / .002) * np.exp(-tt / .05)
                                * (.35 + .4 * i / steps))
            land = int(dur * SR)
            tt = t[: nn - land]
            for f0 in (880.0, 1108.7, 1318.5):  # A major, soft
                sig[land:] += .3 * np.sin(2 * np.pi * f0 * tt) * np.minimum(1, tt / .01) * np.exp(-tt / .5)
            noise = np.convolve(rnd.normal(0, 1, nn), np.ones(40) / 40, "same")
            sig += noise * np.clip(t / dur, 0, 1) ** 2 * np.exp(-np.maximum(0, t - dur) / .1) * .5
            add(at, (sig * .4 * g).astype(np.float32))
        elif kind == "notify":  # a messenger's notification: two quick soft notes, a fifth apart
            nn = int(.45 * SR)
            t = np.arange(nn) / SR
            sig = np.zeros(nn)
            for i, f0 in enumerate((987.8, 1480.0)):
                start = int(i * .085 * SR)
                tt = t[: nn - start]
                tone = np.sin(2 * np.pi * f0 * tt) + .3 * np.sin(2 * np.pi * 2 * f0 * tt) * np.exp(-tt / .04)
                sig[start:] += tone * np.minimum(1, tt / .003) * np.exp(-tt / .11)
            add(at, (sig * .35 * g).astype(np.float32), .1)
        elif kind == "coin":  # two soft bell notes
            nn = int(.9 * SR)
            t = np.arange(nn) / SR
            sig = np.zeros(nn)
            for i, f0 in enumerate((1318.5 * pitch, 1760 * pitch)):
                start = int(i * .09 * SR)
                tt = t[: nn - start]
                sig[start:] += np.sin(2 * np.pi * f0 * tt) * np.minimum(1, tt / .004) * np.exp(-tt / .28)
            add(at, (sig * .32 * g).astype(np.float32))
        elif kind == "send":
            nn = int(.2 * SR)
            t = np.arange(nn) / SR
            f = 500 + 500 * t / t[-1]
            sig = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(nn, .01, .06)
            add(at, (sig * .35).astype(np.float32))
        elif kind == "whoosh":  # an airy swell, no hiss
            dur = p.get("dur", .5)
            nn = int(dur * SR)
            t = np.arange(nn) / SR
            noise = np.convolve(rnd.normal(0, 1, nn), np.ones(60) / 60, "same")
            sig = noise * np.sin(np.pi * t / dur) ** 3
            add(at, (sig * .6 * g).astype(np.float32))
        elif kind == "riser":  # a gentle rising tone
            dur = p["dur"]
            nn = int(dur * SR)
            t = np.arange(nn) / SR
            f = 220 * (2 ** (2 * t / dur))
            sig = np.sin(2 * np.pi * np.cumsum(f) / SR) * (t / dur) ** 2
            add(at, (sig * .25 * g).astype(np.float32))
        elif kind == "impact":  # a soft low bloom instead of a hit
            nn = int(1.6 * SR)
            t = np.arange(nn) / SR
            sig = np.sin(2 * np.pi * 73.4 * t) * np.minimum(1, t / .02) * np.exp(-t / .5)
            sig += .4 * np.sin(2 * np.pi * 146.8 * t) * np.minimum(1, t / .02) * np.exp(-t / .35)
            add(at, (sig * .5 * g).astype(np.float32))
        elif kind == "chord":  # the closing chord: A major, soft and slow
            dur = p["dur"]
            nn = int(dur * SR)
            t = np.arange(nn) / SR
            sig = np.zeros(nn)
            for i, f0 in enumerate((220, 277.2, 329.6, 440)):
                sig += np.sin(2 * np.pi * f0 * t + i)
            sig *= np.minimum(1, t / .4) * np.exp(-t / 1.8) / 4
            add(at, (sig * .5 * g).astype(np.float32))
    peak = float(np.max(np.abs(sfx))) or 1.0
    sfx *= _db(SFX_DB) / peak
    out = sfx + promo_music.render(style, length, scenes, grad_at, MUSIC_DB)[: len(sfx)]
    return np.clip(out, -.98, .98)


def write_wav(path, audio):
    data = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


# ----------------------------------------------------------------------------- render
def render_video(fmt, preview, data, out_dir, stills=None, audio_only=False, style="", variant="classic",
                 sfx_style="soft"):
    W, H = (1920, 1080) if fmt == "16x9" else (1080, 1920)
    fps = 30
    if preview:
        W, H, fps = W // 2, H // 2, 15
    import promo_variants
    promo = promo_variants.VARIANTS[variant](W, H, fps, data)
    style = style or promo.MUSIC
    soundtrack = lambda: synth(promo.events, promo.length, style, promo.music_scenes(), promo.grad_time(),  # noqa: E731
                               sfx_style)
    suffix = (("" if variant == "classic" else f"-{variant}") + ("" if style == promo.MUSIC else f"-{style}")
              + ("" if sfx_style == "soft" else f"-{sfx_style}"))
    if audio_only:  # a new soundtrack under the video already made: seconds, not minutes
        src = out_dir / (f"promo-{fmt}" + ("" if variant == "classic" else f"-{variant}") + ".mp4")
        if not src.exists():
            sys.exit(f"{src} is not made yet")
        mp4 = out_dir / f"promo-{fmt}{suffix}.mp4"
        wav, tmp = out_dir / f"promo-{fmt}{suffix}.wav", out_dir / f"promo-{fmt}{suffix}.tmp.mp4"
        write_wav(wav, soundtrack())
        subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", str(src), "-i", str(wav), "-map", "0:v", "-map", "1:a",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                        str(tmp)], check=True)
        tmp.replace(mp4)
        wav.unlink()
        print(f"{mp4}: new soundtrack")
        return
    if stills:
        for s in stills:
            p = out_dir / f"promo-{fmt}{suffix}-{s:05.1f}s.png"
            promo.frame(s).save(p)
            print(p)
        return
    n = int(round(promo.length * fps))
    wav = out_dir / f"promo-{fmt}{suffix}.wav"
    write_wav(wav, soundtrack())
    mp4 = out_dir / (f"promo-{fmt}{suffix}" + ("-preview" if preview else "") + ".mp4")
    cmd = [ffmpeg(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(fps), "-i", "-", "-i", str(wav), "-map", "0:v", "-map", "1:a", "-shortest",
           "-c:v", "libx264", "-preset", "medium" if not preview else "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(mp4)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    t0 = time.time()
    for i in range(n):
        proc.stdin.write(promo.frame(i / fps).tobytes())
        if i % (fps * 2) == 0:
            print(f"  {fmt}: {i / fps:5.1f}/{promo.length:.1f} s ({time.time() - t0:.0f} s)", flush=True)
    proc.stdin.close()
    if proc.wait() != 0:
        sys.exit("ffmpeg failed")
    wav.unlink()
    print(f"{mp4} {mp4.stat().st_size / 1e6:.1f} MB, {promo.length:.1f} s")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--format", choices=["16x9", "9x16", "both"], default="both")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--stills", default="", help="comma-separated seconds: PNG frames only")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--audio-only", action="store_true", help="only a new soundtrack for the videos made")
    ap.add_argument("--music", default="", help="music style (default: the variant's own), or several: "
                    + ", ".join(promo_music.STYLES))
    ap.add_argument("--variant", default="classic", help="classic, terminal, hype, phone (or several)")
    ap.add_argument("--sfx", default="soft", choices=["soft", "notify", "tg-pop", "tg-chime", "glass"],
                    help="the posts' sound: a soft pop, or a messenger-like notification")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    i18n.LANGS = ("en",)
    data = build_data()
    stills = [float(x) for x in a.stills.split(",") if x.strip()] or None
    for fmt in (["16x9", "9x16"] if a.format == "both" else [a.format]):
        for variant in a.variant.split(","):
            for style in a.music.split(","):
                render_video(fmt, a.preview, data, out, stills, a.audio_only, style.strip(), variant.strip(), a.sfx)


if __name__ == "__main__":
    main()
