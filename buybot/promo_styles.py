"""The /promo video's other looks. Each style tells the same facts (promo.gather's spec) in
its own design and ends on the same card ("made with Silverhand Buys", add the bot):

  dashboard  panels and tiles, the bot's green (promo_video.Video)
  kinetic    huge condensed type, one fact a beat, word by word (SilverhandMedia's kinetic format)
  terminal   a terminal window: the facts print under the command, an ASCII curve bar fills
  ticket     a paper token report on black: rows fill in, a highlighter, a stamp
  gauge      silver signal: a chrome title band, then a dial whose needle sweeps to the curve %
  lime       a big ring around the logo fills to the curve %, pills with the facts

Same rules as promo_video: Pillow and ffmpeg only, 640x800, the Premium motion (one curve,
350-600 ms, rise-and-fade entrances, ease-in exits, no overshoot), frame 0 a full cover,
crossfades, still parts drawn once as sprites.
"""

import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import promo_video as pv
from .promo import STYLE_NAMES
from .card import font
from .promo_video import (DIM, DW, FAINT, GOLD, LINE, MOVE, QUICK, STAGGER, TEXT, Sprite, _blank,
                          _down, _lerp, _mix, _seg, _text, _vgrad, compact, ease, ease_in, ease_out, pct)

RED = (226, 72, 64)
LIME = (223, 249, 2)
SIGNAL = (96, 165, 250)


