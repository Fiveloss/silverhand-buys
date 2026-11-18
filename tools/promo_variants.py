"""Cuts of the promo video beside make_promo.Promo ("classic"):

    terminal  a real vibe/vibe launch raced from launch to graduation in twenty seconds: the
              market cap drawn live, every trade popping up on the chart, a feed and counters
    hype      a fast cut on the beat: big words that slam in, shakes, glitches, a cascade of
              posts, a card punch, the graduation burst
    phone     a product film: a phone with the live chat, tilting in 3D among soft lights; the
              trade card flies out of it

Every number and post comes from the testnet curve make_promo.build_data() replays.
"""

import math
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import make_intro
from make_promo import (BG, BOT, DIM, GREEN, INK, LIME, RED, ROOT, BADGES, Promo, draw_message,
                        ease, ease_io, font, make_card, paste_alpha, post_text, rounded, seg, shadow,
                        text_w)

AVATAR = ROOT / "assets" / "brand" / "avatar.png"


# ----------------------------------------------------------------------------- helpers
def perspective_coeffs(src, dst):
    """PIL's PERSPECTIVE coefficients that send the output quad `dst` back to the input `src`."""
    rows, rhs = [], []
    for (x, y), (u, v) in zip(dst, src):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        rhs += [u, v]
    return np.linalg.solve(np.array(rows, float), np.array(rhs, float)).tolist()


def tilt_quad(w, h, cx, cy, yaw=0.0, pitch=0.0, scale=1.0, focal=2.6):
    """The corners of a w x h card turned by yaw (around the vertical) and pitch, seen in perspective."""
    f = focal * max(w, h)
    pts = []
    for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        x1 = x * math.cos(yaw)
        z = x * math.sin(yaw)
        y1 = y * math.cos(pitch)
        z += y * math.sin(pitch)
        k = f / (f + z) * scale
        pts.append((cx + x1 * k, cy + y1 * k))
    return pts


def place_tilted(dst, img, cx, cy, yaw=0.0, pitch=0.0, scale=1.0, alpha=1.0):
    """Draw an RGBA image onto dst, turned in 3D around its centre at (cx, cy)."""
    if alpha <= 0:
        return
    w, h = img.size
    quad = tilt_quad(w, h, cx, cy, yaw, pitch, scale)
    xs, ys = [p[0] for p in quad], [p[1] for p in quad]
    x0, y0 = int(max(0, min(xs) - 2)), int(max(0, min(ys) - 2))
    x1, y1 = int(min(dst.width, max(xs) + 2)), int(min(dst.height, max(ys) + 2))
    if x1 <= x0 or y1 <= y0:
        return
    local = [(x - x0, y - y0) for x, y in quad]
    co = perspective_coeffs([(0, 0), (w, 0), (w, h), (0, h)], local)
    out = img.transform((x1 - x0, y1 - y0), Image.PERSPECTIVE, co, Image.BICUBIC)
    paste_alpha(dst, out, (x0, y0), alpha)


def overshoot(x):
    """0 -> 1 with a springy overshoot."""
    x = min(1.0, max(0.0, x))
    return 1 - math.exp(-6 * x) * math.cos(9 * x)


def money(v):
    if v >= 1000:
        return f"${v / 1000:.1f}K"
    return f"${v:,.2f}" if v < 100 else f"${v:,.0f}"


class Particles:
    def __init__(self, n, seed, speed, colors):
        rnd = random.Random(seed)
        self.p = [(rnd.uniform(0, math.tau), rnd.uniform(.3, 1) * speed, rnd.uniform(3, 9), rnd.choice(colors),
                   rnd.uniform(.6, 1.4)) for _ in range(n)]

    def draw(self, img, t, x, y, u):
        if t < 0 or t > 1.6:
            return
        d = ImageDraw.Draw(img)
        for ang, sp, size, col, life in self.p:
            k = t / life
            if k >= 1:
                continue
            px = x + math.cos(ang) * sp * t * u
            py = y + math.sin(ang) * sp * t * u + 260 * t * t * u
            r = size * u * (1 - k)
            d.rectangle((px - r, py - r, px + r, py + r), fill=(*col, int(255 * (1 - k) ** .7)))


def glow_text(img, xy, text, f, color, glow=.6, blur=18):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(xy, text, font=f, fill=(*color, 255))
    if glow:
        g = layer.filter(ImageFilter.GaussianBlur(blur))
        paste_alpha(img, g, (0, 0), glow)
    img.alpha_composite(layer)


