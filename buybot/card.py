"""The animated card posted with each trade: a trading-desk dashboard.

render_card(spec) -> MP4 bytes, render_still(spec) -> PNG bytes.

Layout (1280x720 design units, everything scales with the output size):
  header      token monogram, $SYMBOL + name, network and venue chips, the bot's brand
  progress    a thin bar to graduation when the token is on a bonding curve
  chart card  MARKET CAP hero + change chip, candles with volume, a dashed graduation
              line, the last-price line and the trade's candle marked with a ping
  side panel  BUY/SELL pill and badges, the trade amount (the hero number), quote and
              tokens, trader + holder status, stat tiles, 24h buy/sell pressure bars

Motion (60 fps by default): the card's chrome is there from frame 0; the numbers count
up with an ease-out, the candles draw in left to right behind a soft wipe with a light
at its tip, bars fill, the trade candle pings, a silver glint sweeps over the card once,
then a calm idle (ping, shimmer on the progress bar) and a short dissolve back to frame 0
so Telegram's loop restarts cleanly.

Cost: everything still is drawn once at 2x and scaled down (anti-aliased). Each frame is
a copy of a pre-rendered image plus a few pastes of small cached sprites and masks; after
the intro the frame starts from the fully drawn card. Pillow only, ffmpeg (libx264).
"""

import io
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# ----------------------------------------------------------------------------- fonts
# Bundled open fonts (assets/fonts, SIL OFL): Instrument Sans for words, JetBrains Mono
# for numbers; DejaVu (Linux) as the fallback. Each role: candidate files, variable axes.
_ROOT = Path(__file__).resolve().parent.parent
_FONTS = _ROOT / "assets" / "fonts"
_SANS, _MONO = _FONTS / "InstrumentSans.ttf", _FONTS / "JetBrainsMono.ttf"
_DJ = Path("/usr/share/fonts/truetype/dejavu")
FONTS = {
    "hero":  [(_SANS, {"Weight": 700, "Width": 100}), (_DJ / "DejaVuSans-Bold.ttf", {})],
    "title": [(_SANS, {"Weight": 700, "Width": 100}), (_DJ / "DejaVuSans-Bold.ttf", {})],
    "ui":    [(_SANS, {"Weight": 600, "Width": 100}), (_DJ / "DejaVuSans-Bold.ttf", {})],
    "uir":   [(_SANS, {"Weight": 400, "Width": 100}), (_DJ / "DejaVuSans.ttf", {})],
    "cond":  [(_SANS, {"Weight": 700, "Width": 75}), (_DJ / "DejaVuSansCondensed-Bold.ttf", {})],
    "num":   [(_MONO, {"Weight": 700}), (_DJ / "DejaVuSansMono-Bold.ttf", {})],
    "mono":  [(_MONO, {"Weight": 400}), (_DJ / "DejaVuSansMono.ttf", {})],
}
_font_cache: dict = {}


def font(role: str, px: float) -> ImageFont.FreeTypeFont:
    px = max(6, int(round(px)))
    key = (role, px)
    if key in _font_cache:
        return _font_cache[key]
    f = None
    for path, axes in FONTS[role]:
        if Path(path).exists():
            f = ImageFont.truetype(str(path), px)
            if axes:
                try:
                    vals = []
                    for a in f.get_variation_axes():
                        name = a["name"].decode() if isinstance(a["name"], bytes) else a["name"]
                        vals.append(axes.get(name, a["default"]))
                    f.set_variation_by_axes(vals)
                except Exception:
                    pass
            break
    if f is None:
        f = ImageFont.load_default(px)
    _font_cache[key] = f
    return f


# ----------------------------------------------------------------------------- themes
THEMES = {
    "silverhand": {
        "page": (6, 8, 8), "card": (12, 15, 15), "tile": (16, 20, 20), "line": (29, 35, 34),
        "line2": (42, 50, 48), "text": (238, 243, 241), "dim": (140, 152, 147), "faint": (82, 93, 89),
        "accent": (45, 245, 154), "buy": (45, 245, 154), "sell": (255, 77, 109), "grad": (242, 201, 76),
        "warn": (255, 176, 59), "ink": (3, 18, 11), "glint": (225, 240, 235),
    },
    "vibe": {
        "page": (12, 14, 16), "card": (16, 18, 20), "tile": (20, 23, 26), "line": (34, 38, 42),
        "line2": (48, 53, 58), "text": (241, 243, 244), "dim": (139, 145, 150), "faint": (86, 92, 97),
        "accent": (223, 249, 2), "buy": (74, 222, 128), "sell": (224, 102, 75), "grad": (223, 249, 2),
        "warn": (255, 176, 59), "ink": (14, 16, 4), "glint": (240, 244, 230),
    },
}
BRAND = "SILVERHAND BUYS"
SLOGAN = "Not paper hands. Silver hands."
AVATAR = _ROOT / "assets" / "brand" / "avatar.png"

DW, DH = 1280, 720  # design units


def _mix(a, b, k):
    """k of a, 1-k of b."""
    return tuple(int(round(x * k + y * (1 - k))) for x, y in zip(a, b))


# ----------------------------------------------------------------------------- easing
def _clamp(x, a=0.0, b=1.0):
    return a if x < a else b if x > b else x


def _seg(t, t0, t1):
    return _clamp((t - t0) / (t1 - t0)) if t1 > t0 else float(t >= t1)


def ease_out_cubic(x):
    return 1 - (1 - x) ** 3


def ease_out_expo(x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    return (1 - 2 ** (-10 * x)) / (1 - 2 ** -10)


def ease_out_quart(x):
    return 1 - (1 - x) ** 4


def ease_in_out_cubic(x):
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def ease_in_out_sine(x):
    return -(math.cos(math.pi * x) - 1) / 2


# ----------------------------------------------------------------------------- numbers
def _grp(x: float, dec: int = 0) -> str:
    return f"{x:,.{dec}f}"


def _subscript_price(x: float):
    """Rich text for tiny prices, DEX style: 0.0₄2917 -> [('0.0',0), ('4',1), ('2917',0)]."""
    if x <= 0:
        return [("0", 0)]
    if x >= 1:
        return [(f"{x:,.4g}" if x < 1000 else _grp(x, 2), 0)]
    if x >= 0.001:
        return [(f"{x:.{max(2, 3 - int(math.floor(math.log10(x))))}f}".rstrip("0"), 0)]
    zeros = -int(math.floor(math.log10(x))) - 1
    digits = f"{x * 10 ** (zeros + 4):.0f}"[:4].rstrip("0") or "0"
    return [("0.0", 0), (str(zeros), 1), (digits, 0)]


def compact(x: float) -> str:
    ax = abs(x)
    for lim, suf, dec in ((1e12, "T", 2), (1e9, "B", 2), (1e6, "M", 2), (1e3, "K", 1)):
        if ax >= lim * 0.9995:
            return f"{x / lim:.{dec}f}{suf}"
    if ax >= 100:
        return f"{x:.0f}"
    if ax >= 1:
        return f"{x:.2f}"
    if ax == 0:
        return "0"
    return f"{x:.{min(8, 2 - int(math.floor(math.log10(ax))))}f}"


def qty(x: float) -> str:
    """Quote amounts: 3.20, 0.0412, 1,204, 12.4K."""
    ax = abs(x)
    if ax >= 10000:
        return compact(x)
    if ax >= 100:
        return _grp(x, 0)
    if ax >= 1:
        return f"{x:.2f}"
    if ax == 0:
        return "0"
    return f"{x:.{min(8, 2 - int(math.floor(math.log10(ax))))}f}"


def money_formatter(final: float):
    """A formatter fixed by the final value, so a count-up keeps its unit and decimals."""
    ax = abs(final)
    for lim, suf, dec in ((1e12, "T", 2), (1e9, "B", 2), (1e6, "M", 2)):
        if ax >= lim * 0.9995:
            return lambda v, lim=lim, suf=suf, dec=dec: f"${v / lim:.{dec}f}{suf}"
    if ax >= 1000:
        return lambda v: f"${v:,.0f}"
    if ax >= 100:
        return lambda v: f"${v:.0f}"
    if ax >= 1:
        return lambda v: f"${v:.2f}"
    dec = min(6, 2 - int(math.floor(math.log10(ax)))) if ax > 0 else 2
    return lambda v, dec=dec: f"${v:.{dec}f}"


def compact_formatter(final: float, prefix="$", suffix=""):
    ax = abs(final)
    for lim, suf, dec in ((1e12, "T", 2), (1e9, "B", 2), (1e6, "M", 2), (1e3, "K", 1)):
        if ax >= lim * 0.9995:
            return lambda v, lim=lim, suf=suf, dec=dec: f"{prefix}{v / lim:.{dec}f}{suf}{suffix}"
    s = compact(final)
    dec = len(s.split(".")[1]) if "." in s else 0
    return lambda v, dec=dec: f"{prefix}{v:.{dec}f}{suffix}"


def int_formatter(final):
    return lambda v: _grp(round(v))


# ----------------------------------------------------------------------------- candles
INTERVALS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 14400]
MAX_CANDLES = 40


