"""The /promo video: a short animated post about one vibe/vibe launch, for its creator's X.

render(spec, logo) -> MP4 bytes (H.264, yuv420p, 4:5, no audio). Pure drawing: the facts
come in `spec` (promo.gather), already checked and cleaned; nothing here touches the network.

Three scenes on black, crossfaded, no hard cuts (design units 1080x1350, scaled to the output):

  0.0 s  hero     the token's logo in a slowly turning ring, $TICKER in chrome, its name,
                  chips with the pair asset and the network. Frame 0 is the whole hero: X and
                  Telegram take the first frame as the cover.
  2.2 s  stats    the hero glides up into a header; the bonding-curve panel rises, its % counts
                  up to the real value as the bar fills (or GRADUATED, full); tiles for market
                  cap, holders, buys in 24 h and the pair rise in a short stagger.
  5.3 s  end      the stats lift away; SILVERHAND BUYS, "made with Silverhand Buys" and the call
                  to add the bot to a group.

Motion is the Premium archetype (SilverhandMedia/STYLE.md, the motion-design skill): one
signature curve, cubic-bezier(0.4, 0, 0.2, 1), moves of 350-600 ms, entrances rise 24 units
while they fade in, exits ease in, no overshoot, staggers under 500 ms. Three layers: the
numbers and the hero (primary), the panels, ring and glint (secondary), a drifting glow over
the grid (ambient).

Cost: every still element is drawn once as a sprite (shapes at 2x, scaled down); a frame is
the backdrop plus a few pastes and the count-up texts. Frames stream into ffmpeg one by one.
"""

import math
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .card import AVATAR, _ffmpeg, font

DW, DH = 1080, 1350
# 640x800, 4:5: on the server (a 300 MB cap, the bot ~160-215 MB) x264 took 87 MB at 720x900
# and 72 MB at 640x800; X shows a post about 400 px wide on a phone.
SIZE = (640, 800)
FPS = 30
SECONDS = 7.4

BG = (5, 7, 7)
CORE = (16, 21, 20)
PANEL = (12, 15, 15)
LINE = (34, 41, 40)
TEXT = (238, 243, 241)
DIM = (140, 152, 147)
FAINT = (82, 93, 89)
ACCENT = (45, 245, 154)
LIME = (223, 249, 2)      # the testnet bot's colour
GOLD = (242, 201, 76)     # graduation, as on the buy card

HERO_Y = 400              # the logo's centre in the hero
HEAD_X, HEAD_Y, HEAD_D = 70, 205, 120  # the logo in the header: left edge, centre, diameter

# the timeline, seconds
T_HERO_OUT = 2.2          # the hero starts gliding into the header
T_STATS = 2.75            # the curve panel rises
T_COUNT = 3.0             # its % starts counting
T_TILES = 3.35            # the first tile rises
T_END = 5.45              # the stats lift away, the end card comes in
MOVE, QUICK, SLOW = 0.5, 0.35, 0.6
STAGGER = 0.09


# ----------------------------------------------------------------------------- easing
def _bezier(x1, y1, x2, y2):
    """cubic-bezier(x1, y1, x2, y2) as f(t) for t in 0..1 (Newton steps, then bisection)."""
    def coord(t, a, b):
        return 3 * a * t * (1 - t) ** 2 + 3 * b * t ** 2 * (1 - t) + t ** 3

    def f(x):
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        t = x
        for _ in range(6):
            dx = coord(t, x1, x2) - x
            d = 3 * x1 * (1 - t) ** 2 + 6 * (x2 - x1) * t * (1 - t) + 3 * (1 - x2) * t ** 2
            if abs(dx) < 1e-5 or d == 0:
                break
            t = min(1.0, max(0.0, t - dx / d))
        if abs(coord(t, x1, x2) - x) > 1e-3:
            lo, hi = 0.0, 1.0
            for _ in range(30):
                t = (lo + hi) / 2
                lo, hi = (t, hi) if coord(t, x1, x2) < x else (lo, t)
        return coord(t, y1, y2)
    return f


ease = _bezier(0.4, 0.0, 0.2, 1.0)        # the signature curve: on-screen moves
ease_out = _bezier(0.0, 0.0, 0.2, 1.0)    # entrances: decelerate
ease_in = _bezier(0.4, 0.0, 1.0, 1.0)     # exits: accelerate


def _seg(t, t0, dur):
    return 0.0 if t <= t0 else 1.0 if t >= t0 + dur else (t - t0) / dur


def _lerp(a, b, k):
    return a + (b - a) * k


def _mix(a, b, k):
    """k of b over a."""
    return tuple(int(round(x + (y - x) * k)) for x, y in zip(a, b))