# ----------------------------------------------------------------------------- terminal
class Terminal(Promo):
    """A real launch, raced: the curve's whole life in twenty seconds."""
    MUSIC = "cinematic"
    XFADE = 0.25

    def _build(self):
        T = 0.0
        for name, dur in (("boot", 3.2), ("race", 20.5), ("grad", 3.0), ("outro", 4.3)):
            self.scenes.append((name, T, dur))
            T += dur
        self.length = T
        trades = [(k, p) for k, p in self.data["trades"] if p.get("mc_usd")]
        ts = [p["ts"] for _, p in trades]
        t0, t1 = min(ts), max(ts)
        n = len(trades)
        # pacing: mostly by order (every trade gets its moment), a little by real time
        self.race = []
        for i, (k, p) in enumerate(trades):
            frac = .72 * i / max(1, n - 1) + .28 * (p["ts"] - t0) / max(1, t1 - t0)
            self.race.append((frac, k, p))
        self.race.sort(key=lambda r: r[0])
        self.top = max(p["mc_usd"] for _, _, p in self.race)
        self.grad_mc = (self.data["grad"]["usd"] or 12000) * 5   # 4 ETH in, the curve ends at 5x: the pool's price
        self.burst = Particles(90, 3, 620, [LIME, GREEN, INK])
        self._sounds()

    def grad_time(self):
        return next(s for name, s, _ in self.scenes if name == "grad") + .05

    def music_scenes(self):
        return [("intro", 0, 3.2), ("pitch", 3.2, 3), ("track", 6.2, 3), ("cards", 9.2, 7), ("feed", 16.2, 5),
                ("grad", 21.2, 5.5), ("outro", 26.7, 4.3)]

    def _sounds(self):
        ev = self.events = []
        st = {n: s for n, s, _ in self.scenes}
        ev += [(.2 + i * .07, "key", {"gain": .25}) for i in range(30)]
        ev += [(2.4, "coin", {"gain": .4})]
        r0, r1 = st["race"], st["race"] + 20.5 * .985
        big = sorted(self.race, key=lambda r: -(r[2]["usd"] or 0))[:6]
        for frac, k, p in self.race:
            at = r0 + frac * (r1 - r0)
            ev += [(at, "tick", {"gain": .35, "pitch": 1.2 if k == "buy" else .8})]
        for frac, k, p in big:
            ev += [(r0 + frac * (r1 - r0), "coin", {"gain": .5})]
        g = st["grad"]
        ev += [(g - 2.2, "riser", {"dur": 2.2, "gain": .6}), (g + .05, "impact", {"gain": 1.0}),
               (g + .1, "coin", {"gain": .6, "pitch": 1.5}), (st["outro"] + .1, "chord", {"dur": 4, "gain": .4})]

    # the race's state at a moment of the race scene
    def shown(self, lt, dur):
        k = min(1.0, lt / (dur * .985))
        return [r for r in self.race if r[0] <= k], k

    def s_boot(self, t, dur):
        u, H = self.u, self.H
        img = self.bg(GREEN, .4)
        tok = self.data["token"]
        lines = [("> silverhand-buys --chain robinhood-testnet", DIM),
                 (f"> /track {tok.address}", INK),
                 (f"[ok] vibe/vibe curve found · {tok.name} (${tok.symbol})", GREEN),
                 ("> watching every trade from launch…", LIME)]
        f = font("mono", (30 if not self.vert else 26) * u)
        x, y = (140 * u, H / 2 - 110 * u) if not self.vert else (60 * u, H / 2 - 140 * u)
        d = ImageDraw.Draw(img)
        start = 0.15
        for text, col in lines:
            n = int(len(text) * seg(t, start, start + .55))
            if n <= 0:
                break
            shown = text[:n]
            if self.vert and len(shown) > 40:
                d.text((x, y), shown[:40], font=f, fill=col)
                y += 44 * u
                shown = "  " + shown[40:]
            d.text((x, y), shown, font=f, fill=col)
            if n < len(text) or (text == lines[-1][0] and int(t * 3) % 2 == 0):
                cx = x + text_w(d, shown, f) + 6 * u
                d.rectangle((cx, y + 4 * u, cx + 14 * u, y + 34 * u), fill=GREEN)
            y += 56 * u
            start += .7
        self.tapes(img, t)
        return img

    def chart_geom(self):
        u, W, H = self.u, self.W, self.H
        if not self.vert:
            return (90 * u, 210 * u, W * .66, H - 120 * u)
        return (60 * u, 520 * u, W - 60 * u, 1230 * u)

    def s_race(self, t, dur):
        u = self.u
        img = self.bg(LIME, .55)
        d = ImageDraw.Draw(img)
        shown, k = self.shown(t, dur)
        x0, y0, x1, y1 = self.chart_geom()
        top = max(self.grad_mc * 1.08, self.top)

        def X(frac):
            return x0 + (x1 - x0) * frac

        def Y(mc):  # a square-root scale: the early trades get room, the graduation still towers
            return y1 - (y1 - y0) * min(1.0, math.sqrt(max(0.0, mc) / top))

        # the frame: graduation line, gridlines
        for g in range(1, 4):
            yy = y1 - (y1 - y0) * g / 4
            d.line((x0, yy, x1, yy), fill=(30, 34, 38), width=max(1, int(u)))
        gy = Y(self.grad_mc)
        for xx in range(int(x0), int(x1), int(18 * u)):
            d.line((xx, gy, xx + 9 * u, gy), fill=LIME, width=max(1, int(2 * u)))
        fl = font("num", 18 * u)
        d.rounded_rectangle((x0, gy - 34 * u, x0 + 250 * u, gy - 6 * u), 6 * u, fill=(46, 52, 10))
        d.text((x0 + 10 * u, gy - 31 * u), f"GRADUATION · {money(self.grad_mc)}", font=fl, fill=LIME)
        # the line and the area under it
        if shown:
            pts = [(X(fr), Y(p["mc_usd"])) for fr, _, p in shown]
            pts.append((X(k), pts[-1][1]))
            area = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ad = ImageDraw.Draw(area)
            ad.polygon([(pts[0][0], y1)] + pts + [(pts[-1][0], y1)], fill=(*GREEN, 46))
            img.alpha_composite(area)
            d = ImageDraw.Draw(img)
            d.line(pts, fill=GREEN, width=max(2, int(4 * u)), joint="curve")
            for fr, kind, p in shown[-40:]:
                c = GREEN if kind == "buy" else RED
                r = 4 * u
                d.ellipse((X(fr) - r, Y(p["mc_usd"]) - r, X(fr) + r, Y(p["mc_usd"]) + r), fill=c)
            hx, hy = pts[-1]
            pulse = .5 + .5 * math.sin(t * 8)
            halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(halo).ellipse((hx - 26 * u, hy - 26 * u, hx + 26 * u, hy + 26 * u), fill=(*GREEN, 90))
            paste_alpha(img, halo.filter(ImageFilter.GaussianBlur(10 * u)), (0, 0), .5 + .5 * pulse)
            d = ImageDraw.Draw(img)
            d.ellipse((hx - 8 * u, hy - 8 * u, hx + 8 * u, hy + 8 * u), fill=INK)
            # pop-ups rising from the latest trades
            span = dur * .985
            pops = [r for r in shown[-12:] if (r[2].get("usd") or 0) >= 5][-3:]
            for j, (fr, kind, p) in enumerate(pops):
                age = t - fr * span
                if age > 1.3:
                    continue
                a = min(1, age * 6) * (1 - seg(age, .8, 1.3))
                usd = p.get("usd") or 0
                big = usd >= 150
                txt = ("+" if kind == "buy" else "−") + money(usd)
                ff = font("num", (34 if big else 24) * u)
                px, py = X(fr) + (j - 1) * 30 * u, Y(p["mc_usd"]) - (40 + 46 * j) * u - age * 60 * u
                layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
                ld = ImageDraw.Draw(layer)
                bsz = int((40 if big else 28) * u)
                name = "whale" if (p.get("usd") or 0) >= 1000 else ("dev" if p.get("dev") else ("buy" if kind == "buy" else "sell"))
                layer.alpha_composite(BADGES.get(name, t, bsz), (int(px - bsz - 6 * u), int(py)))
                ld.text((px, py + (bsz - (34 if big else 24) * u) / 2 - 4 * u), txt, font=ff,
                        fill=(*(GREEN if kind == "buy" else RED), 255))
                paste_alpha(img, layer, (0, 0), a)
        # the side: market cap, progress, counters and the live feed
        last = shown[-1][2] if shown else None
        self._panel(img, t, shown, last)
        self.caption(img, t, "// A REAL VIBE/VIBE LAUNCH · TESTNET", "From launch to Uniswap, live",
                     50 * u if not self.vert else 150 * u, LIME, .05, 50 if not self.vert else 60)
        self.tapes(img, t, LIME)
        return img

    def _panel(self, img, t, shown, last):
        u, W = self.u, self.W
        d = ImageDraw.Draw(img)
        if not self.vert:
            px, py, pw = W * .69, 210 * u, W * .31 - 70 * u
        else:
            px, py, pw = 60 * u, 1290 * u, W - 120 * u
        mc = last["mc_usd"] if last else 0
        prog = (last.get("progress") or 0) if last else 0
        d.text((px, py), "MARKET CAP", font=font("num", 20 * u), fill=DIM)
        d.text((px, py + 26 * u), money(mc), font=font("num", 64 * u), fill=INK)
        by = py + 120 * u
        d.rounded_rectangle((px, by, px + pw, by + 16 * u), 8 * u, fill=(30, 34, 38))
        if prog > 0:
            d.rounded_rectangle((px, by, px + max(16 * u, pw * prog), by + 16 * u), 8 * u, fill=LIME)
        d.text((px, by + 26 * u), f"{prog * 100:.0f}% TO GRADUATION", font=font("num", 20 * u), fill=LIME)
        buys = sum(1 for _, k, _ in shown if k == "buy")
        sells = len(shown) - buys
        vol = sum((p.get("usd") or 0) for _, _, p in shown)
        cy = by + 80 * u
        for i, (label, val, col) in enumerate((("BUYS", str(buys), GREEN), ("SELLS", str(sells), RED),
                                               ("VOLUME", money(vol), INK))):
            xx = px + i * pw / 3
            d.text((xx, cy), label, font=font("num", 18 * u), fill=DIM)
            d.text((xx, cy + 24 * u), val, font=font("num", 36 * u), fill=col)
        if self.vert:
            return
        # the live feed: the last few trades sliding in at the top
        fy = cy + 110 * u
        d.text((px, fy), "LIVE FEED", font=font("num", 18 * u), fill=DIM)
        fy += 34 * u
        span = self.scenes[1][2] * .985
        for j, (fr, kind, p) in enumerate(reversed(shown[-7:])):
            age = t - fr * span
            a = min(1, age * 5) if j == 0 else 1.0
            row_y = fy + j * 54 * u - (1 - a) * 20 * u
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            ld.rounded_rectangle((px, row_y, px + pw, row_y + 46 * u), 10 * u, fill=(18, 21, 24, 255))
            name = "whale" if (p.get("usd") or 0) >= 1000 else ("dev" if p.get("dev") else ("buy" if kind == "buy" else "sell"))
            layer.alpha_composite(BADGES.get(name, t, int(30 * u)), (int(px + 10 * u), int(row_y + 8 * u)))
            amt = ("+" if kind == "buy" else "−") + money(p.get("usd") or 0)
            ld.text((px + 52 * u, row_y + 10 * u), amt, font=font("num", 22 * u),
                    fill=(*(GREEN if kind == "buy" else RED), 255))
            who = (p.get("buyer") or "")[:6] + "…" + (p.get("buyer") or "")[-4:]
            fw = font("mono", 18 * u)
            ld.text((px + pw - 12 * u - text_w(ld, who, fw), row_y + 13 * u), who, font=fw, fill=(*DIM, 255))
            paste_alpha(img, layer, (0, 0), a * (1 - j * .1))

    def s_grad(self, t, dur):
        u, W, H = self.u, self.W, self.H
        img = self.s_race(self.scenes[1][2], self.scenes[1][2])
        x0, y0, x1, y1 = self.chart_geom()
        hx, hy = x1, y1 - (y1 - y0) * math.sqrt(min(1, self.grad_mc / max(self.grad_mc * 1.08, self.top)))
        flash = 1 - seg(t, 0, .5)
        if flash > 0:
            img = Image.alpha_composite(img, Image.new("RGBA", img.size, (*LIME, int(110 * flash))))
        shake = (1 - seg(t, 0, .6)) * 14 * u
        if shake > 0:
            dx, dy = math.sin(t * 90) * shake, math.cos(t * 77) * shake
            img = img.transform(img.size, Image.AFFINE, (1, 0, -dx, 0, 1, -dy))
        dim = Image.new("RGBA", img.size, (0, 0, 0, int(185 * seg(t, .1, .5))))
        img = Image.alpha_composite(img, dim)
        self.burst.draw(img, t, hx, hy, u)
        k = overshoot(seg(t, .05, .7))
        f = font("hero", (110 if not self.vert else 84) * u * (0.6 + .4 * k))
        d = ImageDraw.Draw(img)
        for i, (txt, col) in enumerate((("GRADUATED", LIME), ("→ UNISWAP V4", INK))):
            w = text_w(d, txt, f)
            glow_text(img, ((W - w) / 2, H / 2 - (130 - i * 125) * u), txt, f, col, .5 * k)
        sub = f"{self.data['grad']['raised']:g} ETH raised · the bot follows it into the pool"
        fs = font("uir", 30 * u)
        a = seg(t, .6, 1.0)
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).text(((W - text_w(d, sub, fs)) / 2, H / 2 + 140 * u), sub, font=fs, fill=(*INK, 255))
        paste_alpha(img, layer, (0, 0), a)
        return img