def backdrop(W, H, s, core, edge, grid=(255, 255, 255, 8), step=45, cy=0.42):
    """A near-black backdrop: a soft lift around (0.5, cy) and a faint grid."""
    r = Image.radial_gradient("L").resize((W * 2, H * 2), Image.BILINEAR)
    r = r.crop((W // 2, int(H * (1 - cy)), W // 2 + W, int(H * (1 - cy)) + H))
    bg = Image.composite(Image.new("RGB", (W, H), edge), Image.new("RGB", (W, H), core), r)
    if grid:
        d = ImageDraw.Draw(bg, "RGBA")
        st = step * s
        x = (W % st) / 2
        while x < W:
            d.line([(x, 0), (x, H)], fill=grid)
            x += st
        y = (H % st) / 2
        while y < H:
            d.line([(0, y), (W, y)], fill=grid)
            y += st
    return bg


def sans(txt: str) -> str:
    """Instrument Sans has no "≈": an approximate dollar sign becomes "~" in it."""
    return txt.replace("≈", "~")


class Style(pv.Video):
    """A style: build() draws its still parts and its backdrop (self.bg), scene() a frame of its
    own scenes; the header line, the corner mark and the end card are shared."""
    bar_colours = (DIM, FAINT)

    def __init__(self, spec, logo, size=pv.SIZE, fps=pv.FPS, seconds=pv.SECONDS):
        self._setup(spec, size, fps, seconds)
        self.build(logo)
        self._build_bar(*self.bar_colours)
        self._build_end()

    def build(self, logo):
        raise NotImplementedError

    def scene(self, frame, t):
        raise NotImplementedError

    def leaving(self, t):
        """0 -> 1 as the style's scenes leave for the end card (ease-in, quick)."""
        return ease_in(_seg(t, self.t_end, QUICK))

    def frame(self, t):
        f = self.bg.copy()
        self.scene(f, t)
        self._draw_end(f, t)
        self._draw_bar(f, t)
        return f

    # shared pieces
    def ring_sprite(self, d, width, colour):
        """A ring d units across, `width` thick, drawn at 2x."""
        k = 2
        n = int(self.p(d) * k)
        im = _blank(n, n)
        w = int(self.p(width) * k)
        ImageDraw.Draw(im).ellipse([w / 2, w / 2, n - w / 2, n - w / 2], outline=colour + (255,), width=w)
        return Sprite(_down(im, self.p(d), self.p(d)))

    def put_arc(self, frame, ring: Sprite, x, y, start, end, a=1.0):
        """Paste the part of a ring sprite between two angles (degrees, clockwise from 3 o'clock)."""
        if end <= start or a <= 0.01:
            return
        m = Image.new("L", (ring.w, ring.h), 0)
        ImageDraw.Draw(m).pieslice([-2, -2, ring.w + 2, ring.h + 2], start, end, fill=int(255 * a))
        frame.paste(ring.rgb, (int(x), int(y)), ImageChops.multiply(ring.a, m))

    def dot_sprite(self, d, colour):
        k = 4
        n = max(2, int(self.p(d) * k))
        im = _blank(n, n)
        ImageDraw.Draw(im).ellipse([0, 0, n - 1, n - 1], fill=colour + (255,))
        return Sprite(_down(im, self.p(d), self.p(d)))

    def glow_sprite(self, d, colour, strength=46):
        g = int(self.p(d))
        disc = ImageChops.invert(Image.radial_gradient("L").resize((g, g), Image.BILINEAR))
        disc = disc.point(lambda v: int((v / 255) ** 2.2 * strength))
        im = Image.new("RGBA", (g, g), colour + (0,))
        im.putalpha(disc)
        return Sprite(im)

    def plain(self, txt, role, size, colour, max_w=None, min_size=None, tracking=0.0):
        if max_w:
            txt, size = self._fit(txt, role, size, max_w, min_size or size * 0.5)
        return Sprite(self._text_sprite(txt, role, size, colour + (255,), tracking)[0])


# ----------------------------------------------------------------------------- kinetic
class Kinetic(Style):
    """Huge condensed type, one fact a beat: $TICKER, the curve %, then each number with its word.
    The new beat rises in as the old one lifts away; a bar under the number grows with it."""
    preview_at = 2.45
    BG = (4, 4, 5)
    FIRST = 1.5     # the ticker's beat (the cover) holds this long

    def build(self, logo):
        sp = self.spec
        hi = GOLD if self.grad else self.accent
        self.hi = hi
        self.bg = backdrop(self.W, self.H, self.s, (14, 15, 17), self.BG, (255, 255, 255, 7))
        self.logo = Sprite(self.logo_disc(logo, 170, square=True))
        tick, size = self._fit("$" + sp["symbol"], "cond", 290, DW - 140, 120)
        self.tick = Sprite(self._chrome(tick, size)[0])
        self.name = self.plain(sp["name"], "uir", 50, DIM, DW - 140, 30) if sp["name"] else None
        self.pair = Sprite(self._chip(f"PAIRED WITH {sp['pair']}", self.accent))
        # the beats after the ticker: (big text or a value to count, its format, the words under it)
        beats = []
        if self.grad:
            beats.append(("GRADUATED", None, None, "TO UNISWAP V4"))
        else:
            beats.append((None, self.prog, pct, self.curve_caption()))
        for label, value, fmt, _ in self.stat_items():
            if value is not None:
                word = {"MARKET CAP": "MARKET CAP", "HOLDERS": "HOLDERS", "BUYS · 24H": "BUYS IN 24 H"}[label]
                beats.append((None, value, fmt, word))
        self.beats = []
        for big, value, fmt, word in beats:
            final = big if big is not None else sans(fmt(value))
            _, size = self._fit(final, "cond", 300, DW - 140, 110)
            self.beats.append(dict(big=big, value=value, fmt=fmt, size=size,
                                   word=self.plain(word, "cond", 92, TEXT, DW - 140, 50),
                                   width=self.f("cond", size).getlength(final) / self.s))
        self.beat_len = (self.t_end - self.FIRST) / max(1, len(self.beats))

    def _beat_alpha(self, t, t0, t1):
        """(in, out) of a beat shown from t0 to t1; the first beat is on screen at frame 0."""
        e = 1.0 if t0 <= 0 else ease_out(_seg(t, t0 + 0.22, 0.45))
        o = ease_in(_seg(t, t1, 0.25))
        return e, o

    def scene(self, frame, t):
        # beat 0: logo, ticker, name, the pair
        e, o = self._beat_alpha(t, 0, self.FIRST)
        if o < 1:
            a, dy = e * (1 - o), -40 * o
            self.logo.put(frame, self.p(70), self.p(330 + dy), a)
            self.tick.put(frame, self.p(70 - 24), self.p(560 + dy), a)
            y = 560 + self.tick.h / self.s - 20
            if self.name:
                self.name.put(frame, self.p(70), self.p(y + dy), a)
                y += 80
            self.pair.put(frame, self.p(70), self.p(y + dy), a)
        # the other beats
        n = len(self.beats)
        for i, b in enumerate(self.beats):
            t0 = self.FIRST + i * self.beat_len
            t1 = t0 + self.beat_len if i < n - 1 else self.t_end
            if t < t0 or t > t1 + 0.35:
                continue
            e, o = self._beat_alpha(t, t0, t1)
            a = e * (1 - o)
            if a <= 0.01:
                continue
            dy = 50 * (1 - e) - 40 * o
            if b["big"] is not None:
                txt = b["big"]
            else:
                txt = sans(b["fmt"](b["value"] * ease(_seg(t, t0 + 0.22, 0.7))))
            base = 760
            _text(frame, (self.p(70), self.p(base + dy)), txt, self.f("cond", b["size"]), self.hi if i == 0 else TEXT, a)
            # the bar under the number grows as it lands (secondary)
            g = ease(_seg(t, t0 + 0.2, 0.5))
            bw = b["width"] * g
            if bw > 2:
                ImageDraw.Draw(frame).rectangle(
                    [self.p(70), self.p(base + 34 + dy), self.p(70 + bw), self.p(base + 44 + dy)],
                    fill=_mix(pv.BG, self.hi, a))
            b["word"].put(frame, self.p(70), self.p(base + 70 + dy), a)
        # ambient: the beat counter and a thin line through the beats
        total = n + 1
        k = 0 if t < self.FIRST else min(n, int((t - self.FIRST) / self.beat_len) + 1)
        out = 1 - self.leaving(t)
        _text(frame, (self.p(70), self.p(1200)), f"{k + 1:02d} / {total:02d}", self.f("mono", 24), FAINT, out)
        prog = min(1.0, t / self.t_end)
        d = ImageDraw.Draw(frame)
        d.rectangle([self.p(230), self.p(1191), self.p(DW - 70), self.p(1193)], fill=_mix(self.BG, LINE, out))
        d.rectangle([self.p(230), self.p(1191), self.p(_lerp(230, DW - 70, prog)), self.p(1193)],
                    fill=_mix(self.BG, self.hi, out))


# ----------------------------------------------------------------------------- terminal
class Terminal(Style):
    """A terminal window: `silverhand promo 0x…` (typed already on the cover), then the facts
    print line by line, the curve as an ASCII bar that fills; the cursor blinks."""
    preview_at = 4.6
    WIN = (50, 300, DW - 50, 1170)
    LH = 64          # line height
    CELLS = 16       # the ASCII bar

    def build(self, logo):
        sp = self.spec
        self.bg = backdrop(self.W, self.H, self.s, (16, 18, 20), (6, 7, 8), (255, 255, 255, 7))
        x0, y0, x1, y1 = self.WIN
        win = self._rrect(x1 - x0, y1 - y0, 22, (14, 16, 18, 250), (48, 54, 58, 255), 1.5)
        d = ImageDraw.Draw(win)
        d.line([(0, self.p(64)), (win.width, self.p(64))], fill=(40, 45, 48, 255), width=max(1, int(self.p(1.5))))
        for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            cx = self.p(34 + i * 34)
            r = self.p(9)
            d.ellipse([cx - r, self.p(32) - r, cx + r, self.p(32) + r], fill=c + (255,))
        d.text((win.width / 2, self.p(32)), "silverhand@buys: ~/promo", font=self.f("mono", 24),
               fill=DIM + (255,), anchor="mm")
        self.win = Sprite(win)
        tick, size = self._fit("$" + sp["symbol"], "cond", 150, DW - 140 - 160, 70)
        self.tick = Sprite(self._chrome(tick, size)[0])
        self.logo = Sprite(self.logo_disc(logo, 140, square=True))
        tok = sp["token"]
        self.cmd = f"silverhand promo {tok[:6]}…{tok[-4:]}"
        mc = None
        for label, value, fmt, sub in self.stat_items():
            if label == "MARKET CAP":
                mc = fmt(value) + (f"  ({sub})" if sub else "")
        rows = [("pair", sp["pair"], TEXT)]
        rows.append(("curve", None, None))  # the bar
        if not self.grad:
            rows.append(("", self.curve_line(), DIM))
        if mc:
            rows.append(("mcap", mc, TEXT))
        if sp.get("holders") is not None:
            rows.append(("holders", f"{sp['holders']:,}", TEXT))
        if sp.get("buys_24h") is not None:
            rows.append(("buys 24h", f"{sp['buys_24h']:,}", TEXT))
        rows.append(("status", "GRADUATED · UNISWAP V4" if self.grad else "ON THE BONDING CURVE",
                     GOLD if self.grad else self.accent))
        self.rows = rows
        self.mono = self.f("mono", 34)

    def scene(self, frame, t):
        out = self.leaving(t)
        a0, dy = 1 - out, -30 * out
        if a0 <= 0.01:
            return
        x0, y0, x1, y1 = self.WIN
        self.tick.put(frame, self.p(70 - 24), self.p(118 + dy), a0)
        self.logo.put(frame, self.p(DW - 70 - 140), self.p(130 + dy), a0)
        self.win.put(frame, self.p(x0), self.p(y0 + dy), a0)
        f = self.mono
        x = self.p(x0 + 40)
        y = y0 + 64 + 76
        # the command is typed already on the cover; the output prints after it
        n = len(self.cmd)
        _text(frame, (x, self.p(y + dy)), "$", f, self.accent, a0)
        _text(frame, (x + self.p(34), self.p(y + dy)), self.cmd[:n], f, TEXT, a0)
        typing = n < len(self.cmd)
        col = self.p(220)
        last = y
        for i, (label, value, colour) in enumerate(self.rows):
            t0 = 0.5 + i * 0.24
            e = ease_out(_seg(t, t0, 0.3))
            if e <= 0:
                break
            ly = y + (i + 1) * self.LH + 8 * (1 - e)
            last = y + (i + 1) * self.LH
            a = e * a0
            if label:
                _text(frame, (x, self.p(ly + dy)), label, f, FAINT, a)
            if value is None:  # the curve: an ASCII bar that fills, then the %
                c = ease(_seg(t, t0 + 0.1, 0.9))
                full = int(round(self.CELLS * self.prog * c))
                bar = "█" * full + "░" * (self.CELLS - full)
                _text(frame, (x + col, self.p(ly + dy)), bar, f, GOLD if self.grad else self.accent, a)
                _text(frame, (x + col + f.getlength(bar) + self.p(24), self.p(ly + dy)), pct(self.prog * c), f,
                      TEXT, a)
            else:
                _text(frame, (x + col if label else x + col, self.p(ly + dy)), value, f, colour, a)
        # the prompt again, with a blinking cursor (ambient)
        if not typing and t > 0.5 + len(self.rows) * 0.24 + 0.2:
            py = last + self.LH * 1.4
            _text(frame, (x, self.p(py + dy)), "$", f, self.accent, a0)
        else:
            py = y
        blink = (t * 2) % 2 < 1.2
        if blink:
            cx = x + self.p(34) + (f.getlength(self.cmd[:n]) if py == y else 0)
            ImageDraw.Draw(frame).rectangle([cx + self.p(4), self.p(py - 30 + dy), cx + self.p(22), self.p(py + 6 + dy)],
                                            fill=_mix((14, 16, 18), TEXT, a0 * 0.85))


# ----------------------------------------------------------------------------- ticket
class Ticket(Style):
    """A paper token report on black: its header is there at once, the rows fill in like a form,
    a highlighter marks the curve, a stamp lands."""
    preview_at = 4.8
    PAPER = (240, 234, 220)
    INK = (24, 24, 26)
    BOX = (110, 230, DW - 110, 1190)

    def build(self, logo):
        sp = self.spec
        self.bg = backdrop(self.W, self.H, self.s, (16, 17, 19), (4, 4, 5), (255, 255, 255, 7))
        x0, y0, x1, y1 = self.BOX
        w, h = x1 - x0, y1 - y0
        k = 2
        W2, H2 = int(self.p(w) * k), int(self.p(h) * k)
        paper = _vgrad(W2, H2, [(0, (246, 241, 229)), (1, self.PAPER)]).convert("RGBA")
        m = Image.new("L", (W2, H2), 255)
        md = ImageDraw.Draw(m)
        tooth = self.p(18) * k  # a torn, perforated top edge
        xx = 0.0
        while xx < W2:
            md.polygon([(xx, 0), (xx + tooth, 0), (xx + tooth / 2, tooth * 0.6)], fill=0)
            xx += tooth
        paper.putalpha(m)
        paper = _down(paper, self.p(w), self.p(h))
        d = ImageDraw.Draw(paper)
        ink, faint = self.INK + (255,), (120, 116, 108, 255)
        d.text((self.p(56), self.p(80)), "SILVERHAND BUYS · TOKEN REPORT", font=self.f("mono", 22), fill=faint,
               anchor="ls")
        status = "STATUS: GRADUATED" if self.grad else "STATUS: ON CURVE"
        d.text((self.p(w - 56), self.p(80)), status, font=self.f("mono", 22), fill=RED + (255,), anchor="rs")
        tick, size = self._fit("$" + sp["symbol"], "cond", 150, w - 112 - 190, 70)
        d.text((self.p(56), self.p(230)), tick, font=self.f("cond", size), fill=ink, anchor="ls")
        if sp["name"]:
            nm, ns = self._fit(sp["name"], "uir", 36, w - 112 - 190, 22)
            d.text((self.p(56), self.p(290)), nm, font=self.f("uir", ns), fill=(70, 68, 64, 255), anchor="ls")
        d.line([(self.p(56), self.p(330)), (self.p(w - 56), self.p(330))], fill=(200, 194, 180, 255),
               width=max(1, int(self.p(2))))
        lg = self.logo_disc(logo, 150, square=True)
        paper.alpha_composite(lg, (int(self.p(w - 56 - 150)), int(self.p(118))))
        # the shadow under the paper
        pad = int(self.p(40))
        sh = Image.new("L", (paper.width + 2 * pad, paper.height + 2 * pad), 0)
        ImageDraw.Draw(sh).rectangle([pad, pad + self.p(16), pad + paper.width, pad + paper.height + self.p(16)],
                                     fill=150)
        sh = sh.filter(ImageFilter.GaussianBlur(self.p(18)))
        full = Image.new("RGBA", sh.size, (0, 0, 0, 0))
        full.paste((0, 0, 0, 255), (0, 0), sh)
        full.putalpha(sh)
        full.alpha_composite(paper, (pad, pad))
        self.paper = Sprite(full)
        self.pad = pad
        # the rows: label, value, highlighted
        rows = [("PAIR", sp["pair"], False)]
        rows.append(("CURVE", "GRADUATED · 100%" if self.grad else f"{pct(self.prog)} {self.curve_word()}", True))
        line = self.curve_line()
        if not self.grad and line.endswith((" sold", " raised")):  # "765M of 793M tokens sold"
            rows.append((self.curve_word(), line.rsplit(" ", 1)[0].replace(" tokens", ""), False))
        for label, value, fmt, sub in self.stat_items():
            if value is not None:
                rows.append((label.replace(" · ", " "), sans(fmt(value)), False))
        self.rows = rows
        # the stamp
        word = "GRADUATED" if self.grad else "ON CURVE"
        f = self.f("cond", 96)
        tw = f.getlength(word)
        st = _blank(tw + self.p(70), self.p(150))
        sd = ImageDraw.Draw(st)
        colour = (40, 150, 90) if self.grad else RED
        sd.rounded_rectangle([self.p(6), self.p(6), st.width - self.p(6), st.height - self.p(6)], radius=self.p(14),
                             outline=colour + (230,), width=max(2, int(self.p(7))))
        sd.text((st.width / 2, st.height / 2), word, font=f, fill=colour + (230,), anchor="mm")
        self.stamp = st.rotate(9, resample=Image.BICUBIC, expand=True)

    def scene(self, frame, t):
        out = self.leaving(t)
        a0, dy = 1 - out, -40 * out
        if a0 <= 0.01:
            return
        x0, y0, x1, y1 = self.BOX
        self.paper.put(frame, self.p(x0) - self.pad, self.p(y0 + dy) - self.pad, a0)
        lab, val = self.f("mono", 24), self.f("ui", 38)
        for i, (label, value, mark) in enumerate(self.rows):
            t0 = 0.6 + i * 0.28
            if t < t0:
                break
            y = y0 + 410 + i * 76
            la = ease_out(_seg(t, t0, 0.3)) * a0
            _text(frame, (self.p(x0 + 56), self.p(y + dy)), label, lab, (120, 116, 108), la)
            # a dotted leader to the value
            d = ImageDraw.Draw(frame)
            xx = x0 + 56 + 230
            while xx < x1 - 56 - 330:
                d.ellipse([self.p(xx), self.p(y - 4 + dy), self.p(xx + 3), self.p(y - 1 + dy)],
                          fill=_mix(self.PAPER, (170, 164, 150), la))
                xx += 14
            n = int(len(value) * _seg(t, t0 + 0.1, 0.3))  # the value is written in
            right = self.p(x1 - 56)
            txt = value[:n]
            if mark and n:
                g = ease(_seg(t, t0 + 0.45, 0.45))
                full_w = val.getlength(value)
                if g > 0:
                    d.rectangle([right - full_w - self.p(10), self.p(y - 40 + dy),
                                 right - full_w - self.p(10) + (full_w + self.p(20)) * g, self.p(y + 10 + dy)],
                                fill=_mix(self.PAPER, (255, 222, 80), 0.85 * a0))
            if txt:
                _text(frame, (right - val.getlength(value), self.p(y + dy)), txt, val, self.INK, a0)
        # the stamp lands once the rows are in
        ts = 0.6 + len(self.rows) * 0.28 + 0.35
        e = ease_out(_seg(t, ts, QUICK))
        if e > 0:
            sc = 1.12 - 0.12 * e
            w, h = int(self.stamp.width * sc), int(self.stamp.height * sc)
            st = self.stamp.resize((max(1, w), max(1, h)), Image.BILINEAR)
            cx, cy = self.p(x0 + 300), self.p(y1 - 75 + dy)
            Sprite(st).put(frame, cx - w / 2, cy - h / 2, e * a0)


# ----------------------------------------------------------------------------- gauge
class Gauge(Style):
    """silver signal: a chrome title band (the cover), then a dial whose needle sweeps to the
    curve %, and a market panel with the counts."""
    preview_at = 4.6
    accent = SIGNAL
    NAVY = (6, 9, 16)
    T_PANELS = 2.3
    DIAL = (320, 600, 200)   # centre x, centre y, radius
    A0, A1 = 135, 405        # the dial's 270 degrees, clockwise from 3 o'clock

    def build(self, logo):
        sp = self.spec
        self.bg = backdrop(self.W, self.H, self.s, (18, 26, 44), self.NAVY, (120, 160, 255, 9))
        self.glow = self.glow_sprite(700, self.accent, 40)
        self.logo = Sprite(self.logo_disc(logo, 210))
        # the band
        band = _blank(self.W, self.p(360))
        bd = ImageDraw.Draw(band)
        bd.rectangle([0, 0, self.W, band.height], fill=(12, 16, 26, 240))
        lw = max(2, int(self.p(3)))
        bd.rectangle([0, 0, self.W, lw], fill=self.accent + (255,))
        bd.rectangle([0, band.height - lw, self.W, band.height], fill=self.accent + (255,))
        img, _ = self._chrome(*self._fit("$" + sp["symbol"], "cond", 170, DW - 140, 80))
        band.alpha_composite(img, (int((self.W - img.width) / 2), int(self.p(70))))
        sub = f"PAIRED WITH {sp['pair']} · " + ("GRADUATED" if self.grad else "ON THE BONDING CURVE")
        bd.text((self.W / 2, self.p(300)), sub, font=self.f("mono", 26), fill=TEXT + (255,), anchor="ms")
        self.band = Sprite(band)
        self.band_eyebrow = f"// SILVER SIGNAL · {sp['name'].upper() or sp['symbol']}"[:44]
        # panel 1: the dial
        self.p1 = (70, 300, DW - 70, 860)
        self.panel1 = Sprite(self._panel(self.p1, "BONDING CURVE",
                                         "GRADUATED" if self.grad else
                                         (f"TARGET {compact(sp['target'])} {sp['pair']}" if sp.get("target") else "")))
        cx, cy, r = self.DIAL
        self.track = self.ring_sprite(2 * r, 22, (30, 40, 60))
        self.fill = self.ring_sprite(2 * r, 22, GOLD if self.grad else self.accent)
        ticks = _blank(self.p(2 * r + 80), self.p(2 * r + 80))
        td = ImageDraw.Draw(ticks)
        c = ticks.width / 2
        for i in range(11):
            ang = math.radians(self.A0 + (self.A1 - self.A0) * i / 10)
            r0, r1 = self.p(r + 22), self.p(r + (38 if i % 5 == 0 else 30))
            td.line([(c + r0 * math.cos(ang), c + r0 * math.sin(ang)), (c + r1 * math.cos(ang), c + r1 * math.sin(ang))],
                    fill=DIM + (255,), width=max(1, int(self.p(3))))
            if i % 5 == 0:
                rr = self.p(r + 62)
                td.text((c + rr * math.cos(ang), c + rr * math.sin(ang)), f"{i * 10}", font=self.f("mono", 22),
                        fill=FAINT + (255,), anchor="mm")
        self.ticks = Sprite(ticks)
        # the needle: drawn pointing right, rotated per frame
        L = int(self.p(r - 30))
        nd = _blank(2 * L + 4, 2 * L + 4)
        hw = max(4, self.p(8))
        ImageDraw.Draw(nd).polygon([(L + 2, L + 2 - hw), (2 * L + 2, L + 2), (L + 2, L + 2 + hw)], fill=TEXT + (255,))
        self.needle = nd
        self.cap = self.dot_sprite(34, TEXT)
        self.p1_line = self.plain(self.curve_line(), "uir", 30, DIM, 400, 20)
        # panel 2: the counts
        self.p2 = (70, 890, DW - 70, 1180)
        self.panel2 = Sprite(self._panel(self.p2, "MARKET", "LIVE FROM THE CHAIN"))
        self.cells = [it for it in self.stat_items() if it[1] is not None][:3]

    def _panel(self, box, title, right):
        x0, y0, x1, y1 = box
        img = self._rrect(x1 - x0, y1 - y0, 18, (12, 17, 28, 242), (52, 68, 96, 255), 1.5)
        d = ImageDraw.Draw(img)
        d.line([(0, self.p(56)), (img.width, self.p(56))], fill=(40, 52, 76, 255), width=max(1, int(self.p(1.5))))
        d.ellipse([self.p(22), self.p(22), self.p(34), self.p(34)], fill=self.accent + (255,))
        d.text((self.p(48), self.p(38)), title, font=self.f("mono", 22), fill=DIM + (255,), anchor="ls")
        d.text((img.width - self.p(24), self.p(38)), right, font=self.f("mono", 22), fill=FAINT + (255,), anchor="rs")
        return img

    def scene(self, frame, t):
        # ambient: a slow blue glow
        gx = self.W * (0.5 + 0.2 * math.sin(t * 0.5))
        self.glow.put(frame, gx - self.glow.w / 2, self.H * 0.45 - self.glow.h / 2, 0.8)
        # the band and the logo: the cover, then they leave
        bo = ease_in(_seg(t, self.T_PANELS - 0.35, QUICK))
        if bo < 1:
            a = 1 - bo
            self.logo.put(frame, (self.W - self.logo.w) / 2, self.p(200 - 20 * bo), a)
            self.band.put(frame, 0, self.p(470 - 20 * bo), a)
            n = len(self.band_eyebrow) if t <= 0 else int(len(self.band_eyebrow) * min(1.0, 0.4 + t / 0.8))
            _text(frame, (self.p(70), self.p(450 - 20 * bo)), self.band_eyebrow[:n], self.f("mono", 24), self.accent, a)
        if t < self.T_PANELS:
            return
        out = self.leaving(t)
        lift = -30 * out
        # panel 1 with the dial
        e = ease_out(_seg(t, self.T_PANELS, MOVE))
        a = e * (1 - out)
        dy = 24 * (1 - e) + lift
        x0, y0 = self.p1[0], self.p1[1]
        self.panel1.put(frame, self.p(x0), self.p(y0 + dy), a)
        cx, cy, r = self.DIAL
        tx, ty = self.p(cx) - self.ticks.w / 2, self.p(cy + dy) - self.ticks.h / 2
        self.ticks.put(frame, tx, ty, a)
        rx, ry = self.p(cx) - self.track.w / 2, self.p(cy + dy) - self.track.h / 2
        self.put_arc(frame, self.track, rx, ry, self.A0, self.A1, a)
        c = ease(_seg(t, self.T_PANELS + 0.3, 1.1))
        v = self.prog * c
        ang = self.A0 + (self.A1 - self.A0) * v
        self.put_arc(frame, self.fill, rx, ry, self.A0, ang, a)
        nd = Sprite(self.needle.rotate(-ang, resample=Image.BICUBIC, expand=False))
        nd.put(frame, self.p(cx) - nd.w / 2, self.p(cy + dy) - nd.h / 2, a)
        self.cap.put(frame, self.p(cx) - self.cap.w / 2, self.p(cy + dy) - self.cap.h / 2, a)
        _text(frame, (self.p(580), self.p(560 + dy)), pct(v), self.f("num", 104), GOLD if self.grad else TEXT, a)
        _text(frame, (self.p(584), self.p(620 + dy)), self.curve_caption() if not self.grad else "GRADUATED",
              self.f("mono", 24), self.accent, a)
        self.p1_line.put(frame, self.p(584), self.p(660 + dy), a)
        # panel 2: three counts
        e = ease_out(_seg(t, self.T_PANELS + STAGGER * 2, MOVE))
        a = e * (1 - out)
        dy = 24 * (1 - e) + lift
        x0, y0, x1, y1 = self.p2
        self.panel2.put(frame, self.p(x0), self.p(y0 + dy), a)
        if self.cells:
            cw = (x1 - x0) / len(self.cells)
            for i, (label, value, fmt, sub) in enumerate(self.cells):
                cxx = x0 + i * cw + 32
                c = ease(_seg(t, self.T_PANELS + 0.4 + i * STAGGER, 0.9))
                _text(frame, (self.p(cxx), self.p(y0 + 120 + dy)), label, self.f("mono", 22), FAINT, a)
                txt = fmt(value * c)
                f = self.f("num", 58)
                while f.getlength(txt) > self.p(cw - 50) and f.size > 16:
                    f = font("num", f.size - 3)
                _text(frame, (self.p(cxx), self.p(y0 + 210 + dy)), txt, f, TEXT, a)
                if i:
                    ImageDraw.Draw(frame).line([(self.p(x0 + i * cw), self.p(y0 + 86 + dy)),
                                                (self.p(x0 + i * cw), self.p(y1 - 30 + dy))],
                                               fill=_mix(self.NAVY, (52, 68, 96), a), width=max(1, int(self.p(1.5))))


# ----------------------------------------------------------------------------- lime
class Lime(Style):
    """A big ring around the logo fills to the curve %, the ticker under it, the facts in pills.
    Lime on near-black (a colour, not a claim: no vibe/vibe logo or wording)."""
    preview_at = 4.6
    accent = LIME
    BG = (10, 12, 14)
    RING = (540, 470, 600)    # centre x, centre y, diameter
    T_PILLS = 2.2

    def build(self, logo):
        sp = self.spec
        self.bg = backdrop(self.W, self.H, self.s, (20, 23, 20), self.BG, None)
        d = ImageDraw.Draw(self.bg)  # a dot grid
        st = self.p(45)
        yy = st / 2
        while yy < self.H:
            xx = st / 2
            while xx < self.W:
                d.point((xx, yy), fill=(40, 44, 40))
                xx += st
            yy += st
        cx, cy, D = self.RING
        self.track = self.ring_sprite(D, 34, (32, 36, 38))
        self.fill = self.ring_sprite(D, 34, self.accent)
        self.cap = self.dot_sprite(34, self.accent)
        self.glow = self.glow_sprite(D * 1.35, self.accent, 34)
        self.logo = Sprite(self.logo_disc(logo, D - 150))
        tick, size = self._fit("$" + sp["symbol"], "cond", 140, DW - 140, 70)
        self.tick = self.plain(tick, "cond", size, TEXT)
        self.sub = self.plain(f"PAIRED WITH {sp['pair']}" + (f" · {sp['name'].upper()}" if sp["name"] else ""),
                              "mono", 26, DIM, DW - 140, 18)
        pills = []
        for label, value, fmt, _ in self.stat_items():
            if value is None:
                continue
            word = {"MARKET CAP": "MC", "HOLDERS": "HOLDERS", "BUYS · 24H": "BUYS 24H"}[label]
            pills.append(self._pill(word, fmt(value)))
        # lay the pills out in rows, centred
        self.pills, row, x, y = [], [], 0, 0
        gap = self.p(16)
        for pimg in pills:
            if row and x + pimg.width > self.p(DW - 140):
                self._place(row, x - gap, y)
                row, x, y = [], 0, y + pimg.height + gap
            row.append(pimg)
            x += pimg.width + gap
        if row:
            self._place(row, x - gap, y)

    def _pill(self, word, value):
        fw, fv = self.f("mono", 24), self.f("num", 32)
        w = (fw.getlength(word) + fv.getlength(value)) / self.s + 80
        img = self._rrect(w, 78, 39, (22, 26, 22, 245), (60, 70, 40, 255), 1.5)
        d = ImageDraw.Draw(img)
        d.text((self.p(30), self.p(50)), word, font=fw, fill=DIM + (255,), anchor="ls")
        d.text((self.p(30) + fw.getlength(word) + self.p(16), self.p(52)), value, font=fv, fill=TEXT + (255,),
               anchor="ls")
        return img

    def _place(self, row, width, y):
        x = (self.W - width) / 2
        for pimg in row:
            self.pills.append((Sprite(pimg), x, y))
            x += pimg.width + self.p(16)

    def scene(self, frame, t):
        out = self.leaving(t)
        a0, lift = 1 - out, -30 * out
        if a0 <= 0.01:
            return
        cx, cy, D = self.RING
        pulse = 0.75 + 0.25 * math.sin(t * 1.6)  # ambient: the glow breathes
        self.glow.put(frame, self.p(cx) - self.glow.w / 2, self.p(cy + lift) - self.glow.h / 2, pulse * a0)
        rx, ry = self.p(cx) - self.track.w / 2, self.p(cy + lift) - self.track.h / 2
        self.put_arc(frame, self.track, rx, ry, 0, 360, a0)
        c = ease(_seg(t, 0.4, 1.5))
        v = self.prog * c
        start = -90
        end = start + 360 * v
        self.put_arc(frame, self.fill, rx, ry, start, end, a0)
        if v > 0.002:  # round caps at both ends of the arc
            rr = self.p(D / 2 - 17)
            for ang in (start, end):
                x = self.p(cx) + rr * math.cos(math.radians(ang))
                y = self.p(cy + lift) + rr * math.sin(math.radians(ang))
                self.cap.put(frame, x - self.cap.w / 2, y - self.cap.h / 2, a0)
        self.logo.put(frame, self.p(cx) - self.logo.w / 2, self.p(cy + lift) - self.logo.h / 2, a0)
        self.tick.put(frame, (self.W - self.tick.w) / 2, self.p(800 + lift), a0)
        txt = "GRADUATED TO UNISWAP V4" if self.grad else f"{pct(v)} {self.curve_word()}"
        f = self.f("num", 46)
        _text(frame, ((self.W - f.getlength(txt)) / 2, self.p(1050 + lift)), txt, f, self.accent, a0)
        self.sub.put(frame, (self.W - self.sub.w) / 2, self.p(1068 + lift), a0)
        for i, (spr, x, y) in enumerate(self.pills):
            e = ease_out(_seg(t, self.T_PILLS + i * STAGGER, MOVE))
            if e > 0:
                spr.put(frame, x, self.p(1130 + lift + 24 * (1 - e)) + y, e * a0)


STYLES = {"dashboard": pv.Video, "kinetic": Kinetic, "terminal": Terminal, "ticket": Ticket,
          "gauge": Gauge, "lime": Lime}
NAMES = STYLE_NAMES  # promo.STYLES and STYLE_NAMES: the bot reads them without Pillow