# ----------------------------------------------------------------------------- numbers
def compact(x: float) -> str:
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= limit:
            v = x / limit
            return (f"{v:.2f}" if v < 10 else f"{v:.1f}" if v < 100 else f"{v:.0f}").rstrip("0").rstrip(".") + suffix
    if x >= 100:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:.2f}".rstrip("0").rstrip(".")
    if x <= 0:
        return "0"
    digits = max(2, 3 - int(math.floor(math.log10(x))))
    return f"{x:.{digits}f}".rstrip("0").rstrip(".")


def usd(x: float, approx: bool) -> str:
    return ("≈" if approx else "") + "$" + compact(x)


def pct(p: float) -> str:
    v = p * 100
    return f"{v:.1f}%" if v < 99.95 else "100%"


# ----------------------------------------------------------------------------- sprites
class Sprite:
    """An RGBA picture split once into colour and alpha. A faded alpha is made per paste and
    not kept: each level is used for a frame or two, and keeping them cost ~9 MB."""

    def __init__(self, img: Image.Image):
        self.w, self.h = img.size
        self.rgb = img.convert("RGB")
        self.a = img.getchannel("A")

    def alpha(self, k: float):
        q = int(round(k * 64))
        if q >= 64:
            return self.a
        return self.a.point([v * q // 64 for v in range(256)])

    def put(self, frame, x, y, k=1.0):
        if k <= 0.01:
            return
        frame.paste(self.rgb, (int(round(x)), int(round(y))), self.alpha(k))


def _text(frame, xy, txt, f, colour, a):
    """Text at baseline-left xy with opacity a (ImageDraw draws text on RGB at full ink)."""
    if a <= 0.01:
        return
    l, t, r, b = f.getbbox(txt, anchor="ls")
    m = Image.new("L", (r - l + 2, b - t + 2), 0)
    ImageDraw.Draw(m).text((-l + 1, -t + 1), txt, font=f, fill=int(255 * min(1.0, a)), anchor="ls")
    frame.paste(colour, (int(xy[0] + l - 1), int(xy[1] + t - 1)), m)


def _blank(w, h):
    return Image.new("RGBA", (max(1, int(w)), max(1, int(h))), (0, 0, 0, 0))


def _down(img, w, h):
    return img.resize((max(1, int(round(w))), max(1, int(round(h)))), Image.LANCZOS)


def _vgrad(w, h, stops):
    """A vertical gradient (RGB) through [(pos 0..1, colour)]."""
    col = Image.new("RGB", (1, max(2, h)))
    px = col.load()
    for y in range(col.height):
        p = y / (col.height - 1)
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= p <= p1:
                px[0, y] = _mix(c0, c1, (p - p0) / (p1 - p0) if p1 > p0 else 0)
                break
    return col.resize((max(1, w), max(1, h)), Image.BILINEAR)


CHROME = [(0, (255, 255, 255)), (0.42, (210, 222, 230)), (0.5, (104, 120, 132)),
          (0.62, (178, 194, 204)), (1, (240, 246, 248))]


class Video:
    """The "dashboard" style; other styles (promo_styles) reuse its helpers, header line and
    end card, and draw their own scenes."""
    accent = ACCENT
    t_end = T_END       # when the end card's scene starts
    preview_at = 4.6    # the frame the style picker shows

    def __init__(self, spec: dict, logo: Image.Image | None, size=SIZE, fps=FPS, seconds=SECONDS):
        self._setup(spec, size, fps, seconds)
        self._build_backdrop()
        self._build_hero(logo)
        self._build_stats()
        self._build_end()

    def _setup(self, spec, size, fps, seconds):
        if spec["name"].casefold() == spec["symbol"].casefold():  # $KYLE named KYLE: say it once
            spec = dict(spec, name="")
        self.spec = spec
        self.W, self.H = size
        self.s = self.W / DW
        self.fps, self.seconds = fps, seconds
        self.grad = bool(spec.get("graduated"))
        self.prog = 1.0 if self.grad else max(0.0, min(1.0, float(spec.get("progress") or 0)))

    def stat_items(self):
        """What is known besides the curve, as (label, value or None, format, small text): the
        tiles of the dashboard, the lines of other styles. The pair is always there."""
        sp = self.spec
        out = []
        if sp.get("mc_usd") is not None:
            out.append(("MARKET CAP", sp["mc_usd"], lambda v: usd(v, sp.get("mc_approx", False)),
                        f"{compact(sp['mc_quote'])} {sp['pair']}" if sp.get("mc_quote") is not None else None))
        elif sp.get("mc_quote") is not None:
            out.append(("MARKET CAP", sp["mc_quote"], lambda v: f"{compact(v)} {sp['pair']}", None))
        if sp.get("holders") is not None:
            out.append(("HOLDERS", sp["holders"], lambda v: f"{int(round(v)):,}", None))
        if sp.get("buys_24h") is not None:
            out.append(("BUYS · 24H", sp["buys_24h"], lambda v: f"{int(round(v)):,}", None))
        out.append(("PAIR", None, None, sp["pair"]))
        return out

    def curve_word(self):
        """What the % counts: the curve's tokens SOLD (curve factory) or the target RAISED (V6)."""
        return "SOLD" if self.spec.get("sold") is not None else "RAISED"

    def curve_caption(self):
        return "OF THE CURVE SOLD" if self.curve_word() == "SOLD" else "OF THE TARGET RAISED"

    def curve_line(self):
        """What the curve % means, in words."""
        sp = self.spec
        if self.grad:
            return f"Graduated to {sp.get('dex') or 'Uniswap v4'}"
        if sp.get("sold") is not None and sp.get("curve_tokens"):  # a curve-factory curve
            return f"{compact(sp['sold'])} of {compact(sp['curve_tokens'])} tokens sold"
        if sp.get("raised") is not None and sp.get("target"):      # a V6 curve
            return f"{compact(sp['raised'])} of {compact(sp['target'])} {sp['pair']} raised"
        return f"{pct(self.prog)} of the way to graduation"

    # ------------------------------------------------------------------ helpers
    def p(self, v):
        return v * self.s

    def f(self, role, size):
        return font(role, self.p(size))

    def _text_sprite(self, txt, role, size, fill, tracking=0.0):
        f = self.f(role, size)
        tr = self.p(tracking)
        w = sum(f.getlength(c) for c in txt) + tr * max(0, len(txt) - 1) if tr else f.getlength(txt)
        asc, desc = f.getmetrics()
        img = _blank(math.ceil(w) + 4, asc + desc + 4)
        d = ImageDraw.Draw(img)
        if tr:
            x = 2.0
            for c in txt:
                d.text((x, 2 + asc), c, font=f, fill=fill, anchor="ls")
                x += f.getlength(c) + tr
        else:
            d.text((2, 2 + asc), txt, font=f, fill=fill, anchor="ls")
        return img, 2 + asc  # the sprite and its baseline

    def _fit(self, txt, role, size, max_w, min_size):
        """The biggest size from `size` down to `min_size` at which txt fits max_w (design
        units), and the text, cut with an ellipsis if it does not fit even then."""
        sz = size
        while sz > min_size and self.f(role, sz).getlength(txt) > self.p(max_w):
            sz -= 2
        f = self.f(role, sz)
        if f.getlength(txt) > self.p(max_w):
            while len(txt) > 1 and f.getlength(txt + "…") > self.p(max_w):
                txt = txt[:-1]
            txt = txt.rstrip() + "…"
        return txt, sz

    def _rrect(self, w, h, r, fill, outline=None, width=1.5):
        """A rounded rectangle sprite (design units), drawn at 2x and scaled down."""
        k = 2
        W2, H2 = int(self.p(w) * k), int(self.p(h) * k)
        img = _blank(W2, H2)
        ImageDraw.Draw(img).rounded_rectangle([0, 0, W2 - 1, H2 - 1], radius=self.p(r) * k, fill=fill,
                                              outline=outline, width=max(1, int(self.p(width) * k)) if outline else 0)
        return _down(img, self.p(w), self.p(h))

    def _chip(self, txt, colour, fill=(14, 18, 18, 235)):
        f = self.f("ui", 27)
        tw = f.getlength(txt) / self.s
        w, h = tw + 52, 60
        img = self._rrect(w, h, 30, fill, colour + (200,), 1.5)
        d = ImageDraw.Draw(img)
        d.text((self.p(w / 2), self.p(h / 2)), txt, font=f, fill=colour + (255,), anchor="mm")
        return img

    # ------------------------------------------------------------------ backdrop + ambient
    def _build_backdrop(self):
        W, H = self.W, self.H
        r = Image.radial_gradient("L").resize((W * 2, H * 2), Image.BILINEAR)
        r = r.crop((W // 2, int(H * 0.58), W // 2 + W, int(H * 0.58) + H))  # centre a bit above the middle
        bg = Image.composite(Image.new("RGB", (W, H), BG), Image.new("RGB", (W, H), CORE), r)
        d = ImageDraw.Draw(bg, "RGBA")
        step = self.p(45)
        x = (W % step) / 2
        while x < W:
            d.line([(x, 0), (x, H)], fill=(255, 255, 255, 9))
            x += step
        y = (H % step) / 2
        while y < H:
            d.line([(0, y), (W, y)], fill=(255, 255, 255, 9))
            y += step
        self.bg = bg
        # the drifting glow: a soft green disc, small and blurred, scaled up
        g = int(self.p(760))
        disc = Image.radial_gradient("L").resize((g, g), Image.BILINEAR)
        disc = ImageChops.invert(disc).point(lambda v: int((v / 255) ** 2.2 * 46))
        glow = Image.new("RGBA", (g, g), ACCENT + (0,))
        glow.putalpha(disc)
        self.glow = Sprite(glow)

    def _ambient(self, frame, t):
        a = 0.5 + 0.5 * math.sin(t / self.seconds * 2 * math.pi)
        cx = self.W * (0.5 + 0.18 * math.sin(t * 0.55))
        cy = self.H * (0.36 + 0.05 * math.cos(t * 0.4))
        self.glow.put(frame, cx - self.glow.w / 2, cy - self.glow.h / 2, 0.75 + 0.25 * a)

    # ------------------------------------------------------------------ the hero
    def logo_disc(self, logo, L, square=False):
        """The token's logo cut to a disc (or a rounded square) L design units across; a
        monogram in the accent when there is no logo."""
        k = 2
        n = int(self.p(L) * k)
        if logo is not None:
            im = logo.convert("RGBA")
            side = min(im.size)
            im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                          (im.width - side) // 2 + side, (im.height - side) // 2 + side)).resize((n, n), Image.LANCZOS)
        else:  # a monogram disc
            im = _vgrad(n, n, [(0, _mix(BG, self.accent, 0.25)), (1, _mix(BG, self.accent, 0.06))]).convert("RGBA")
            f = font("cond", n * 0.46)
            ImageDraw.Draw(im).text((n / 2, n / 2), (self.spec["symbol"][:2] or "?").upper(), font=f,
                                    fill=self.accent + (255,), anchor="mm")
        mask = Image.new("L", (n, n), 0)
        if square:
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, n - 1, n - 1], radius=n * 0.16, fill=255)
        else:
            ImageDraw.Draw(mask).ellipse([0, 0, n - 1, n - 1], fill=255)
        im.putalpha(ImageChops.multiply(im.getchannel("A"), mask))
        return _down(im, self.p(L), self.p(L))

    def _build_hero(self, logo):
        sp = self.spec
        L = 380  # logo diameter, design units
        k = 2
        self.logo = self.logo_disc(logo, L)
        self.logo_d = L
        # the ring: a hairline circle and a brighter arc that turns
        R = L + 56
        m = int(self.p(R) * k)
        ring = _blank(m, m)
        d = ImageDraw.Draw(ring)
        wd = max(2, int(self.p(2) * k))
        d.ellipse([wd, wd, m - wd, m - wd], outline=(255, 255, 255, 34), width=wd)
        d.arc([wd, wd, m - wd, m - wd], 200, 320, fill=ACCENT + (220,), width=wd * 2)
        d.arc([wd, wd, m - wd, m - wd], 20, 60, fill=ACCENT + (120,), width=wd * 2)
        self.ring = _down(ring, self.p(R), self.p(R))
        self.ring_d = R
        # halo under the logo
        h = int(self.p(L * 1.5))
        halo = Image.new("L", (h, h), 0)
        ImageDraw.Draw(halo).ellipse([h * 0.2, h * 0.2, h * 0.8, h * 0.8], fill=70)
        halo = halo.filter(ImageFilter.GaussianBlur(h * 0.09))
        hal = Image.new("RGBA", (h, h), ACCENT + (0,))
        hal.putalpha(halo)
        self.halo = Sprite(hal)

        # $TICKER in chrome
        tick, size = self._fit("$" + sp["symbol"], "cond", 168, 960, 84)
        self.ticker = self._chrome(tick, size)
        name, nsize = self._fit(sp["name"], "uir", 46, 900, 28)
        self.name_img, _ = self._text_sprite(name, "uir", nsize, DIM + (255,))
        # chips: what it trades against, where
        chips = [self._chip(f"PAIRED WITH {sp['pair']}", ACCENT)]
        chips.append(self._chip("GRADUATED" if self.grad else "ON THE BONDING CURVE", GOLD if self.grad else TEXT))
        gap = self.p(14)
        cw = sum(c.width for c in chips) + gap * (len(chips) - 1)
        row = _blank(cw, max(c.height for c in chips))
        x = 0
        for c in chips:
            row.alpha_composite(c, (int(x), 0))
            x += c.width + gap
        self.chips = Sprite(row)
        self._build_bar()

    def _build_bar(self, dim=DIM, faint=FAINT):
        """The header line of every scene (eyebrow left, the testnet badge right) and the mark
        in the corners until the end card."""
        sp = self.spec
        launchpad = sp.get("launchpad") or "vibe/vibe"
        self.eyebrow, _ = self._text_sprite(f"// {launchpad.upper()} · {sp.get('network', '').upper()}", "mono", 23,
                                            dim + (255,), 2)
        self.eyebrow = Sprite(self.eyebrow)
        self.badge = Sprite(self._chip("TESTNET", LIME, (20, 24, 6, 240))) if sp.get("testnet") else None
        self.mark, _ = self._text_sprite("made with Silverhand Buys", "ui", 25, faint + (255,), 1)
        self.mark = Sprite(self.mark)
        self.handle, _ = self._text_sprite(sp.get("bot", ""), "mono", 23, faint + (255,))
        self.handle = Sprite(self.handle)

    def _chrome(self, txt, size):
        """A chrome title: a vertical metal gradient through the glyphs, a dark rim and a soft
        drop shadow. Returns (RGBA image, face mask) for the glint."""
        f = self.f("cond", size)
        l, t, r, b = f.getbbox(txt)
        _, ct, _, cb = f.getbbox("H")
        pad = int(self.p(24))
        w, h = r - l + 2 * pad, max(b, cb) - min(t, ct) + 2 * pad
        ox, oy = pad - l, pad - min(t, ct)
        face = Image.new("L", (w, h), 0)
        ImageDraw.Draw(face).text((ox, oy), txt, font=f, fill=255)
        shadow = Image.new("L", (w, h), 0)
        ImageDraw.Draw(shadow).text((ox, oy + self.p(8)), txt, font=f, fill=150)
        shadow = shadow.filter(ImageFilter.GaussianBlur(self.p(10)))
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        img.paste((0, 0, 0, 255), (0, 0), shadow)
        img.putalpha(ImageChops.lighter(shadow, Image.new("L", (w, h), 0)))
        rim = face.filter(ImageFilter.MaxFilter(3))
        img.paste((40, 52, 56, 255), (0, 0), rim)
        metal = Image.new("RGB", (w, h), CHROME[-1][1])
        metal.paste(_vgrad(w, cb - ct + 2, CHROME), (0, oy + ct - 1))
        img.paste(metal, (0, 0), face)
        return img, face

    def _hero_layout(self, k):
        """The logo's centre and diameter (design units) at morph k: 0 the hero, 1 the header."""
        e = ease(k)
        return _lerp(DW / 2, HEAD_X + HEAD_D / 2, e), _lerp(HERO_Y, HEAD_Y, e), _lerp(self.logo_d, HEAD_D, e)

    def _header_sprites(self):
        """$TICKER and the name, small, for the header right of the logo."""
        if not hasattr(self, "_head"):
            tick, size = self._fit("$" + self.spec["symbol"], "cond", 76, DW - 2 * HEAD_X - HEAD_D - 30 - 40, 40)
            img, _ = self._chrome(tick, size)
            txt, sz = self._fit(self.spec["name"], "uir", 30, DW - 2 * HEAD_X - HEAD_D - 30, 20)
            self._head = Sprite(img), Sprite(self._text_sprite(txt, "uir", sz, DIM + (255,))[0])
        return self._head

    def _draw_hero(self, frame, t):
        """The logo glides from the hero into the header (the one shared element); the big
        ticker, name and chips lift away while it moves, and the header's small ticker and
        name rise in beside it as it lands: a crossfade, never a cut."""
        k = _seg(t, T_HERO_OUT, SLOW)
        fade = 1 - ease_in(_seg(t, T_END - 0.05, QUICK))  # the header leaves with the stats
        if fade <= 0:
            return
        cx, cy, d = self._hero_layout(k)
        out = ease_in(_seg(t, T_HERO_OUT, QUICK))
        # halo and ring around the logo (secondary), the ring turning slowly; they go first
        if out < 1:
            ha = (1 - out) * (0.85 + 0.15 * math.sin(t * 2.1))
            self.halo.put(frame, self.p(cx) - self.halo.w / 2, self.p(cy) - self.halo.h / 2, ha)
            ring = self.ring.rotate(-t * 38, resample=Image.BICUBIC)
            if k > 0:  # it shrinks with the logo while it fades
                rp = max(1, int(ring.width * d / self.logo_d))
                ring = ring.resize((rp, rp), Image.BILINEAR)
            Sprite(ring).put(frame, self.p(cx) - ring.width / 2, self.p(cy) - ring.height / 2, 1 - out)
        # the logo: a slow breath in the hero (ambient), then the glide
        breathe = 1 + 0.012 * math.sin(t * 1.7) * (1 - k)
        px = max(1, int(self.p(d * breathe)))
        logo = self.logo if px == self.logo.width else self.logo.resize((px, px), Image.LANCZOS)
        Sprite(logo).put(frame, self.p(cx) - px / 2, self.p(cy) - px / 2, fade)
        # the big ticker (a glint across it once), the name and the chips: lift away
        if out < 1:
            img, face = self.ticker
            g = _seg(t, 0.5, 1.0)
            if 0 < g < 1:
                img = self._glinted(img, face, ease(g))
            y = HERO_Y + self.logo_d / 2 + 50
            lift = -16 * out
            Sprite(img).put(frame, (self.W - img.width) / 2, self.p(y + lift), 1 - out)
            y += self.ticker[0].height / self.s - 14
            nm = Sprite(self.name_img)
            nm.put(frame, (self.W - nm.w) / 2, self.p(y + lift), 1 - out)
            self.chips.put(frame, (self.W - self.chips.w) / 2, self.p(y + (84 if self.spec["name"] else 24) + lift),
                           1 - out)
        # the header: small ticker and name rise in as the logo lands
        inn = ease_out(_seg(t, T_HERO_OUT + 0.45, MOVE))
        if inn > 0:
            tick, name = self._header_sprites()
            x = HEAD_X + HEAD_D + 30
            rise = 24 * (1 - inn)
            ty = HEAD_Y - tick.h / self.s / 2 - 14
            tick.put(frame, self.p(x - 22), self.p(ty + rise), inn * fade)
            name.put(frame, self.p(x), self.p(ty + tick.h / self.s - 22 + rise), inn * fade)

    def _glinted(self, img, face, p):
        """A soft diagonal band of light across the chrome face, p from 0 to 1."""
        w, h = face.size
        band = Image.new("L", (w, h), 0)
        x = -0.3 * w + 1.6 * w * p
        bw = max(8, w * 0.07)
        ImageDraw.Draw(band).polygon([(x, 0), (x + bw, 0), (x + bw - h * 0.5, h), (x - h * 0.5, h)], fill=200)
        band = ImageChops.multiply(band.filter(ImageFilter.GaussianBlur(bw * 0.35)), face)
        out = img.copy()
        out.paste((255, 255, 255, 255), (0, 0), band)
        out.putalpha(img.getchannel("A"))
        return out

    # ------------------------------------------------------------------ the stats
    def _build_stats(self):
        sp = self.spec
        # the curve panel
        self.panel_box = (70, 330, DW - 70, 700)
        x0, y0, x1, y1 = self.panel_box
        panel = self._rrect(x1 - x0, y1 - y0, 26, PANEL + (240,), LINE + (255,), 1.5)
        d = ImageDraw.Draw(panel)
        d.text((self.p(40), self.p(64)), "BONDING CURVE", font=self.f("mono", 25), fill=DIM + (255,), anchor="ls")
        right = "GRADUATED" if self.grad else (f"TARGET {compact(sp['target'])} {sp['pair']}" if sp.get("target") else "")
        d.text((self.p(x1 - x0 - 40), self.p(64)), right, font=self.f("mono", 25),
               fill=(GOLD if self.grad else FAINT) + (255,), anchor="rs")
        # the bar's track
        self.bar = (x0 + 40, y0 + 250, x1 - 40, y0 + 278)
        bx0, by0, bx1, by1 = self.bar
        ImageDraw.Draw(panel).rounded_rectangle(
            [self.p(bx0 - x0), self.p(by0 - y0), self.p(bx1 - x0), self.p(by1 - y0)],
            radius=self.p(14), fill=(26, 32, 31, 255))
        line = self.curve_line()
        d.text((self.p(40), self.p(y1 - y0 - 40)), line, font=self.f("uir", 31), fill=DIM + (255,), anchor="ls")
        self.panel = Sprite(panel)
        self.bar_fill = self._bar_fill()
        # the tiles: what is known, in a 2x2 grid
        tiles = self.stat_items()
        self.tiles = []
        tw, th, gap = (DW - 140 - 24) / 2, 220, 24
        for i, (label, value, fmt, sub) in enumerate(tiles[:4]):
            col, row = i % 2, i // 2
            x = 70 + col * (tw + gap)
            y = 730 + row * (th + gap)
            img = self._rrect(tw, th, 22, PANEL + (240,), LINE + (255,), 1.5)
            d = ImageDraw.Draw(img)
            d.text((self.p(30), self.p(52)), label, font=self.f("mono", 24), fill=DIM + (255,), anchor="ls")
            if value is None:  # the pair tile: the asset's symbol is the big text
                txt, sz = self._fit(sub, "cond", 84, tw - 60, 40)
                d.text((self.p(30), self.p(160)), txt, font=self.f("cond", sz), fill=ACCENT + (255,), anchor="ls")
            elif sub:
                txt, sz = self._fit(sub, "mono", 26, tw - 60, 18)
                d.text((self.p(30), self.p(192)), txt, font=self.f("mono", sz), fill=FAINT + (255,), anchor="ls")
            self.tiles.append(dict(sprite=Sprite(img), x=x, y=y, w=tw, value=value, fmt=fmt))

    def _bar_fill(self):
        bx0, by0, bx1, by1 = self.bar
        w, h = bx1 - bx0, by1 - by0
        c0, c1 = ((180, 140, 40), GOLD) if self.grad else ((10, 120, 74), ACCENT)
        k = 2
        W2, H2 = int(self.p(w) * k), int(self.p(h) * k)
        g = Image.new("RGB", (2, 1))
        g.putpixel((0, 0), c0)
        g.putpixel((1, 0), c1)
        g = g.resize((W2, H2), Image.BILINEAR).convert("RGBA")
        m = Image.new("L", (W2, H2), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, W2 - 1, H2 - 1], radius=self.p(14) * k, fill=255)
        g.putalpha(m)
        return _down(g, self.p(w), self.p(h))

    def _draw_stats(self, frame, t):
        if t < T_STATS:
            return
        gone = ease_in(_seg(t, T_END, QUICK))
        if gone >= 1:
            return
        lift = -30 * gone
        x0, y0, x1, y1 = self.panel_box
        e = ease_out(_seg(t, T_STATS, MOVE))
        a = e * (1 - gone)
        dy = 24 * (1 - e) + lift
        self.panel.put(frame, self.p(x0), self.p(y0 + dy), a)
        # the % and the bar fill count up together
        c = ease(_seg(t, T_COUNT, 1.1))
        v = self.prog * c
        d = ImageDraw.Draw(frame, "RGBA")
        col = GOLD if self.grad else TEXT
        _text(frame, (self.p(x0 + 38), self.p(y0 + 205 + dy)), pct(v), self.f("num", 120), col, a)
        bx0, by0, bx1, by1 = self.bar
        fw = int(self.bar_fill.width * v)
        if fw > 2:
            fill = self.bar_fill.crop((0, 0, fw, self.bar_fill.height))
            # a light at the tip while it moves (secondary)
            Sprite(fill).put(frame, self.p(bx0), self.p(by0 + dy), a)
            if 0 < c < 1:
                tip = self.p(bx0) + fw
                d.ellipse([tip - self.p(18), self.p(by0 + dy) - self.p(4), tip + self.p(2),
                           self.p(by1 + dy) + self.p(4)], fill=(255, 255, 255, int(70 * a)))
        # tiles: rise in a short stagger, numbers count up
        for i, tile in enumerate(self.tiles):
            t0 = T_TILES + i * STAGGER
            e = ease_out(_seg(t, t0, MOVE))
            if e <= 0:
                continue
            a = e * (1 - gone)
            dy = 24 * (1 - e) + lift
            tile["sprite"].put(frame, self.p(tile["x"]), self.p(tile["y"] + dy), a)
            if tile["value"] is not None:
                c = ease(_seg(t, t0 + 0.1, 0.9))
                txt = tile["fmt"](tile["value"] * c)
                f = self.f("num", 64)
                while f.getlength(txt) > self.p(tile["w"] - 60) and f.size > 20:
                    f = font("num", f.size - 4)
                _text(frame, (self.p(tile["x"] + 30), self.p(tile["y"] + 140 + dy)), txt, f, TEXT, a)

    # ------------------------------------------------------------------ the end card
    def _build_end(self):
        sp = self.spec
        n = int(self.p(150) * 2)
        with Image.open(AVATAR) as im:
            av = im.convert("RGBA").resize((n, n), Image.LANCZOS)
        m = Image.new("L", (n, n), 0)
        ImageDraw.Draw(m).ellipse([0, 0, n - 1, n - 1], fill=255)
        av.putalpha(m)
        self.avatar = Sprite(_down(av, self.p(150), self.p(150)))
        img, _ = self._chrome("SILVERHAND BUYS", 112)
        self.brand = Sprite(img)
        self.made, _ = self._text_sprite("made with Silverhand Buys", "uir", 30, DIM + (255,))
        self.made = Sprite(self.made)
        # the call to action
        bot = sp.get("bot", "")
        f = self.f("ui", 34)
        line1 = "Every buy, live in your Telegram group"
        txt = f"Add {bot} to your group" if bot else "Add the bot to your group"
        w = max(f.getlength(txt) / self.s + 80, 640)
        cta = self._rrect(w, 96, 48, _mix(BG, self.accent, 0.12) + (245,), self.accent + (255,), 2)
        ImageDraw.Draw(cta).text((self.p(w / 2), self.p(48)), txt, font=f, fill=self.accent + (255,), anchor="mm")
        self.cta = Sprite(cta)
        self.line1, _ = self._text_sprite(line1, "ui", 36, TEXT + (255,))
        self.line1 = Sprite(self.line1)
        foot = f"{sp.get('network', '')} · data from the chain · NFA".strip(" ·")
        self.foot, _ = self._text_sprite(foot, "mono", 22, FAINT + (255,), 1)
        self.foot = Sprite(self.foot)

    def _draw_end(self, frame, t):
        base = self.t_end + 0.3  # once the scene before is nearly gone: no muddy overlap
        if t < base:
            return
        items = [(self.avatar, 420), (self.brand, 590), (self.made, 740), (self.line1, 880), (self.cta, 940)]
        for i, (spr, y) in enumerate(items):
            e = ease_out(_seg(t, base + i * STAGGER, MOVE))
            if e > 0:
                spr.put(frame, (self.W - spr.w) / 2, self.p(y + 24 * (1 - e)), e)
        e = ease_out(_seg(t, base + 0.4, MOVE))
        self.foot.put(frame, (self.W - self.foot.w) / 2, self.p(DH - 90), e)

    # ------------------------------------------------------------------ chrome of every frame
    def _draw_bar(self, frame, t):
        self.eyebrow.put(frame, self.p(70), self.p(70), 1.0)
        if self.badge:
            self.badge.put(frame, self.W - self.p(70) - self.badge.w, self.p(58), 1.0)
        out = 1 - ease_in(_seg(t, self.t_end, QUICK))
        self.mark.put(frame, self.p(70), self.p(DH - 84), out)
        self.handle.put(frame, self.W - self.p(70) - self.handle.w, self.p(DH - 84), out)

    def frame(self, t):
        f = self.bg.copy()
        self._ambient(f, t)
        self._draw_hero(f, t)
        self._draw_stats(f, t)
        self._draw_end(f, t)
        self._draw_bar(f, t)
        return f


def video(spec: dict, logo, style: str = "dashboard", **kw):
    """The Video of a style (promo_styles.STYLES); an unknown style is the dashboard."""
    from .promo_styles import STYLES
    return STYLES.get(style, Video)(spec, logo, **kw)


def render(spec: dict, logo: Image.Image | None = None, size=SIZE, fps: int = FPS, seconds: float = SECONDS,
           crf: int = 22, preset: str = "veryfast", stats: dict | None = None, style: str = "dashboard") -> bytes:
    """MP4 bytes in a style (promo_styles.STYLES): H.264 High, yuv420p, `fps`, no audio,
    +faststart (X takes it as is)."""
    t0 = time.perf_counter()
    v = video(spec, logo, style, size=size, fps=fps, seconds=seconds)
    n = int(round(seconds * fps))
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    W, H = v.W, v.H
    # -thread_queue_size 1: ffmpeg 7 queues 8 raw frames from the pipe by default (~10 MB here)
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-thread_queue_size", "1",
           "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-an", "-c:v", "libx264", "-preset", preset,
           "-profile:v", "high", "-crf", str(crf), "-pix_fmt", "yuv420p", "-threads", "1",
           "-x264-params", "rc-lookahead=0:ref=1:bframes=0",  # few frame buffers: little memory
           "-movflags", "+faststart", path]
    err = []
    try:
        # Keep ffmpeg below normal priority so other services retain CPU time.
        nice = {"preexec_fn": lambda: os.nice(10)} if hasattr(os, "nice") else {}
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, **nice)
        th = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
        th.start()
        try:
            for i in range(n):
                im = v.frame(i / fps)
                try:
                    proc.stdin.write(im.tobytes())
                finally:
                    im.close()
            proc.stdin.close()
            rc = proc.wait(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            try:
                proc.stdin.close()
            except OSError:
                pass
            th.join(5)
            proc.stderr.close()
        if rc != 0:
            raise RuntimeError((err[0] if err else b"").decode(errors="replace")[-500:])
        data = Path(path).read_bytes()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if stats is not None:
        stats.update(wall=time.perf_counter() - t0, frames=n, bytes=len(data))
    return data


def still(spec: dict, logo: Image.Image | None = None, t: float = 0.0, size=SIZE,
          style: str = "dashboard") -> Image.Image:
    return video(spec, logo, style, size=size).frame(t)