# ----------------------------------------------------------------------------- hype
BPM_HYPE = 100
BEAT = 60 / BPM_HYPE


class Hype(Promo):
    """A fast cut on the beat (100 BPM, synthwave)."""
    MUSIC = "synthwave"
    XFADE = 0.0

    def _build(self):
        # (scene, beats)
        T = 0.0
        for name, beats in (("words", 8), ("posts", 8), ("cards", 8), ("race", 4), ("slam", 4), ("logo", 5)):
            self.scenes.append((name, T, beats * BEAT))
            T += beats * BEAT
        self.length = T + .6
        self.scenes[-1] = (self.scenes[-1][0], self.scenes[-1][1], self.scenes[-1][2] + .6)
        self.burst = Particles(120, 5, 760, [LIME, GREEN, INK])
        d = self.data
        self.posts = [(d["first"], False), (d["mid"], False), (d["sell"], True), (d["whale"], False),
                      (d["late"], False), (d["mid"], False), (d["first"], False), (d["whale"], False)]
        self._sounds()

    def grad_time(self):
        return next(s for name, s, _ in self.scenes if name == "slam")

    def music_scenes(self):  # 100 BPM like the music: the cuts land on its beats
        return [("intro", 0, .1), ("pitch", 0, 1), ("track", 1, 1.4), ("cards", 2.4, 7.2), ("feed", 9.6, 4.8),
                ("grad", 14.4, 4.8), ("outro", 19.2, 2)]

    def _sounds(self):
        ev = self.events = []
        st = {n: s for n, s, _ in self.scenes}
        for i in range(8):
            ev += [(st["words"] + i * BEAT, "impact" if i % 2 == 0 else "pop", {"gain": .5 if i % 2 == 0 else .7})]
        for i in range(8):
            ev += [(st["posts"] + i * BEAT, "pop", {"pitch": 1 + (i % 4) * .1, "gain": .8})]
        ev += [(st["cards"], "whoosh", {"dur": .4}), (st["cards"] + .25, "coin", {"gain": .6}),
               (st["cards"] + 4 * BEAT, "whoosh", {"dur": .4}), (st["cards"] + 4 * BEAT + .25, "coin", {"gain": .8})]
        ev += [(st["race"], "riser", {"dur": 4 * BEAT, "gain": .7})]
        ev += [(st["slam"], "impact", {"gain": 1.0}), (st["slam"] + .05, "coin", {"gain": .7, "pitch": 1.5}),
               (st["logo"], "impact", {"gain": .7}), (st["logo"] + .1, "chord", {"dur": 3.5, "gain": .4})]

    def shake(self, img, t, amount):
        if amount <= 0:
            return img
        dx, dy = math.sin(t * 97) * amount, math.cos(t * 83) * amount
        return img.transform(img.size, Image.AFFINE, (1, 0, -dx, 0, 1, -dy), fillcolor=(*BG, 255))

    def glitch(self, img, t, strength):
        if strength <= 0:
            return img
        rnd = random.Random(int(t * 60))
        W, H = img.size
        out = img.copy()
        for _ in range(int(6 * strength)):
            h = rnd.randint(int(8 * self.u), int(60 * self.u))
            y = rnd.randint(0, H - h)
            dx = rnd.randint(-int(60 * self.u), int(60 * self.u))
            out.paste(img.crop((0, y, W, y + h)), (dx, y))
        return out

    def slam_word(self, img, word, lt, color, size, y=None, badge=None):
        u, W, H = self.u, self.W, self.H
        k = overshoot(seg(lt, 0, .22))
        f = font("hero", size * u * (1.6 - .6 * k))
        d = ImageDraw.Draw(img)
        w = text_w(d, word, f)
        if w > W - 80 * u:
            f = font("hero", size * u * (1.6 - .6 * k) * (W - 80 * u) / w)
            w = text_w(d, word, f)
        yy = (H / 2 - f.size * .62) if y is None else y
        glow_text(img, ((W - w) / 2, yy), word, f, color, .45)
        if badge:
            bsz = int(170 * u * k)
            if bsz > 4:
                paste_alpha(img, BADGES.get(badge, lt, bsz), ((W - bsz) / 2, yy - bsz - 30 * u), min(1, lt * 6))

    def s_words(self, t, dur):
        u = self.u
        i = int(t / BEAT)
        lt = t - i * BEAT
        words = [("EVERY", INK, None), ("VIBE/VIBE", LIME, None), ("LAUNCH.", INK, "rocket"), ("EVERY", INK, None),
                 ("BUY.", GREEN, "buy"), ("LIVE", INK, None), ("IN YOUR", INK, None), ("CHAT.", GREEN, "chart")]
        word, col, badge = words[min(i, 7)]
        img = self.bg(LIME if col == LIME else GREEN, 1.0)
        self.slam_word(img, word, lt, col, 210 if not self.vert else 170, badge=badge)
        img = self.shake(img, t, (1 - seg(lt, 0, .18)) * 16 * u)
        img = self.glitch(img, t, 1 - seg(lt, 0, .1))
        self.tapes(img, t * 3, LIME if i % 2 else GREEN)
        return img

    def s_posts(self, t, dur):
        u, W, H = self.u, self.W, self.H
        img = self.bg(GREEN, .9)
        i = min(7, int(t / BEAT))
        labels = ["BUYS", "BUYS", "SELLS", "WHALES", "BUYS", "BUYS", "DEV BUYS", "WHALES"]
        lab = labels[i]
        f = font("hero", (150 if not self.vert else 120) * u)
        d = ImageDraw.Draw(img)
        lw = text_w(d, lab, f)
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).text(((W - lw) / 2, (90 if not self.vert else 200) * u), lab, font=f,
                                   fill=(*(RED if lab == "SELLS" else LIME if lab in ("WHALES", "DEV BUYS") else GREEN), 255))
        img.alpha_composite(layer)
        pw = int((900 if not self.vert else 940) * u)
        px = 33 * u if not self.vert else 34 * u
        stack = []
        for j in range(i + 1):
            p, sell = self.posts[j]
            key = ("post", j, pw)
            if key not in self._cache:
                self._cache[key] = draw_message(post_text(p, sell), pw, px, 0.0, sender="Silverhand Buys")
            stack.append(self._cache[key])
        lt = t - i * BEAT
        cx, cy = W / 2, H * (.68 if not self.vert else .62)
        for depth, m in reversed(list(enumerate(reversed(stack[-3:])))):
            if depth == 0:
                k = overshoot(seg(lt, 0, .3))
                side = -1 if i % 2 else 1
                x = cx + side * (1 - k) * W * .7
                place_tilted(img, m, x, cy, yaw=side * (1 - k) * .6, scale=1.0)
            else:  # the older posts behind, darker and smaller, fanned upwards
                old = m.copy()
                shade = Image.new("RGBA", old.size, (0, 0, 0, int(90 + 50 * depth)))
                shade.putalpha(Image.composite(shade.getchannel("A"), Image.new("L", old.size, 0), old.getchannel("A")))
                old.alpha_composite(shade)
                place_tilted(img, old, cx + (depth % 2 * 2 - 1) * 60 * u * depth, cy - 75 * u * depth,
                             yaw=(depth % 2 * 2 - 1) * .14, scale=1 - .1 * depth)
        img = self.shake(img, t, (1 - seg(lt, 0, .12)) * 8 * u)
        self.tapes(img, t * 3)
        return img

    def s_cards(self, t, dur):
        u, W, H = self.u, self.W, self.H
        img = self.bg(LIME, .9)
        i = 0 if t < 4 * BEAT else 1
        lt = t - i * 4 * BEAT
        cw = int((1500 if not self.vert else 1000) * u)
        ch = cw * 9 // 16
        key = ("hcards", cw)
        if key not in self._cache:
            d = self.data
            self._cache[key] = [make_card(d, d["mid"], 1, (cw, ch), 4 * BEAT + .5),
                                make_card(d, d["whale"], 1, (cw, ch), 4 * BEAT + .5)]
        card = self._cache[key][i].frame(min(lt + .5, 4 * BEAT)).convert("RGBA")
        card.putalpha(rounded(card.size, int(20 * u)))
        k = overshoot(seg(lt, 0, .35))
        yaw = (1 - k) * (.7 if i == 0 else -.7) + .06 * math.sin(lt * 2)
        place_tilted(img, card, W / 2, H / 2 + (40 if not self.vert else 0) * u, yaw=yaw, pitch=.05,
                     scale=.55 + .45 * k)
        title = "A LIVE CARD FOR EVERY TRADE" if i == 0 else "WHALES GET NOTICED"
        f = font("hero", (60 if not self.vert else 54) * u)
        d = ImageDraw.Draw(img)
        if self.vert and i == 0:
            title = "A LIVE CARD\nFOR EVERY TRADE"
        yy = (40 if not self.vert else 260) * u
        for ln in title.split("\n"):
            glow_text(img, ((W - text_w(d, ln, f)) / 2, yy), ln, f, LIME, .3)
            yy += 66 * u
        img = self.shake(img, t, (1 - seg(lt, 0, .15)) * 12 * u)
        self.tapes(img, t * 3, LIME)
        return img

    def s_race(self, t, dur):
        u, W, H = self.u, self.W, self.H
        img = self.bg(LIME, 1.0 + seg(t, 0, dur))
        p = ease_io(seg(t, 0, dur * .95))
        bw, bh = int((1400 if not self.vert else 900) * u), int(60 * u)
        bx, by = (W - bw) / 2, H / 2 - bh / 2
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((bx, by, bx + bw, by + bh), bh / 2, fill=(30, 34, 38))
        d.rounded_rectangle((bx, by, bx + max(bh, bw * p), by + bh), bh / 2, fill=LIME)
        f = font("num", 120 * u)
        txt = f"{p * 100:.0f}%"
        glow_text(img, ((W - text_w(d, txt, f)) / 2, by + 90 * u), txt, f, INK if p < .99 else LIME, .3)
        f2 = font("hero", 54 * u)
        cap = "THE CURVE FILLS"
        glow_text(img, ((W - text_w(d, cap, f2)) / 2, by - 250 * u), cap, f2, LIME, .3)
        bsz = int(110 * u)
        paste_alpha(img, BADGES.get("rocket", t, bsz), (bx + max(bh, bw * p) - bsz / 2, by - bsz - 10 * u))
        img = self.shake(img, t, seg(t, dur * .5, dur) * 6 * u)
        self.tapes(img, t * 6, LIME)
        return img

    def s_slam(self, t, dur):
        u, W, H = self.u, self.W, self.H
        img = self.bg(LIME, 2.0)
        flash = 1 - seg(t, 0, .35)
        self.burst.draw(img, t, W / 2, H / 2, u)
        k = overshoot(seg(t, 0, .25))
        for i, (txt, col) in enumerate((("GRADUATED", LIME), ("UNISWAP V4", INK))):
            f = font("hero", (190 if i == 0 else 120) * u * (1.5 - .5 * k) * (1 if not self.vert else .62))
            d = ImageDraw.Draw(img)
            glow_text(img, ((W - text_w(d, txt, f)) / 2, H / 2 - (200 - i * 230) * u * (1 if not self.vert else .8)),
                      txt, f, col, .5)
        if flash > 0:
            img = Image.alpha_composite(img, Image.new("RGBA", img.size, (*LIME, int(200 * flash))))
        img = self.shake(img, t, (1 - seg(t, 0, .5)) * 22 * u)
        img = self.glitch(img, t, 1 - seg(t, 0, .2))
        self.tapes(img, t * 3, LIME)
        return img

    def s_logo(self, t, dur):
        u, W, H = self.u, self.W, self.H
        img = self.bg(GREEN, 1.4)
        k = overshoot(seg(t, 0, .3))
        av = int(260 * u * k)
        if av > 4:
            logo, mask = make_intro.circle_logo(AVATAR, av)
            ring = Image.new("RGBA", (av + 40, av + 40), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse((0, 0, av + 39, av + 39), outline=(*GREEN, 255), width=max(2, int(5 * u)))
            img.alpha_composite(ring, (int(W / 2 - av / 2 - 20), int(H / 2 - 330 * u - 20)))
            img.paste(logo, (int(W / 2 - av / 2), int(H / 2 - 330 * u)), mask)
        d = ImageDraw.Draw(img)
        f = font("hero", (130 if not self.vert else 96) * u)
        txt = "SILVERHAND BUYS"
        a = seg(t, .1, .4)
        if a > 0:
            glow_text(img, ((W - text_w(d, txt, f)) / 2, H / 2 - 20 * u), txt, f, GREEN, .5 * a)
        for i, (line, col, ff) in enumerate(((BOT, INK, font("num", 40 * u)),
                                             ("Built for vibe/vibe launches · try it on testnet", LIME, font("ui", 30 * u)),
                                             ("Not affiliated with Robinhood · NFA · DYOR", DIM, font("uir", 22 * u)))):
            a = seg(t, .4 + i * .2, .7 + i * .2)
            if a <= 0:
                continue
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).text(((W - text_w(d, line, ff)) / 2, H / 2 + (150 + i * 60) * u), line, font=ff,
                                       fill=(*col, 255))
            paste_alpha(img, layer, (0, 0), a)
        img = self.shake(img, t, (1 - seg(t, 0, .25)) * 14 * u)
        self.tapes(img, t)
        return img