def build_candles(points, now, span):
    for iv in INTERVALS:
        if span / iv <= MAX_CANDLES:
            break
    n = int(math.ceil(span / iv))
    start0 = (now // iv - n + 1) * iv
    prev = None
    for p in points:
        if p[0] >= start0:
            break
        prev = p[1]
    inside = [p for p in points if p[0] >= start0]
    out, i = [], 0
    for k in range(n):
        s = start0 + k * iv
        e = s + iv
        chunk = []
        while i < len(inside) and inside[i][0] < e:
            chunk.append(inside[i])
            i += 1
        if not chunk:
            if prev is not None:
                out.append({"t": s, "o": prev, "h": prev, "l": prev, "c": prev, "v": 0.0, "n": 0, "bv": 0.0})
            else:
                out.append(None)
            continue
        pr = [p[1] for p in chunk]
        o = prev if prev is not None else pr[0]
        out.append({"t": s, "o": o, "h": max(o, *pr), "l": min(o, *pr), "c": pr[-1],
                    "v": sum(p[2] for p in chunk), "n": len(chunk),
                    "bv": sum(p[2] for p in chunk if p[3] > 0)})
        prev = pr[-1]
    return out, iv


def nice_ticks(lo, hi, n=4):
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if span / step <= n + 0.5:
            break
    v = math.ceil(lo / step) * step
    out = []
    while v <= hi + 1e-12:
        out.append(v)
        v += step
    return out


def _span_label(sec):
    if sec >= 3600 * 0.95:
        return f"{round(sec / 3600)}H"
    return f"{max(1, round(sec / 60))}M"


def _iv_label(iv):
    return f"{iv // 3600}H" if iv >= 3600 else f"{iv // 60}M"


# ----------------------------------------------------------------------------- drawing helpers
class _Ctx:
    """A drawing surface in design units: `s` pixels per unit (2x canvases for still layers)."""

    def __init__(self, img, s):
        self.img, self.s = img, s
        self.d = ImageDraw.Draw(img, "RGBA") if img.mode == "RGB" else ImageDraw.Draw(img)

    def p(self, v):
        return v * self.s

    def box(self, x0, y0, x1, y1):
        s = self.s
        return [x0 * s, y0 * s, x1 * s - 1, y1 * s - 1]

    def rrect(self, x0, y0, x1, y1, r, fill=None, outline=None, width=1):
        s = self.s
        self.d.rounded_rectangle([x0 * s, y0 * s, x1 * s - 1, y1 * s - 1], radius=r * s, fill=fill,
                                 outline=outline, width=max(1, int(round(width * s))) if outline else 0)

    def line(self, pts, fill, width=1):
        self.d.line([(x * self.s, y * self.s) for x, y in pts], fill=fill, width=max(1, int(round(width * self.s))))

    def text(self, x, y, txt, role, size, fill, anchor="ls", tracking=0.0):
        f = font(role, size * self.s)
        if not tracking:
            self.d.text((x * self.s, y * self.s), txt, font=f, fill=fill, anchor=anchor)
            return x + f.getlength(txt) / self.s
        w = _tracked_width(txt, f, tracking * self.s) / self.s
        if anchor[0] == "r":
            x -= w
        elif anchor[0] == "m":
            x -= w / 2
        a = "l" + anchor[1]
        xx = x * self.s
        for ch in txt:
            self.d.text((xx, y * self.s), ch, font=f, fill=fill, anchor=a)
            xx += f.getlength(ch) + tracking * self.s
        return x + w

    def width(self, txt, role, size, tracking=0.0):
        f = font(role, size * self.s)
        return _tracked_width(txt, f, tracking * self.s) / self.s


def _tracked_width(txt, f, tr):
    if not tr:
        return f.getlength(txt)
    return sum(f.getlength(ch) for ch in txt) + tr * max(0, len(txt) - 1)


def _text_mask(txt, f, tabular=False, tracking=0.0, rich=None):
    """An L mask of the text and the baseline/left offsets inside it.
    tabular: digits get the same advance (count-ups do not wobble). rich: [(text, sub)]."""
    segs = rich if rich is not None else [(txt, 0)]
    asc, desc = f.getmetrics()
    pad = int(f.size * 0.12) + 2
    subf = font_like(f, 0.62)
    dig = max(f.getlength(c) for c in "0123456789") if tabular else 0
    # measure
    x = 0.0
    items = []
    for s, sub in segs:
        ff = subf if sub else f
        for ch in s:
            adv = dig if (tabular and ch.isdigit()) else ff.getlength(ch)
            items.append((x, ch, ff, sub, adv))
            x += adv + tracking
    w = int(math.ceil(x)) + 2 * pad
    h = asc + desc + 2 * pad
    m = Image.new("L", (max(1, w), h), 0)
    d = ImageDraw.Draw(m)
    base = pad + asc
    for x0, ch, ff, sub, adv in items:
        cw = ff.getlength(ch)
        xx = pad + x0 + ((adv - cw) / 2 if (tabular and ch.isdigit()) else 0)
        yy = base + (int(f.size * 0.10) if sub else 0)
        d.text((xx, yy), ch, font=ff, fill=255, anchor="ls")
    return m, pad, base, x - tracking if items else 0.0


_like_cache: dict = {}


def font_like(f, scale):
    key = (f.path, f.size, scale, getattr(f, "_role", None))
    if key not in _like_cache:
        g = ImageFont.truetype(f.path, max(6, int(f.size * scale)))
        try:
            g.set_variation_by_axes([a["default"] for a in g.get_variation_axes()])
        except Exception:
            pass
        _like_cache[key] = g
    return _like_cache[key]


def _round_mask(w, h, r, ss=4):
    m = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=r * ss, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def _hgrad(w, h, c0, c1):
    g = Image.new("RGB", (max(2, w), 1))
    px = g.load()
    for x in range(g.width):
        px[x, 0] = _mix(c1, c0, x / (g.width - 1))
    return g.resize((max(1, w), max(1, h)), Image.BILINEAR)


def _vgrad(w, h, c0, c1):
    g = Image.new("RGB", (1, max(2, h)))
    px = g.load()
    for y in range(g.height):
        px[0, y] = _mix(c1, c0, y / (g.height - 1))
    return g.resize((max(1, w), max(1, h)), Image.BILINEAR)


def _scaled(mask, a):
    if a >= 0.999:
        return mask
    a = max(0.0, a)
    return mask.point([int(v * a + 0.5) for v in range(256)])


# ----------------------------------------------------------------------------- the card
class Card:
    INTRO = 1.75      # by then every number has landed
    SWEEP = (1.6, 2.6)
    OUTRO = 0.40      # dissolve back to frame 0 at the end of the loop

    def __init__(self, spec: dict, size=(1280, 720), seconds=4.8, fps=60):
        self.spec = spec
        self.W, self.H = size
        self.k = self.W / DW
        self.fps = fps
        self.seconds = seconds
        self.pal = THEMES.get(spec.get("theme") or "silverhand", THEMES["silverhand"])
        self.side = -1 if (spec.get("side") or 1) < 0 else 1
        pal = self.pal
        self.sc = pal["buy"] if self.side > 0 else pal["sell"]
        self._tm: dict = {}
        self.dyn = []       # dynamic items: callables (frame, t) drawn during the intro
        self._prepare_data()
        self._layout()
        self._draw_still_layers()
        self._prepare_motion()

    # ------------------------------------------------------------------ data
    def _prepare_data(self):
        sp = self.spec
        now = int(sp.get("now") or time.time())
        pts = sorted(tuple(p) for p in (sp.get("points") or []) if p[1] and p[1] > 0 and p[0] <= now)
        self.upq = sp.get("usd_per_quote")
        self.usd_axis = self.upq is not None
        supply = sp.get("supply") or 0
        self.scale = supply * (self.upq or 1.0) if supply else None
        self.candles, self.iv, self.span = None, None, None
        if len(pts) >= 2 and self.scale:
            span = max(1800, min(6 * 3600, now - pts[0][0]))
            cs, iv = build_candles(pts, now, span)
            if sum(1 for c in cs if c) >= 2:
                self.candles, self.iv, self.span = cs, iv, span
        self.pts = pts
        self.now = now

    def _val(self, price):
        return price * self.scale

    # ------------------------------------------------------------------ layout
    def _layout(self):
        sp = self.spec
        self.has_prog = sp.get("progress") is not None
        top = 140 if self.has_prog else 104
        self.top = top
        self.chart_box = (24, top, 812, 696)
        self.panel_box = (828, top, 1256, 696)
        cx0, cy0, cx1, cy1 = self.chart_box
        self.plot = (cx0 + 22, cy0 + 112, cx1 - 92, cy1 - 40)

    # ------------------------------------------------------------------ still layers (2x)
    def _draw_still_layers(self):
        S = 2 * self.k
        W2, H2 = int(round(self.W * 2)), int(round(self.H * 2))
        pal = self.pal
        img = Image.new("RGB", (W2, H2), pal["page"])
        # ambient light: a wide soft glow of the trade's color from the upper right
        glow = Image.new("L", (64, 36), 0)
        ImageDraw.Draw(glow).ellipse((34, -26, 86, 18), fill=255)
        # the 2x layers are ~6 MB each: every one is dropped as soon as it is used, as the
        # card is drawn next to a ~180 MB bot under a 300 MB cap
        glow = glow.filter(ImageFilter.GaussianBlur(9)).resize((W2, H2), Image.BICUBIC)
        img.paste(self.sc, (0, 0), _scaled(glow, 0.07))
        glow = Image.new("L", (64, 36), 0)
        ImageDraw.Draw(glow).ellipse((-26, 18, 26, 60), fill=255)
        glow = glow.filter(ImageFilter.GaussianBlur(10)).resize((W2, H2), Image.BICUBIC)
        img.paste(pal["accent"], (0, 0), _scaled(glow, 0.035))
        del glow
        c = _Ctx(img, S)
        self._header(c)
        if self.has_prog:
            self._progress_track(c)
        self._cards(c)
        self._chart_chrome(c)
        self._panel_chrome(c)
        # text is measured later at the same scale; the 2x canvas itself is not kept for it
        self.c2 = _Ctx(Image.new("L", (1, 1)), S)
        del c
        # one 2x canvas: the chrome alone is the base, then the chart goes on the same canvas
        base = img.resize((self.W, self.H), Image.LANCZOS)
        self._chart_content(_Ctx(img, S))
        full = img.resize((self.W, self.H), Image.LANCZOS)
        del img
        # dithering noise against banding of the soft glows after yuv420p (one grain per
        # pixel of the final size)
        noise = Image.effect_noise((self.W, self.H), 18).point(lambda v: int(max(0, v - 128) * 0.035))
        noise = Image.merge("RGB", (noise, noise, noise))
        self.base = ImageChops.add(base, noise)
        self.full = ImageChops.add(full, noise)

    def _header(self, c: _Ctx):
        pal, sp = self.pal, self.spec
        # monogram tile: no token images here, so a tile with the initial in the accent
        x, y, sz = 24, 20, 64
        c.rrect(x, y, x + sz, y + sz, 14, fill=_mix(pal["accent"], pal["card"], 0.10),
                outline=_mix(pal["accent"], pal["card"], 0.42), width=1.5)
        sym = (sp.get("symbol") or "?").strip()
        ini = (sym.lstrip("$")[:1] or "?").upper()
        c.text(x + sz / 2, y + sz / 2 + 2, ini, "title", 34, pal["accent"], anchor="mm")
        # brand on the right
        bx1 = 1256
        av = 48
        bw = max(c.width(BRAND, "ui", 17, 2.2), c.width(SLOGAN, "uir", 15))
        tx = bx1 - bw
        c.text(tx, 46, BRAND, "ui", 17, pal["text"], tracking=2.2)
        c.text(tx, 70, SLOGAN, "uir", 15, pal["dim"])
        ax = tx - 14 - av
        self._avatar(c, ax, 20 + (64 - av) / 2, av)
        right_limit = ax - 24
        # symbol, name, chips
        tx = x + sz + 18
        chips = [t for t in (sp.get("network"), sp.get("venue")) if t]
        chip_w = sum(c.width(t, "ui", 17) + 34 + (14 if i == 0 else 0) for i, t in enumerate(chips))
        chip_w += 10 * max(0, len(chips) - 1)
        room = right_limit - tx
        label = f"${sym.lstrip('$')}"
        name = (sp.get("name") or "").strip()
        if name.lower().lstrip("$") == sym.lower().lstrip("$"):
            name = ""
        # symbol: shrink, then condensed face, then ellipsis
        sym_room = max(160, room - (chip_w + 24 if chips else 0) - (120 if name else 0))
        role, size = "title", 40
        while c.width(label, role, size) > sym_room and size > 30:
            size -= 2
        if c.width(label, role, size) > sym_room:
            role, size = "cond", 42
            while c.width(label, role, size) > sym_room and size > 30:
                size -= 2
            while c.width(label + "…", role, size) > sym_room and len(label) > 4:
                label = label[:-1]
            if c.width(label, role, size) > sym_room:
                label += "…"
        end = c.text(tx, 66, label, role, size, pal["text"])
        if name:
            nroom = right_limit - (chip_w + 24 if chips else 0) - end - 14
            nm = name
            while nm and c.width(nm, "uir", 22) > nroom:
                nm = nm[:-1]
            if nm != name and nm:
                nm = nm[:-1].rstrip() + "…"
            if nm and len(nm) > 1:
                end = c.text(end + 14, 65, nm, "uir", 22, pal["dim"])
        # chips after the name
        cx = end + 22
        for i, t in enumerate(chips):
            w = c.width(t, "ui", 17) + 30 + (14 if i == 0 else 0)
            if cx + w > right_limit:
                break
            c.rrect(cx, 37, cx + w, 69, 8, fill=pal["tile"], outline=pal["line2"], width=1)
            tx2 = cx + 15
            if i == 0:
                c.d.ellipse([c.p(tx2), c.p(49), c.p(tx2 + 8) - 1, c.p(57) - 1], fill=pal["accent"])
                tx2 += 14
            c.text(tx2, 59, t, "ui", 17, pal["text"] if i == 0 else pal["dim"])
            cx += w + 10

    def _avatar(self, c: _Ctx, x, y, size):
        try:
            av = Image.open(AVATAR).convert("RGB")
        except Exception:
            return
        px = int(round(size * c.s))
        av = av.resize((px, px), Image.LANCZOS)
        m = Image.new("L", (px * 2, px * 2), 0)
        ImageDraw.Draw(m).ellipse([0, 0, px * 2 - 1, px * 2 - 1], fill=255)
        m = m.resize((px, px), Image.LANCZOS)
        c.img.paste(av, (int(round(x * c.s)), int(round(y * c.s))), m)
        c.d.ellipse([c.p(x - 2), c.p(y - 2), c.p(x + size + 2) - 1, c.p(y + size + 2) - 1],
                    outline=_mix(self.pal["accent"], self.pal["page"], 0.55), width=max(1, int(c.s * 1.5)))

    def _progress_track(self, c: _Ctx):
        pal = self.pal
        x0, x1, y = 24, 1000, 112
        self.prog_bar = (x0, y - 5, x1, y + 5)
        c.rrect(x0, y - 5, x1, y + 5, 5, fill=_mix(pal["line"], pal["page"], 0.9))
        c.text(1256, y + 8, "TO GRADUATION", "mono", 17, pal["dim"], anchor="rs", tracking=1.2)

    def _cards(self, c: _Ctx):
        pal = self.pal
        for b in (self.chart_box, self.panel_box):
            c.rrect(*b, 14, fill=pal["card"], outline=pal["line"], width=1)
            # a hairline highlight on the top edge, like light on a glass panel
            x0, y0, x1, _ = b
            g = _hgrad(int(c.p(x1 - x0 - 40)), max(1, int(c.s)), pal["card"], _mix(pal["text"], pal["card"], 0.16))
            gm = Image.new("L", g.size, 0)
            gm.paste(_hgrad(g.width // 2, 1, (0, 0, 0), (255, 255, 255)).convert("L"), (0, 0))
            gm.paste(_hgrad(g.width - g.width // 2, 1, (255, 255, 255), (0, 0, 0)).convert("L").resize(
                (g.width - g.width // 2, g.height)), (g.width // 2, 0))
            c.img.paste(_mix(pal["text"], pal["card"], 0.22), (int(c.p(x0 + 20)), int(c.p(y0))), _scaled(gm, 0.9))

    # ------------------------------------------------------------------ chart
    def _chart_geometry(self):
        if self.candles is None:
            return None
        if hasattr(self, "_geo"):
            return self._geo
        cs = self.candles
        vals = [c for c in cs if c]
        lo = min(self._val(c["l"]) for c in vals)
        hi = max(self._val(c["h"]) for c in vals)
        grad = self._grad_value()
        self.grad_in_range = False
        if grad is not None and grad > hi:
            if grad <= hi + 1.5 * max(hi - lo, hi * 0.04):
                hi = grad
                self.grad_in_range = True
        elif grad is not None and grad >= lo:
            self.grad_in_range = True
        if hi <= lo:
            hi, lo = hi * 1.02 + 1e-12, lo * 0.98
        pad = (hi - lo) * 0.10
        lo, hi = lo - pad * 0.6, hi + pad
        px0, py0, px1, py1 = self.plot
        vol_h = 64
        price_bot = py1 - vol_h - 12
        n = len(cs)
        slot = (px1 - px0 - 14) / n
        body = max(2.0, min(14.0, slot * 0.62))

        def y(v):
            return price_bot - (v - lo) / (hi - lo) * (price_bot - py0)

        def xc(i):
            return px0 + 4 + (i + 0.5) * slot
        self._geo = dict(lo=lo, hi=hi, y=y, xc=xc, slot=slot, body=body, vol_h=vol_h, price_bot=price_bot)
        return self._geo

    def _grad_value(self):
        sp = self.spec
        if sp.get("progress") is None:
            return None
        gq = sp.get("grad_mc_quote")
        if gq is None:
            return None
        return gq * (self.upq or 1.0)

    def _fmt_axis(self, v, tick=False):
        s = compact(v)
        if tick and "." in s:
            head, tail = s.split(".")
            suf = tail.lstrip("0123456789")
            if tail[:len(tail) - len(suf)].strip("0") == "":
                s = head + suf
        return ("$" + s) if self.usd_axis else s

    def _chart_chrome(self, c: _Ctx):
        pal = self.pal
        x0, y0, x1, y1 = self.chart_box
        px0, py0, px1, py1 = self.plot
        c.text(x0 + 24, y0 + 38, "MARKET CAP", "mono", 17, pal["dim"], tracking=1.6)
        right = x1 - 24
        if self.candles is not None:
            lbl = f"{_iv_label(self.iv)} CANDLES · {_span_label(self.span)}"
            c.text(right, y0 + 38, lbl, "mono", 17, pal["faint"], anchor="rs", tracking=1.2)
        geo = self._chart_geometry()
        if geo is None:
            c.text((px0 + px1) / 2, (py0 + py1) / 2, "NOT ENOUGH TRADES FOR A CHART YET", "mono", 18,
                   pal["faint"], anchor="mm", tracking=1.5)
            return
        y = geo["y"]
        ticks = nice_ticks(geo["lo"], geo["hi"], 5)
        last = [cc for cc in self.candles if cc][-1]
        last_y = y(self._val(last["c"]))
        grad = self._grad_value()
        grad_y = y(grad) if (grad is not None and self.grad_in_range) else None
        for v in ticks:
            yy = y(v)
            if yy < py0 - 2 or yy > geo["price_bot"] + 2:
                continue
            c.line([(px0, yy), (px1, yy)], fill=_mix(pal["line"], pal["card"], 0.75), width=1)
            if abs(yy - last_y) < 24 or (grad_y is not None and abs(yy - grad_y) < 24):
                continue
            c.text(x1 - 12, yy + 6, self._fmt_axis(v, tick=True), "mono", 17, pal["faint"], anchor="rs")
        # volume baseline and time labels
        c.line([(px0, py1), (px1, py1)], fill=pal["line"], width=1)
        n = len(self.candles)
        step = max(1, int(round(3600 / self.iv))) if self.span >= 3 * 3600 else max(1, n // 4)
        for i, cc in enumerate(self.candles):
            if cc is None:
                continue
            t = cc["t"]
            if self.span >= 3 * 3600:
                if t % 3600 != 0:
                    continue
            elif i % step:
                continue
            xx = geo["xc"](i) - geo["slot"] / 2
            if xx < px0 + 20 or xx > px1 - 40:
                continue
            c.line([(xx, py0), (xx, py1)], fill=_mix(pal["line"], pal["card"], 0.45), width=1)
            c.text(xx, py1 + 26, time.strftime("%H:%M", time.gmtime(t)), "mono", 17, pal["faint"], anchor="ms")
        c.text(x1 - 12, py1 + 26, "UTC", "mono", 17, pal["faint"], anchor="rs")
        # graduation line: dashed, with a chip and the value on the axis
        if grad_y is not None:
            gcol = pal["grad"]
            xx = px0
            while xx < px1:
                c.line([(xx, grad_y), (min(px1, xx + 7), grad_y)], fill=gcol, width=1.5)
                xx += 12
            tag = self._fmt_axis(grad)
            tw = c.width(tag, "num", 16) + 14
            c.rrect(x1 - 12 - tw, grad_y - 13, x1 - 6, grad_y + 13, 5, fill=gcol)
            c.text(x1 - 13, grad_y + 6, tag, "num", 16, pal["ink"], anchor="rs")
            lw = c.width("GRADUATION", "num", 15, 1.0) + 16
            c.rrect(px0 + 8, grad_y - 26, px0 + 8 + lw, grad_y - 4, 5, fill=_mix(gcol, pal["card"], 0.16))
            c.text(px0 + 16, grad_y - 9, "GRADUATION", "num", 15, gcol, tracking=1.0)
        elif grad is not None and grad > geo["hi"]:
            # off the scale: a dashed line along the top edge, labelled with the value and the
            # distance, so it never reads as a level on the axis
            gcol = pal["grad"]
            gy = py0 + 4
            xx = px0
            while xx < px1:
                c.line([(xx, gy), (min(px1, xx + 7), gy)], fill=_mix(gcol, pal["card"], 0.55), width=1.5)
                xx += 12
            cur = self._val(last["c"])
            dist = f"  +{(grad / cur - 1) * 100:.0f}%" if cur > 0 else ""
            label = f"GRADUATION {self._fmt_axis(grad)}{dist}"
            lw = c.width(label, "num", 15, 0.8) + 34
            c.rrect(px0 + 8, gy - 12, px0 + 8 + lw, gy + 12, 5, fill=_mix(gcol, pal["card"], 0.16))
            ax_, ay_ = px0 + 20, gy
            c.d.polygon([(c.p(ax_ - 5), c.p(ay_ + 3)), (c.p(ax_ + 5), c.p(ay_ + 3)), (c.p(ax_), c.p(ay_ - 5))], fill=gcol)
            c.text(px0 + 32, gy + 5, label, "num", 15, gcol, tracking=0.8)

    def _chart_content(self, c: _Ctx):
        """Candles, volume, last-price line and the trade's marker (revealed by the wipe)."""
        geo = self._chart_geometry()
        if geo is None:
            return
        pal = self.pal
        y, xc, body = geo["y"], geo["xc"], geo["body"]
        px0, py0, px1, py1 = self.plot
        cs = self.candles
        vmax = max((cc["v"] for cc in cs if cc), default=0) or 1.0
        up_c, dn_c = pal["buy"], pal["sell"]
        last_i = max(i for i, cc in enumerate(cs) if cc)
        # crosshair at the trade's candle
        tx = xc(last_i)
        yy = py0
        while yy < py1:
            c.line([(tx, yy), (tx, min(py1, yy + 3))], fill=_mix(self.sc, pal["card"], 0.40), width=1)
            yy += 7
        for i, cc in enumerate(cs):
            if cc is None:
                continue
            x = xc(i)
            up = cc["c"] >= cc["o"]
            col = up_c if up else dn_c
            if cc["n"]:
                vh = max(1.0, cc["v"] / vmax * geo["vol_h"])
                c.rrect(x - body / 2, py1 - vh, x + body / 2, py1, min(2, body / 4),
                        fill=_mix(col, pal["card"], 0.30 if i != last_i else 0.65))
                yh, yl = y(self._val(cc["h"])), y(self._val(cc["l"]))
                c.line([(x, yh), (x, yl)], fill=col, width=1.5)
                yo, ycl = y(self._val(cc["o"])), y(self._val(cc["c"]))
                t0, t1 = min(yo, ycl), max(yo, ycl)
                if t1 - t0 < 1.5:
                    t0, t1 = (t0 + t1) / 2 - 0.75, (t0 + t1) / 2 + 0.75
                c.rrect(x - body / 2, t0, x + body / 2, t1, min(1.5, body / 5), fill=col)
            else:
                yv = y(self._val(cc["c"]))
                c.line([(x - body / 2, yv), (x + body / 2, yv)], fill=_mix(pal["dim"], pal["card"], 0.45), width=1.5)
        # last price: a dashed line in the trade's color, its own mask (it fades in with the tag)
        last = cs[last_i]
        ly = y(self._val(last["c"]))
        col = self.sc
        S = c.s
        lm = Image.new("L", (int((px1 - px0) * S) + 2, int(8 * S)), 0)
        ld = ImageDraw.Draw(lm)
        my = 4 * S
        xx = px0
        while xx < px1:
            a_, b_ = xx, min(px1, xx + 5)
            if b_ < tx - 10 or a_ > tx + 10:
                ld.line([((a_ - px0) * S, my), ((b_ - px0) * S, my)], fill=150, width=max(1, int(S)))
            xx += 9
        lm = lm.resize((max(1, int(round(lm.width / 2))), max(1, int(round(lm.height / 2)))), Image.LANCZOS)
        self.price_line = (lm, px0, ly - 4)
        # the trade: a dot with a dark ring on the close
        r = 6
        c.d.ellipse([c.p(tx - r - 2.5), c.p(ly - r - 2.5), c.p(tx + r + 2.5), c.p(ly + r + 2.5)], fill=pal["card"])
        c.d.ellipse([c.p(tx - r), c.p(ly - r), c.p(tx + r), c.p(ly + r)], fill=col)
        c.d.ellipse([c.p(tx - 2.2), c.p(ly - 2.2), c.p(tx + 2.2), c.p(ly + 2.2)], fill=pal["text"])
        self.trade_xy = (tx, ly)
        self.close_path = [(xc(i), y(self._val(cc["c"]))) for i, cc in enumerate(cs) if cc]

    # ------------------------------------------------------------------ panel chrome
    def _panel_chrome(self, c: _Ctx):
        """Static labels of the panel; values are dynamic. Also computes the panel layout."""
        pal, sp = self.pal, self.spec
        x0, y0, x1, y1 = self.panel_box
        L, R = x0 + 24, x1 - 24
        self.pl = {}
        has_trader = bool(sp.get("buyer")) or bool(sp.get("holder"))
        tiles = self._tiles()
        rows = self._pressure_rows(sp.get("day") or {})
        ncols, th, rh = 2, 80, 52
        head_h = 22 + 34 + 16 + 86 + 40 + (42 if has_trader else 0) + 8 + 18
        nrows = math.ceil(len(tiles) / ncols) if tiles else 0
        rows_h = (30 + rh * len(rows)) if rows else 0
        avail = (y1 - 20) - y0

        def tiles_h(nr):
            return nr * th + max(0, nr - 1) * 12 + (22 if rows else 0) if nr else 0
        if nrows > 1 and head_h + tiles_h(nrows) + rows_h > avail:
            tiles = tiles[:ncols]
            nrows = 1
        if head_h + tiles_h(nrows) + rows_h > avail and rows:
            rows = rows[:max(1, len(rows) - 1)]
            rows_h = 30 + rh * len(rows)
        self.tiles, self.rows = tiles, rows
        left = avail - head_h - tiles_h(nrows) - rows_h
        ex = _clamp(left / 6, 0, 22)
        y = y0 + 22
        self.pl["pills_y"] = y
        y += 34 + 16 + ex * 0.5
        self.pl["hero_top"] = y
        y += 86
        self.pl["sub_base"] = y + 28
        y += 40
        if has_trader:
            self.pl["trader_y"] = y + 6
            y += 42
        y += 8 + ex
        c.line([(L, y), (R, y)], fill=pal["line"], width=1)
        y += 18 + ex
        tw = (R - L - 12) / 2
        self.pl["tiles"] = []
        for i, (label, _) in enumerate(tiles):
            r_, k = divmod(i, ncols)
            tx0 = L + k * (tw + 12)
            ty0 = y + r_ * (th + 12)
            c.rrect(tx0, ty0, tx0 + tw, ty0 + th, 10, fill=pal["tile"], outline=pal["line"], width=1)
            c.text(tx0 + 16, ty0 + 28, label, "mono", 16, pal["dim"], tracking=1.3)
            self.pl["tiles"].append((tx0 + 16, ty0 + 66, tx0 + tw - 16))
        if tiles:
            y += tiles_h(nrows) + (ex if rows else 0)
        if rows:
            c.text(L, y + 14, "BUYS VS SELLS", "mono", 16, pal["faint"], tracking=1.3)
            c.text(R, y + 14, "LAST 24H", "mono", 16, pal["faint"], anchor="rs", tracking=1.3)
            y += 30
            self.pl["rows"] = []
            for lab_l, lab_r, a, b, fmt in rows:
                c.text(L, y + 20, lab_l, "mono", 16, pal["dim"], tracking=1.2)
                c.text(R, y + 20, lab_r, "mono", 16, pal["dim"], anchor="rs", tracking=1.2)
                wl = c.width(lab_l, "mono", 16, 1.2)
                wr = c.width(lab_r, "mono", 16, 1.2)
                by = y + 32
                c.rrect(L, by, R, by + 8, 3, fill=_mix(pal["line"], pal["card"], 0.7))
                self.pl["rows"].append((L, R, y + 21, by, wl, wr))
                y += rh + ex * 0.5
        self.pl["bottom"] = y

    def _tiles(self):
        sp = self.spec
        out = []
        price_usd = None
        if sp.get("mc_usd") and sp.get("supply"):
            price_usd = sp["mc_usd"] / sp["supply"]
        elif self.pts and self.upq:
            price_usd = self.pts[-1][1] * self.upq
        if price_usd:
            out.append(("PRICE", ("price", price_usd, "$")))
        elif sp.get("mc_quote") and sp.get("supply"):
            out.append(("PRICE", ("price", sp["mc_quote"] / sp["supply"], sp.get("quote_symbol") or "")))
        day = sp.get("day") or {}
        dv = None
        if day.get("buy_volume_usd") is not None and day.get("sell_volume_usd") is not None:
            dv = day["buy_volume_usd"] + day["sell_volume_usd"]
        if dv is not None:
            out.append(("VOL 24H", ("money", dv)))
        if self.candles is not None:
            win = [p for p in self.pts if p[0] >= self.now - self.span]
            lab = _span_label(self.span)
            vq = sum(p[2] for p in win)
            if dv is None:
                if self.upq:
                    out.append((f"VOL {lab}", ("money", vq * self.upq)))
                else:
                    out.append((f"VOL {lab}", ("qty", vq, sp.get("quote_symbol") or "")))
                out.append((f"TRADES {lab}", ("int", len(win))))
                hi = max(p[1] for p in win) if win else None
                if hi:
                    if self.usd_axis:
                        out.append((f"HIGH {lab}", ("money", self._val(hi))))
                    else:
                        out.append((f"HIGH {lab}", ("qty", self._val(hi), sp.get("quote_symbol") or "")))
        return out

    def _pressure_rows(self, day):
        rows = []
        if day.get("buys") is not None and day.get("sells") is not None and day["buys"] + day["sells"] > 0:
            rows.append(("BUYS", "SELLS", day["buys"], day["sells"], "int"))
        if day.get("buy_volume_usd") is not None and day.get("sell_volume_usd") is not None and \
                day["buy_volume_usd"] + day["sell_volume_usd"] > 0:
            rows.append(("BUY VOL", "SELL VOL", day["buy_volume_usd"], day["sell_volume_usd"], "money"))
        if day.get("buyers") is not None and day.get("sellers") is not None and day["buyers"] + day["sellers"] > 0:
            rows.append(("BUYERS", "SELLERS", day["buyers"], day["sellers"], "int"))
        return rows

    # ------------------------------------------------------------------ dynamic pieces
    def u(self, v):
        return int(round(v * self.k))

    def tmask(self, role, size, txt, tabular=False, tracking=0.0, rich=None):
        key = (role, size, txt, tabular, tracking, repr(rich))
        m = self._tm.get(key)
        if m is None:
            m = _text_mask(txt, font(role, size * self.k), tabular, tracking * self.k, rich)
            self._tm[key] = m
        return m

    def put_text(self, frame, x, ybase, txt, role, size, color, alpha=1.0, anchor="l", tabular=False,
                 tracking=0.0, rich=None, dy=0.0, fill_img=None):
        """Draw text at design coords (x, baseline). anchor l/r/m. Returns the width in design units."""
        mask, pad, base, w = self.tmask(role, size, txt, tabular, tracking, rich)
        if alpha <= 0.003:
            return w / self.k
        px = x * self.k
        if anchor == "r":
            px -= w
        elif anchor == "m":
            px -= w / 2
        X = int(round(px)) - pad
        Y = int(round((ybase + dy) * self.k)) - base
        m = _scaled(mask, alpha)
        if fill_img is not None:
            fi = fill_img.resize(m.size) if fill_img.size != m.size else fill_img
            frame.paste(fi, (X, Y), m)
        else:
            frame.paste(color, (X, Y, X + m.width, Y + m.height), m)
        return w / self.k

    def _prepare_motion(self):
        pal, sp = self.pal, self.spec
        k = self.k
        self.items = []
        # --- pills: side + badges
        pills = [("BUY" if self.side > 0 else "SELL", self.sc, True)]
        if sp.get("whale"):
            pills.append(("WHALE", pal["accent"] if self.side > 0 else self.sc, False))
        if sp.get("dev"):
            pills.append(("DEV", pal["warn"], False))
        self.pill_sprites = []
        x = self.panel_box[0] + 24
        for i, (label, col, filled) in enumerate(pills):
            spr = self._pill_sprite(label, col, filled, arrow=(i == 0))
            self.pill_sprites.append((spr, x))
            x += spr[0].width / k + 10
        # --- hero amount
        usd, q = sp.get("usd"), sp.get("quote")
        qs = sp.get("quote_symbol") or ""
        self.hero = None
        L, R = self.panel_box[0] + 24, self.panel_box[2] - 24
        if usd is not None:
            fmt = money_formatter(usd)
            self.hero = (usd, fmt)
        elif q is not None:
            f = compact_formatter(q, prefix="", suffix=f" {qs}") if q >= 10000 else \
                (lambda v, d=(2 if q >= 1 else 4): f"{v:.{d}f} {qs}")
            self.hero = (q, f)
        self.hero_size = 100
        if self.hero:
            txt = self.hero[1](self.hero[0])
            while self.hero_size > 54:
                m = self.tmask("hero", self.hero_size, txt, tabular=True)
                if m[3] / k <= (R - L):
                    break
                self.hero_size -= 4
            fg = _vgrad(4, 64, _mix((255, 255, 255), self.sc, 0.35), self.sc)
            self.hero_fill = fg
            # soft bloom behind the hero (static, faded in)
            m = self.tmask("hero", self.hero_size, txt, tabular=True)[0]
            pad = int(36 * k)
            bm = Image.new("L", (m.width + 2 * pad, m.height + 2 * pad), 0)
            bm.paste(m, (pad, pad))
            bm = bm.filter(ImageFilter.GaussianBlur(16 * k))
            self.hero_bloom = (_scaled(bm, 0.16), pad)
        # --- sub line: quote and tokens
        parts = []
        if usd is not None and q is not None:
            parts.append(f"{qty(q)} {qs}")
        if sp.get("tokens") is not None:
            parts.append(f"{compact(sp['tokens'])} ${(sp.get('symbol') or '').lstrip('$')}")
        self.sub = parts
        # --- MC
        self.mc = None
        if sp.get("mc_usd") is not None:
            self.mc = (sp["mc_usd"], compact_formatter(sp["mc_usd"]))
        elif sp.get("mc_quote") is not None:
            self.mc = (sp["mc_quote"], compact_formatter(sp["mc_quote"], prefix="", suffix=f" {qs}"))
        if self.mc:
            self.mc_w = self.tmask("num", 50, self.mc[1](self.mc[0]))[3] / k
        ch = sp.get("price_change")
        self.chip = None
        if ch is not None:
            col = pal["buy"] if ch >= 0 else pal["sell"]
            txt = f"{abs(ch) * 100:,.1f}%" if abs(ch) < 10 else f"{abs(ch) * 100:,.0f}%"
            lab = _span_label(self.span) if self.span else ""
            self.chip = self._chip_sprite(txt, col, up=ch >= 0, suffix=lab)
        # --- last price tag on the axis
        self.tag = None
        geo = self._chart_geometry()
        if geo is not None:
            last = [cc for cc in self.candles if cc][-1]
            v = self._val(last["c"])
            self.tag = (self._tag_sprite(self._fmt_axis(v), self.sc), self.chart_box[2] - 12, geo["y"](v))
        # --- bars
        self.bar_sprites = []
        if getattr(self, "rows", None):
            for (L_, R_, ty, by, wl, wr), (ll, lr, a, b, fmt) in zip(self.pl["rows"], self.rows):
                tot = a + b
                gap = 4
                wa = (R_ - L_ - gap) * a / tot
                wb = (R_ - L_ - gap) - wa
                sa = self._bar_sprite(wa, 8, pal["buy"]) if wa >= 1 else None
                sb = self._bar_sprite(wb, 8, pal["sell"]) if wb >= 1 else None
                self.bar_sprites.append((sa, sb, L_, R_, by, wa, wb))
        if self.has_prog:
            x0, y0, x1, y1 = self.prog_bar
            p = _clamp(float(sp["progress"]))
            w = (x1 - x0) * p
            self.prog_fill = self._bar_sprite(w, 10, pal["accent"], grad=True) if w >= 1 else None
            self.prog_shine = self._bar_sprite(w, 10, _mix((255, 255, 255), pal["accent"], 0.7)) if w >= 1 else None
        # --- ping sprites (period in frames)
        self.ping_period = 1.25
        if geo is not None:
            self._make_pings()
            self._make_tip()
        self._make_sweep()
        # final frame: base + chart + every value landed
        self.final = self.full.copy()
        self._draw_values(self.final, self.INTRO + 10, final=True)
        self._make_glint_mask()

    # sprites ------------------------------------------------------------
    def _pill_sprite(self, label, col, filled, arrow=False):
        pal = self.pal
        S = 4 * self.k
        f = font("ui", 19 * S)
        tw = f.getlength(label) / S
        aw = 16 if arrow else 0
        w, h = tw + 28 + aw, 34
        img = Image.new("RGBA", (int(w * S), int(h * S)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if filled:
            d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=8 * S, fill=(*col, 255))
            tc = pal["ink"]
        else:
            d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=8 * S,
                                fill=(*_mix(col, pal["card"], 0.13), 255), outline=(*_mix(col, pal["card"], 0.55), 255),
                                width=int(1.2 * S))
            tc = col
        x = 14
        if arrow:
            cx, cy = (x + 6) * S, h / 2 * S
            if self.side > 0:
                d.polygon([(cx - 6 * S, cy + 4 * S), (cx + 6 * S, cy + 4 * S), (cx, cy - 6 * S)], fill=(*tc, 255))
            else:
                d.polygon([(cx - 6 * S, cy - 4 * S), (cx + 6 * S, cy - 4 * S), (cx, cy + 6 * S)], fill=(*tc, 255))
            x += aw
        d.text((x * S, h / 2 * S + 1 * S), label, font=f, fill=(*tc, 255), anchor="lm")
        img = img.resize((max(1, int(round(w * self.k))), max(1, int(round(h * self.k)))), Image.LANCZOS)
        return img.convert("RGB"), img.getchannel("A")

    def _chip_sprite(self, txt, col, up=True, suffix=""):
        pal = self.pal
        S = 4 * self.k
        f = font("num", 24 * S)
        fs = font("mono", 17 * S)
        tw = f.getlength(txt) / S
        sw = (fs.getlength(suffix) / S + 10) if suffix else 0
        w, h = tw + 46 + sw, 38
        img = Image.new("RGBA", (int(w * S), int(h * S)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=9 * S,
                            fill=(*_mix(col, pal["card"], 0.14), 255))
        cx, cy = 18 * S, h / 2 * S
        if up:
            d.polygon([(cx - 6 * S, cy + 4 * S), (cx + 6 * S, cy + 4 * S), (cx, cy - 6 * S)], fill=(*col, 255))
        else:
            d.polygon([(cx - 6 * S, cy - 4 * S), (cx + 6 * S, cy - 4 * S), (cx, cy + 6 * S)], fill=(*col, 255))
        d.text((32 * S, h / 2 * S + 1 * S), txt, font=f, fill=(*col, 255), anchor="lm")
        if suffix:
            d.text(((32 + tw + 10) * S, h / 2 * S + 1 * S), suffix, font=fs, fill=(*pal["dim"], 255), anchor="lm")
        img = img.resize((max(1, int(round(w * self.k))), max(1, int(round(h * self.k)))), Image.LANCZOS)
        return img.convert("RGB"), img.getchannel("A")

    def _tag_sprite(self, txt, col):
        S = 4 * self.k
        f = font("num", 16 * S)
        tw = f.getlength(txt) / S
        w, h = tw + 14, 26
        img = Image.new("RGBA", (int(w * S), int(h * S)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=5 * S, fill=(*col, 255))
        d.text((7 * S, h / 2 * S + 1 * S), txt, font=f, fill=(*self.pal["ink"], 255), anchor="lm")
        img = img.resize((max(1, int(round(w * self.k))), max(1, int(round(h * self.k)))), Image.LANCZOS)
        return img.convert("RGB"), img.getchannel("A")

    def _bar_sprite(self, w, h, col, grad=False):
        k = self.k
        W_, H_ = max(1, int(round(w * k))), max(1, int(round(h * k)))
        m = _round_mask(W_, H_, min(H_ / 2, 3 * k) if not grad else H_ / 2)
        if grad:
            rgb = _hgrad(W_, H_, _mix(col, self.pal["card"], 0.55), col)
        else:
            rgb = Image.new("RGB", (W_, H_), col)
        return rgb, m

    def _make_pings(self):
        k = self.k
        n = max(2, int(round(self.ping_period * self.fps)))
        R = int(46 * k)
        ss = 3
        self.pings = []
        for i in range(n):
            p = i / n
            e = ease_out_cubic(p)
            r = (8 + 34 * e) * k
            a = (1 - p) ** 1.6
            m = Image.new("L", (2 * R * ss, 2 * R * ss), 0)
            d = ImageDraw.Draw(m)
            c = R * ss
            wdt = max(1, int(round(2.2 * k * ss)))
            d.ellipse([c - r * ss, c - r * ss, c + r * ss, c + r * ss], outline=int(255 * a), width=wdt)
            # inner soft fill
            m2 = Image.new("L", m.size, 0)
            ImageDraw.Draw(m2).ellipse([c - r * ss, c - r * ss, c + r * ss, c + r * ss], fill=int(40 * a))
            m = ImageChops.lighter(m, m2)
            self.pings.append(m.resize((2 * R, 2 * R), Image.LANCZOS))
        self.ping_R = R
        # breathing halo
        hm = Image.new("L", (2 * R, 2 * R), 0)
        ImageDraw.Draw(hm).ellipse([R - 14 * k, R - 14 * k, R + 14 * k, R + 14 * k], fill=255)
        self.halo = hm.filter(ImageFilter.GaussianBlur(9 * k))

    def _make_tip(self):
        k = self.k
        R = int(30 * k)
        m = Image.new("L", (2 * R, 2 * R), 0)
        ImageDraw.Draw(m).ellipse([R - 12 * k, R - 12 * k, R + 12 * k, R + 12 * k], fill=150)
        m = m.filter(ImageFilter.GaussianBlur(8 * k))
        core = Image.new("L", (2 * R * 4, 2 * R * 4), 0)
        ImageDraw.Draw(core).ellipse([4 * (R - 4 * k), 4 * (R - 4 * k), 4 * (R + 4 * k), 4 * (R + 4 * k)], fill=255)
        core = core.resize((2 * R, 2 * R), Image.LANCZOS)
        self.tip = (ImageChops.lighter(m, core), R)
        # a soft vertical light under the wipe's front, inside the plot only
        px0, py0, px1, py1 = self.plot
        hgt = int((py1 - py0) * k)
        bw = int(70 * k)
        col = Image.new("L", (bw, 1), 0)
        cp = col.load()
        for x in range(bw):
            f = (x - bw / 2) / (bw * 0.28)
            cp[x, 0] = int(34 * math.exp(-f * f))
        self.front = col.resize((bw, max(1, hgt)), Image.NEAREST)
        vf = Image.new("L", (1, max(2, hgt)), 0)
        vp = vf.load()
        for y in range(vf.height):
            f = y / (vf.height - 1)
            vp[0, y] = int(255 * min(1.0, f / 0.25, (1 - f) / 0.25) ** 1.5)
        self.front = ImageChops.multiply(self.front, vf.resize((bw, max(1, hgt)), Image.BILINEAR))

    def _make_sweep(self):
        """A diagonal band (L), used with the bright parts of the card as a silver glint."""
        k = self.k
        H = self.H
        bw = int(150 * k)
        slope = 0.35
        w = bw + int(H * slope) + 2
        # build skewed band row by row is slow; draw a polygon and blur instead
        m = Image.new("L", (w, H), 0)
        d = ImageDraw.Draw(m)
        x_off = int(H * slope)
        d.polygon([(x_off + bw * 0.35, 0), (x_off + bw * 0.65, 0), (bw * 0.65, H), (bw * 0.35, H)], fill=255)
        self.sweep = m.filter(ImageFilter.GaussianBlur(22 * k))
        self.sweep_w = w

    def _make_glint_mask(self):
        # the bright parts of the finished card catch the light, the dark page does not
        lum = self.final.convert("L")
        self.glint_src = lum.point([0 if v < 24 else min(255, int((v - 24) * 1.7)) for v in range(256)])

    # ------------------------------------------------------------------ frame composition
    def _draw_values(self, frame, t, final=False):
        """All numbers, pills, bars and tags at time t of the intro."""
        pal, sp, k = self.pal, self.spec, self.k
        x0, y0, x1, y1 = self.panel_box
        L, R = x0 + 24, x1 - 24
        # pills
        for i, (spr, x) in enumerate(self.pill_sprites):
            a = ease_out_cubic(_seg(t, 0.05 + i * 0.08, 0.35 + i * 0.08))
            if a > 0:
                rgb, m = spr
                frame.paste(rgb, (self.u(x), self.u(self.pl["pills_y"] + (1 - a) * 8)), _scaled(m, a))
        # hero
        if self.hero:
            v, fmt = self.hero
            e = ease_out_expo(_seg(t, 0.12, 1.25))
            a = ease_out_cubic(_seg(t, 0.10, 0.40))
            txt = fmt(v * e) if not final else fmt(v)
            if final or e >= 0.9999:
                txt = fmt(v)
            ybase = self.pl["hero_top"] + 74
            bm, pad = self.hero_bloom
            mm, mp, mb, mw = self.tmask("hero", self.hero_size, txt, tabular=True)
            ba = ease_in_out_sine(_seg(t, 0.9, 1.6))
            if ba > 0:
                bx = self.u(L) - mp - pad
                by = self.u(ybase) - mb - pad
                frame.paste(self.sc, (bx, by, bx + bm.width, by + bm.height), _scaled(bm, ba))
            self.put_text(frame, L, ybase, txt, "hero", self.hero_size, self.sc, alpha=a, tabular=True,
                          dy=(1 - a) * 14, fill_img=self.hero_fill)
        # sub line
        a = ease_out_cubic(_seg(t, 0.35, 0.70))
        if self.sub and a > 0:
            x = L
            yb = self.pl["sub_base"]
            for i, s in enumerate(self.sub):
                if i:
                    x += 12
                    self._arrow(frame, x, yb - 8, a)
                    x += 26
                role, col = ("num", pal["text"]) if i == 0 else ("num", pal["text"])
                avail = R - x
                size = 24
                while size > 16 and self.tmask(role, size, s)[3] / k > avail:
                    size -= 1
                txt = s
                while self.tmask(role, size, txt)[3] / k > avail and len(txt) > 4:
                    txt = txt[:-2] + "…"
                x += self.put_text(frame, x, yb, txt, role, size, col if i == 0 else pal["dim"], alpha=a,
                                   dy=(1 - a) * 6)
        # trader line
        if "trader_y" in self.pl:
            a = ease_out_cubic(_seg(t, 0.5, 0.85))
            if a > 0:
                self._trader(frame, L, R, self.pl["trader_y"], a)
        # MC hero of the chart card
        cx0, cy0, cx1, cy1 = self.chart_box
        if self.mc:
            v, fmt = self.mc
            e = ease_out_expo(_seg(t, 0.25, 1.35))
            a = ease_out_cubic(_seg(t, 0.2, 0.5))
            txt = fmt(v) if (final or e >= 0.9999) else fmt(v * e)
            self.put_text(frame, cx0 + 24, cy0 + 90, txt, "num", 50, pal["text"], alpha=a, dy=(1 - a) * 8)
            if self.chip:
                ca = ease_out_cubic(_seg(t, 1.2, 1.55))
                if ca > 0:
                    rgb, m = self.chip
                    X = self.u(cx0 + 24 + self.mc_w + 16) + int((1 - ca) * -10 * k)
                    frame.paste(rgb, (X, self.u(cy0 + 55)), _scaled(m, ca))
        elif self.chip:
            ca = ease_out_cubic(_seg(t, 1.1, 1.45))
            if ca > 0:
                rgb, m = self.chip
                frame.paste(rgb, (self.u(cx0 + 24), self.u(cy0 + 55)), _scaled(m, ca))
        # last price tag and its line
        if self.tag:
            (rgb, m), xr, ty = self.tag
            a = ease_out_cubic(_seg(t, 1.3, 1.6))
            if a > 0:
                lm, lx, lyy = self.price_line
                X, Y = self.u(lx), self.u(lyy)
                frame.paste(self.sc, (X, Y, X + lm.width, Y + lm.height), _scaled(lm, a))
                frame.paste(rgb, (self.u(xr) - rgb.width, self.u(ty) - rgb.height // 2), _scaled(m, a))
        # tiles
        for i, ((label, val), (tx, tyb, txr)) in enumerate(zip(self.tiles, self.pl.get("tiles", []))):
            a = ease_out_cubic(_seg(t, 0.45 + i * 0.07, 0.85 + i * 0.07))
            if a <= 0:
                continue
            e = ease_out_expo(_seg(t, 0.45 + i * 0.07, 1.35 + i * 0.05))
            if final:
                e = 1.0
            kind = val[0]
            rich = None
            if kind == "price":
                pre = "$" if val[2] == "$" else ""
                rich = [(pre, 0)] + _subscript_price(val[1])
                if val[2] != "$":
                    rich = rich + [(" " + val[2], 0)]
                txt = "".join(s for s, _ in rich)
            elif kind == "money":
                f = compact_formatter(val[1])
                txt = f(val[1] * e) if e < 0.9999 else f(val[1])
            elif kind == "qty":
                txt = f"{compact(val[1] * e)} {val[2]}"
            else:
                txt = _grp(round(val[1] * e))
            size = 30
            while size > 18 and self.tmask("num", size, txt, rich=rich)[3] / k > (txr - tx):
                size -= 2
            self.put_text(frame, tx, tyb, txt, "num", size, pal["text"], alpha=a, rich=rich, dy=(1 - a) * 6)
        # pressure rows
        for i, ((sa, sb, L_, R_, by, wa, wb), (ll, lr, a_v, b_v, fmt), geo) in enumerate(
                zip(self.bar_sprites, self.rows, self.pl.get("rows", []))):
            t0 = 0.55 + i * 0.1
            e = ease_out_quart(_seg(t, t0, t0 + 0.95))
            ea = ease_out_expo(_seg(t, t0, t0 + 1.0))
            if final:
                e = ea = 1.0
            _, _, ty, _, wl, wr = geo
            al = ease_out_cubic(_seg(t, t0, t0 + 0.25))
            if fmt == "money":
                fa, fb = compact_formatter(a_v), compact_formatter(b_v)
            else:
                fa = fb = int_formatter(0)
            ta = fa(a_v * ea) if ea < 0.9999 else fa(a_v)
            tb = fb(b_v * ea) if ea < 0.9999 else fb(b_v)
            self.put_text(frame, L_ + wl + 10, ty, ta, "num", 21, pal["buy"], alpha=al)
            self.put_text(frame, R_ - wr - 10, ty, tb, "num", 21, pal["sell"], alpha=al, anchor="r")
            if sa and e > 0:
                rgb, m = sa
                vis = int(round(rgb.width * e))
                if vis > 0:
                    frame.paste(rgb.crop((rgb.width - vis, 0, rgb.width, rgb.height)), (self.u(L_), self.u(by)),
                                m.crop((rgb.width - vis, 0, rgb.width, rgb.height)))
            if sb and e > 0:
                rgb, m = sb
                vis = int(round(rgb.width * e))
                if vis > 0:
                    X = self.u(R_) - vis
                    frame.paste(rgb.crop((0, 0, vis, rgb.height)), (X, self.u(by)), m.crop((0, 0, vis, rgb.height)))
        # progress
        if self.has_prog:
            p = _clamp(float(sp["progress"]))
            e = ease_out_quart(_seg(t, 0.3, 1.45))
            if final:
                e = 1.0
            x0p, y0p, x1p, y1p = self.prog_bar
            if self.prog_fill:
                rgb, m = self.prog_fill
                vis = int(round(rgb.width * e))
                if vis > 0:
                    frame.paste(rgb.crop((rgb.width - vis, 0, rgb.width, rgb.height)), (self.u(x0p), self.u(y0p)),
                                m.crop((rgb.width - vis, 0, rgb.width, rgb.height)))
            a = ease_out_cubic(_seg(t, 0.3, 0.6))
            txt = f"{p * e * 100:.1f}%"
            gx = 1256 - self.c2.width("TO GRADUATION", "mono", 17, 1.2) - 12
            self.put_text(frame, gx, y1p + 3, txt, "num", 24, pal["accent"], alpha=a, anchor="r", tabular=True)

    def _arrow(self, frame, x, y, a):
        col = _mix(self.pal["dim"], self.pal["card"], a)
        k = self.k
        d = ImageDraw.Draw(frame)
        X0, X1, Y = self.u(x), self.u(x + 16), self.u(y)
        d.line([(X0, Y), (X1, Y)], fill=col, width=max(1, int(2 * k)))
        d.line([(X1 - int(6 * k), Y - int(6 * k)), (X1, Y), (X1 - int(6 * k), Y + int(6 * k))], fill=col,
               width=max(1, int(2 * k)))

    def _trader_plan(self, L, R):
        """What fits on the trader line: address, then holder status (pill or change + share)."""
        pal, sp = self.pal, self.spec
        h = sp.get("holder") or {}
        pill = None
        if h.get("new"):
            pill = ("NEW HOLDER", pal["accent"])
        elif h.get("all"):
            pill = ("SOLD ALL" if self.side < 0 else "ALL IN", self.sc)
        stats = []
        if not pill and h.get("change") is not None:
            chg = h["change"]
            stats.append((f"{'+' if chg >= 0 else '−'}{abs(chg) * 100:.0f}% bag", pal["buy"] if chg >= 0 else pal["sell"]))
        if not pill and h.get("share") is not None:
            stats.append((f"holds {h['share'] * 100:.2f}%", pal["text"]))
        addr = sp.get("buyer")
        avail = R - L
        cands = []
        if addr:
            cands += [(True, len(stats)), (True, min(1, len(stats)))]
        cands += [(False, len(stats))]
        if addr:
            cands += [(True, 0)]
        for with_addr, keep in cands:
            for size in (21, 20, 19, 18):
                items = []
                if with_addr:
                    items.append(("addr", addr, "mono", pal["dim"]))
                if pill:
                    items.append(("pill",) + pill)
                for i, (txt, col) in enumerate(stats[:keep]):
                    if i:
                        items.append(("text", " · ", "mono", pal["faint"]))
                    items.append(("text", txt, "num", col))
                w = 0.0
                for j, it in enumerate(items):
                    if j and (items[j - 1][0] in ("addr", "pill") or it[0] == "pill"):
                        w += 14
                    if it[0] == "pill":
                        w += self._holder_pill(it[1], it[2])[0].width / self.k
                    else:
                        w += self.tmask(it[2], size, it[1])[3] / self.k
                if w <= avail and items:
                    return size, items
        return 21, []

    def _holder_pill(self, label, col):
        key = ("holder_pill", label)
        spr = self._tm.get(key)
        if spr is None:
            spr = self._small_pill(label, col)
            self._tm[key] = spr
        return spr

    def _trader(self, frame, L, R, y, a):
        if not hasattr(self, "_tplan"):
            self._tplan = self._trader_plan(L, R)
        size, items = self._tplan
        x = L
        for j, it in enumerate(items):
            if j and (items[j - 1][0] in ("addr", "pill") or it[0] == "pill"):
                x += 14
            if it[0] == "pill":
                rgb, m = self._holder_pill(it[1], it[2])
                frame.paste(rgb, (self.u(x), self.u(y + 22) - int(rgb.height * 0.78)), _scaled(m, a))
                x += rgb.width / self.k
            else:
                x += self.put_text(frame, x, y + 22, it[1], it[2], size, it[3], alpha=a)

    def _small_pill(self, label, col):
        pal = self.pal
        S = 4 * self.k
        f = font("num", 16 * S)
        tw = f.getlength(label) / S
        w, h = tw + 20, 28
        img = Image.new("RGBA", (int(w * S), int(h * S)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=6 * S,
                            fill=(*_mix(col, pal["card"], 0.14), 255), outline=(*_mix(col, pal["card"], 0.6), 255),
                            width=int(1.2 * S))
        d.text((10 * S, h / 2 * S + 1 * S), label, font=f, fill=(*col, 255), anchor="lm")
        img = img.resize((max(1, int(round(w * self.k))), max(1, int(round(h * self.k)))), Image.LANCZOS)
        return img.convert("RGB"), img.getchannel("A")

    def _chart_reveal(self, frame, t):
        """Candles drawn in left to right behind a soft wipe, with a light at the tip."""
        if self._chart_geometry() is None:
            return 1.0
        e = ease_in_out_cubic(_seg(t, 0.15, 1.45))
        if e <= 0:
            return 0.0
        px0, py0, px1, py1 = self.plot
        k = self.k
        X0, Y0, X1, Y1 = self.u(px0 - 6), self.u(py0 - 8), self.u(px1 + 6), self.u(py1 + 2)
        soft = int(60 * k)
        width = X1 - X0
        front = int(round(-soft + (width + soft) * e))      # x of full visibility end
        if e >= 1:
            frame.paste(self.full.crop((X0, Y0, X1, Y1)), (X0, Y0))
            return e
        if front + soft > 0:
            if not hasattr(self, "_wipe"):
                ramp = Image.new("L", (width + 2 * soft + width, 1), 0)
                rp = ramp.load()
                for x in range(ramp.width):
                    if x < width:
                        rp[x, 0] = 255
                    elif x < width + soft:
                        f = (x - width) / soft
                        rp[x, 0] = int(255 * (1 - f) ** 1.5)
                self._wipe = ramp.resize((ramp.width, Y1 - Y0), Image.NEAREST)
            off = width - front
            mask = self._wipe.crop((off, 0, off + width, Y1 - Y0))
            frame.paste(self.full.crop((X0, Y0, X1, Y1)), (X0, Y0), mask)
            # the light at the front and the tip on the close line
            fx = X0 + front + soft // 3
            fa = math.sin(math.pi * _clamp(e * 1.05))
            if fa > 0.01:
                fw = self.front
                fxx = fx - fw.width // 2
                frame.paste(self.sc, (fxx, self.u(py0), fxx + fw.width, self.u(py0) + fw.height), _scaled(fw, fa * 0.9))
                ty = self._close_at(fx / k)
                if ty is not None:
                    tm, R = self.tip
                    frame.paste(self.sc, (fx - R, self.u(ty) - R, fx + R, self.u(ty) + R), _scaled(tm, fa))
        return e

    def _close_at(self, x):
        cp = self.close_path
        if not cp:
            return None
        if x <= cp[0][0]:
            return cp[0][1]
        for (xa, ya), (xb, yb) in zip(cp, cp[1:]):
            if xa <= x <= xb:
                f = (x - xa) / (xb - xa) if xb > xa else 0
                return ya + (yb - ya) * f
        return cp[-1][1] if x <= cp[-1][0] + 20 else None

    def _idle(self, frame, t):
        """Ping on the trade, breathing halo, shimmer on the progress bar."""
        k = self.k
        if self.tag is not None and hasattr(self, "pings"):
            tx, ty = self.trade_xy
            start = 1.33
            if t >= start:
                n = len(self.pings)
                i = int(round((t - start) * self.fps)) % n
                R = self.ping_R
                X, Y = self.u(tx) - R, self.u(ty) - R
                frame.paste(self.sc, (X, Y, X + 2 * R, Y + 2 * R), self.pings[i])
                br = 0.5 + 0.5 * math.cos(math.tau * (t - start) / self.ping_period)
                frame.paste(self.sc, (X, Y, X + 2 * R, Y + 2 * R), _scaled(self.halo, 0.25 + 0.35 * br))
                # keep the dot on top of the halo
                if not hasattr(self, "_dot"):
                    r = int(12 * k)
                    self._dot = self.full.crop((self.u(tx) - r, self.u(ty) - r, self.u(tx) + r, self.u(ty) + r))
                    dm = Image.new("L", (2 * r * 4, 2 * r * 4), 0)
                    ImageDraw.Draw(dm).ellipse([4 * (r - 8.5 * k), 4 * (r - 8.5 * k), 4 * (r + 8.5 * k), 4 * (r + 8.5 * k)], fill=255)
                    self._dotm = dm.resize((2 * r, 2 * r), Image.LANCZOS)
                    self._dotr = r
                r = self._dotr
                frame.paste(self._dot, (self.u(tx) - r, self.u(ty) - r), self._dotm)
        if self.has_prog and getattr(self, "prog_fill", None) and t > 1.45:
            rgb, m = self.prog_shine
            period = 2.2
            ph = ((t - 1.45) % period) / period
            w = rgb.width
            bw = int(90 * k)
            cx = int(-bw + (w + 2 * bw) * ease_in_out_sine(ph))
            if getattr(self, "_band_strip", None) is None:
                b = Image.new("L", (3 * w + 2 * bw, 1), 0)
                bp = b.load()
                for x in range(-bw, bw):
                    bp[w + bw + x, 0] = int(150 * math.exp(-(x / (bw * 0.45)) ** 2))
                self._band_strip = b.resize((b.width, rgb.height), Image.NEAREST)
            off = w + bw - cx
            bm = self._band_strip.crop((off, 0, off + w, rgb.height))
            mm = ImageChops.multiply(bm, m)
            x0p, y0p, _, _ = self.prog_bar
            frame.paste(rgb, (self.u(x0p), self.u(y0p)), mm)

    def _glint(self, frame, t):
        a, b = self.SWEEP
        if not (a <= t <= b):
            return
        e = ease_in_out_sine(_seg(t, a, b))
        sw = self.sweep
        X = int(-sw.width + (self.W + sw.width) * e)
        x0, x1 = max(0, X), min(self.W, X + sw.width)
        if x1 <= x0:
            return
        band = sw.crop((x0 - X, 0, x1 - X, self.H))
        src = self.glint_src.crop((x0, 0, x1, self.H))
        m = ImageChops.multiply(band, src)
        m = _scaled(m, 0.75)
        frame.paste(self.pal["glint"], (x0, 0, x1, self.H), m)

    def frame(self, t):
        """The frame at time t (seconds)."""
        if t >= self.INTRO:
            f = self.final.copy()
        else:
            f = self.base.copy()
            self._chart_reveal(f, t)
            self._draw_values(f, t)
        self._idle(f, t)
        self._glint(f, t)
        end = self.seconds
        if t > end - self.OUTRO:
            a = ease_in_out_sine(_seg(t, end - self.OUTRO, end))
            f = Image.blend(f, self.base, a)
        return f

    def still(self):
        f = self.final.copy()
        # a ping caught mid-way reads as "this is the trade"
        self._idle(f, 1.33 + self.ping_period * 0.22)
        return f


# ----------------------------------------------------------------------------- output
def _ffmpeg():
    from .chart import find_ffmpeg
    exe = find_ffmpeg()
    if not exe:
        raise RuntimeError("no ffmpeg")
    return exe


# 960x540: Telegram shows the card ~360 px wide on a phone, and on the server (a 300 MB
# memory cap shared with the bot's ~170 MB) a 1280x720 encode does not fit: x264 alone took
# ~170 MB there, ~70 MB at this size without lookahead.
SIZE = (960, 540)


def render_card(spec: dict, fps: int = 60, size=SIZE, seconds: float = 4.8, crf: int = 21,
                preset: str = "veryfast", threads: int = 1, stats: dict | None = None) -> bytes:
    """MP4 bytes (H.264, yuv420p, no audio). fps=30 halves the cost."""
    t0 = time.perf_counter()
    c0 = time.process_time()
    card = Card(spec, size=size, seconds=seconds, fps=fps)
    t_setup = time.perf_counter() - t0
    n = int(round(seconds * fps))
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    W, H = card.W, card.H
    loglevel = "info" if stats is not None else "error"
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", loglevel, "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-an", "-c:v", "libx264", "-preset", preset,
           "-crf", str(crf), "-pix_fmt", "yuv420p", "-threads", str(threads),
           "-x264-params", "rc-lookahead=0:ref=1:bframes=0",  # no frame buffers: far less memory
           "-movflags", "+faststart"]
    if stats is not None:
        cmd += ["-benchmark"]
    cmd += [path]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        import threading
        err = []
        th = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
        th.start()
        for i in range(n):
            proc.stdin.write(card.frame(i / fps).tobytes())
        proc.stdin.close()
        rc = proc.wait(timeout=120)
        th.join(5)
        if rc != 0:
            raise RuntimeError((err[0] if err else b"").decode(errors="replace")[-500:])
        data = Path(path).read_bytes()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if stats is not None:
        stats["wall"] = time.perf_counter() - t0
        stats["setup"] = t_setup
        stats["py_cpu"] = time.process_time() - c0
        txt = (err[0] if err else b"").decode(errors="replace")
        for tok in txt.split():
            if tok.startswith("utime="):
                stats["ff_utime"] = float(tok[6:].rstrip("s"))
            if tok.startswith("stime="):
                stats["ff_stime"] = float(tok[6:].rstrip("s"))
        stats["frames"] = n
        stats["bytes"] = len(data)
    return data


def render_still(spec: dict, size=SIZE) -> bytes:
    card = Card(spec, size=size)
    out = io.BytesIO()
    card.still().save(out, "PNG", optimize=False)
    return out.getvalue()