# ----------------------------------------------------------------------------- phone
class Phone(Promo):
    """A product film: the live chat on a phone, tilting among soft lights."""
    MUSIC = "lofi"
    XFADE = 0.0

    def _build(self):
        self.scenes = [("film", 0.0, 30.0), ("grad", 22.0, 4.0)]  # "grad" only marks the music's downbeat
        self.post_px = (19 if not self.vert else 24) * self.u  # the posts' text on the phone's screen
        self.length = 30.0
        d = self.data
        self.feed = [(3.2, d["first"], False, "12:01"), (6.0, d["mid"], False, "12:04"), (8.6, d["sell"], True, "12:06"),
                     (11.0, d["whale"], False, "12:07"), (13.6, d["late"], False, "12:09")]
        rnd = random.Random(8)
        self.bokeh = [(rnd.random(), rnd.random(), rnd.uniform(40, 160), rnd.choice([GREEN, LIME, (90, 200, 255)]),
                       rnd.uniform(.1, .4), rnd.uniform(0, math.tau)) for _ in range(22)]
        self.floaters = [("buy", .1, .2), ("whale", .9, .22), ("curve", .86, .78), ("dev", .14, .8), ("mc", .5, .08)]
        self._sounds()

    def frame(self, t):
        img = self.s_film(t, self.length).convert("RGB")
        if t < 0.6:
            img = Image.blend(Image.new("RGB", img.size), img, ease(t / 0.6))
        if t > self.length - 0.8:
            img = Image.blend(img, Image.new("RGB", img.size), ease_io((t - self.length + 0.8) / 0.8))
        return img

    def grad_time(self):
        return 23.2

    def music_scenes(self):
        return [("intro", 0, 3), ("pitch", 3, 3), ("track", 6, 4), ("cards", 10, 6), ("feed", 16, 5),
                ("grad", 21, 5), ("outro", 26, 4)]

    def _sounds(self):
        ev = self.events = []
        for at, *_ in self.feed:
            ev += [(at, "pop", {"pitch": 1.1, "gain": .6})]
        ev += [(15.55, "whoosh", {"dur": .45}), (15.75, "chart", {"dur": 1.6, "gain": .9}), (21.0, "riser", {"dur": 2.2, "gain": .4}),
               (23.2, "impact", {"gain": .6}), (23.25, "coin", {"gain": .5, "pitch": 1.5}),
               (26.2, "chord", {"dur": 3.6, "gain": .35})]

    def backdrop(self, t):
        key = ("phone_bg",)
        if key not in self._cache:
            W, H = self.W, self.H
            base = Image.new("RGB", (W, H), (5, 9, 9))
            g = Image.new("L", (W // 4, H // 4), 0)
            gd = ImageDraw.Draw(g)
            gd.ellipse((-W * .05, H * .1, W * .2, H * .3), fill=160)
            gd.ellipse((W * .1, -H * .05, W * .25, H * .12), fill=90)
            g = g.filter(ImageFilter.GaussianBlur(W // 25)).resize((W, H), Image.BILINEAR)
            base = Image.composite(Image.new("RGB", (W, H), (12, 60, 44)), base, g)
            self._cache[key] = base
        img = self._cache[key].copy().convert("RGBA")
        u, W, H = self.u, self.W, self.H
        layer = Image.new("RGBA", (W // 4, H // 4), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for x, y, r, col, a, ph in self.bokeh:
            xx = (x * W + math.sin(t * .2 + ph) * 40 * u) / 4
            yy = (y * H + math.cos(t * .15 + ph) * 30 * u) / 4
            rr = r * u / 4
            ld.ellipse((xx - rr, yy - rr, xx + rr, yy + rr), fill=(*col, int(255 * a * .35)))
        layer = layer.filter(ImageFilter.GaussianBlur(6)).resize((W, H), Image.BILINEAR)
        img.alpha_composite(layer)
        return img

    def phone(self, t, w, h):
        """The phone: bezel, screen with the group chat and the posts arriving."""
        u = self.u
        scr_w, scr_h = w - int(26 * u), h - int(26 * u)
        msgs = []
        for at, p, sell, ts in self.feed:
            key = ("pmsg", at, scr_w)
            if key not in self._cache:
                self._cache[key] = draw_message(post_text(p, sell), int(scr_w * .92), self.post_px, 0.0,
                                                sender="Silverhand Buys", time_s=ts)
            msgs.append((at, self._cache[key], "left"))
        # live badges: redraw only the newest bubble with its badges moving
        if msgs:
            live = [m for m in msgs if m[0] <= t]
            if live:
                at, _, _ = live[-1]
                p, sell, ts = next((p, s, ts_) for a_, p, s, ts_ in self.feed if a_ == at)
                msgs[len(live) - 1] = (at, draw_message(post_text(p, sell), int(scr_w * .92), self.post_px, t,
                                                        sender="Silverhand Buys", time_s=ts), "left")
        bar = int(58 * u)
        chat = self.chat_window(t, msgs, "UG holders", "vibe/vibe launch · 1,204 members", scr_w, scr_h - bar)
        screen = Image.new("RGBA", (scr_w, scr_h), (23, 33, 43, 255))
        sd = ImageDraw.Draw(screen)
        sd.text((int(34 * u), int(16 * u)), "12:07", font=font("ui", 20 * u), fill=INK)
        bx = scr_w - int(70 * u)
        sd.rounded_rectangle((bx, 20 * u, bx + 38 * u, 38 * u), 5 * u, outline=INK, width=max(1, int(2 * u)))
        sd.rectangle((bx + 4 * u, 24 * u, bx + 28 * u, 34 * u), fill=INK)
        screen.alpha_composite(chat, (0, bar))
        body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        bd = ImageDraw.Draw(body)
        bd.rounded_rectangle((0, 0, w - 1, h - 1), int(64 * u), fill=(28, 32, 36, 255))
        bd.rounded_rectangle((3 * u, 3 * u, w - 4 * u, h - 4 * u), int(62 * u), outline=(70, 78, 86, 255), width=int(2 * u))
        screen.putalpha(rounded(screen.size, int(52 * u)))
        body.alpha_composite(screen, (int(13 * u), int(13 * u)))
        bd = ImageDraw.Draw(body)
        bd.rounded_rectangle((w / 2 - 60 * u, 22 * u, w / 2 + 60 * u, 50 * u), 14 * u, fill=(0, 0, 0, 255))
        return body

    def s_film(self, t, dur):
        u, W, H = self.u, self.W, self.H
        img = self.backdrop(t)
        # floating badges, soft and out of focus at the edges
        for i, (name, fx, fy) in enumerate(self.floaters):
            a = seg(t, .5 + i * .3, 1.2 + i * .3) * (1 - seg(t, 25.5, 26.2)) * .55
            if a <= 0:
                continue
            bsz = int(96 * u)
            badge = BADGES.get(name, t + i, bsz).filter(ImageFilter.GaussianBlur(1.5 * u))
            paste_alpha(img, badge, (fx * W - bsz / 2, fy * H - bsz / 2 + math.sin(t * .8 + i) * 14 * u), a)
        # the phone, swaying a little in 3D; it moves to the middle for the end
        ph_h = int((960 if not self.vert else 1300) * u)
        ph_w = int(ph_h * .47)
        end = ease_io(seg(t, 25.8, 27.0))
        cx = (W * (.33 if not self.vert else .5)) * (1 - end) + W * .5 * end
        cy = H * (.53 if not self.vert else .56)
        if t < 26.4:
            ph = self.phone(t, ph_w, ph_h)
            sh, pad = shadow((ph_w, ph_h), int(60 * u), int(50 * u), 200)
            lay = Image.new("RGBA", sh.size, (0, 0, 0, 0))
            lay.putalpha(sh)
            k_in = ease(seg(t, .2, 1.4))
            paste_alpha(img, lay, (cx - ph_w / 2 - pad + 30 * u, cy - ph_h / 2 - pad + 40 * u), .8 * k_in * (1 - end))
            yaw = .32 * (1 - k_in) + .13 * math.sin(t * .45) - .05
            place_tilted(img, ph, cx, cy + (1 - k_in) * 120 * u, yaw=yaw, pitch=.04 * math.sin(t * .3),
                         scale=(.92 + .08 * k_in) * (1 - .5 * end), alpha=k_in * (1 - end))
        # the card flies out of the phone
        if 15.6 <= t < 22.4:
            lt = t - 15.6
            cw = int((900 if not self.vert else 900) * u)
            key = ("pcard", cw)
            if key not in self._cache:
                self._cache[key] = make_card(self.data, self.data["whale"], 1, (cw, cw * 9 // 16), 7.0)
            card = self._cache[key].frame(min(lt, 6.9)).convert("RGBA")
            card.putalpha(rounded(card.size, int(18 * u)))
            k = ease(seg(lt, 0, .9))
            out = ease_io(seg(lt, 6.2, 6.8))
            tx = W * (.68 if not self.vert else .5)
            ty = H * (.6 if not self.vert else .4)
            x = cx + (tx - cx) * k
            y = cy + (ty - cy) * k
            place_tilted(img, card, x, y, yaw=-.32 * (1 - k) - .12, pitch=.05, scale=.3 + .7 * k - .3 * out,
                         alpha=1 - out)
        # the words beside the phone
        lines = [(0.6, 4.6, "Silverhand Buys", "for vibe/vibe launches", GREEN),
                 (4.6, 9.8, "Every buy,", "the moment it lands.", INK),
                 (9.8, 15.4, "Whales and dev buys,", "marked for you.", LIME),
                 (15.6, 22.0, "A live card", "for every trade.", INK),
                 (22.2, 25.8, "Follows it", "into Uniswap v4.", LIME)]
        tx = W * .6 if not self.vert else W * .5
        for a0, a1, l1, l2, col in lines:
            if not (a0 <= t < a1):
                continue
            a = ease(seg(t, a0, a0 + .6)) * (1 - ease_io(seg(t, a1 - .45, a1)))
            if a0 >= 15.6 and a0 < 22 and not self.vert:  # beside the card: above it
                ty = H * .07
            elif self.vert:
                ty = H * .07
            else:
                ty = H * .38
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            size = (70 if not self.vert else 74) * u
            room = (W - tx - 50 * u) if not self.vert else W - 80 * u
            widest = max(text_w(ld, l1, font("hero", size)), text_w(ld, l2, font("hero", size)))
            if widest > room:
                size *= room / widest
            f1 = f2 = font("hero", size)
            if self.vert:
                ld.text(((W - text_w(ld, l1, f1)) / 2, ty), l1, font=f1, fill=(*INK, 255))
                ld.text(((W - text_w(ld, l2, f2)) / 2, ty + 88 * u), l2, font=f2, fill=(*col, 255))
            else:
                ld.text((tx, ty + (1 - a) * 30 * u), l1, font=f1, fill=(*INK, 255))
                ld.text((tx, ty + 90 * u + (1 - a) * 30 * u), l2, font=f2, fill=(*col, 255))
            paste_alpha(img, layer, (0, 0), a)
            if a0 == 22.2:  # a mini curve filling to 100%
                p = ease_io(seg(t, 22.3, 23.2))
                bx = tx if not self.vert else W * .2
                by = ty + 210 * u
                bw = 560 * u if not self.vert else W * .6
                d = ImageDraw.Draw(img)
                d.rounded_rectangle((bx, by, bx + bw, by + 22 * u), 11 * u, fill=(30, 34, 38))
                d.rounded_rectangle((bx, by, bx + max(22 * u, bw * p), by + 22 * u), 11 * u, fill=LIME)
                d.text((bx, by + 34 * u), f"{p * 100:.0f}% · GRADUATED" if p > .99 else f"{p * 100:.0f}%",
                       font=font("num", 26 * u), fill=LIME)
        # the end: the logo and the bot's name
        if t >= 26.0:
            a = ease(seg(t, 26.4, 27.2))
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            av = int(220 * u)
            logo, mask = make_intro.circle_logo(AVATAR, av)
            lx, ly = int(W / 2 - av / 2), int(H / 2 - 300 * u)
            layer.paste(logo, (lx, ly), mask)
            ld = ImageDraw.Draw(layer)
            f = font("hero", (110 if not self.vert else 88) * u)
            ld.text(((W - text_w(ld, "SILVERHAND BUYS", f)) / 2, H / 2 - 40 * u), "SILVERHAND BUYS", font=f,
                    fill=(*GREEN, 255))
            for i, (line, col, ff) in enumerate(((BOT, INK, font("num", 38 * u)),
                                                 ("Built for vibe/vibe launches · try it on testnet", LIME,
                                                  font("ui", 28 * u)),
                                                 ("Not affiliated with Robinhood · NFA · DYOR", DIM, font("uir", 21 * u)))):
                ld.text(((W - text_w(ld, line, ff)) / 2, H / 2 + (110 + i * 56) * u), line, font=ff, fill=(*col, 255))
            paste_alpha(img, layer, (0, 0), a)
        return img


VARIANTS = {"classic": Promo, "terminal": Terminal, "hype": Hype, "phone": Phone}
