"""Draw the Silverhand custom emoji pack: animated 100x100 WEBM (VP9 with alpha), the format
Telegram takes for video custom emoji (at most 3 s, 30 fps, 64 KB).

    python tools/make_emoji.py            # assets/emoji/<name>.webm
    python tools/make_emoji.py --sheet x.png   # also a preview sheet on dark and light

One look for the whole pack: a metal badge (silver for facts, mint for buys, red for sells,
gold for the moments worth a mark) with a dark glyph, a slow shine across it, and one small
motion each. Frames are drawn at 4x and scaled down.
"""

import argparse
import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from buybot.chart import find_ffmpeg  # noqa: E402
from buybot.emoji import PACK  # noqa: E402

OUT = ROOT / "assets" / "emoji"
FONT_SANS = ROOT / "assets" / "fonts" / "InstrumentSans.ttf"
FONT_MONO = ROOT / "assets" / "fonts" / "JetBrainsMono.ttf"

S = 400            # drawing size; the emoji is 100
FPS = 30
FRAMES = 60        # a 2 s loop
C = S // 2

INK = (10, 16, 15, 255)
WHITE = (255, 255, 255, 255)
METALS = {  # top, bottom, rim
    "silver": ((236, 241, 239), (150, 162, 158), (98, 110, 106)),
    "mint": ((120, 255, 196), (22, 196, 118), (12, 128, 78)),
    "red": ((255, 128, 150), (214, 46, 80), (150, 24, 52)),
    "gold": ((255, 226, 128), (214, 160, 36), (148, 102, 12)),
}
GLYPH = {"silver": INK, "mint": INK, "red": WHITE, "gold": INK}


# ----------------------------------------------------------------------------- helpers
def ease(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def back_out(x, k=1.6):
    x = min(1.0, max(0.0, x)) - 1
    return 1 + (k + 1) * x ** 3 + k * x ** 2


def seg(t, a, b):
    return min(1.0, max(0.0, (t - a) / (b - a)))


def font(path, px, weight=None):
    f = ImageFont.truetype(str(path), px)
    if weight:
        try:
            f.set_variation_by_axes([weight] + [a["default"] for a in f.get_variation_axes()[1:]])
        except Exception:
            pass
    return f


def layer():
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))


def shape_mask(kind, inset=0):
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    box = (16 + inset, 16 + inset, S - 16 - inset, S - 16 - inset)
    if kind == "circle":
        d.ellipse(box, fill=255)
    else:
        d.rounded_rectangle(box, radius=96 - inset, fill=255)
    return m


def badge(metal, kind, t):
    """The metal disc or tile: a vertical gradient, a darker rim, a gloss on the top half
    and a shine that crosses it once a loop."""
    top, bot, rim = METALS[metal]
    grad = Image.new("RGBA", (1, S))
    for y in range(S):
        k = y / (S - 1)
        grad.putpixel((0, y), tuple(int(top[i] * (1 - k) + bot[i] * k) for i in range(3)) + (255,))
    grad = grad.resize((S, S))
    outer = shape_mask(kind)
    inner = shape_mask(kind, 12)
    rim_l = Image.new("RGBA", (S, S), rim + (255,))
    out = layer()
    out.paste(rim_l, (0, 0), outer)
    out.paste(grad, (0, 0), inner)
    gloss = layer()
    ImageDraw.Draw(gloss).ellipse((60, 34, S - 60, C + 10), fill=(255, 255, 255, 52))
    gloss = gloss.filter(ImageFilter.GaussianBlur(10))
    out = Image.alpha_composite(out, _clip(gloss, inner))
    # the shine: a soft diagonal band, crossing between 0.55 and 0.95 of the loop
    k = seg(t, 0.55, 0.95)
    if 0 < k < 1:
        band = layer()
        x = -S * 0.6 + k * S * 2.2
        ImageDraw.Draw(band).polygon([(x, -20), (x + 70, -20), (x - 130, S + 20), (x - 200, S + 20)],
                                     fill=(255, 255, 255, 110))
        out = Image.alpha_composite(out, _clip(band.filter(ImageFilter.GaussianBlur(14)), inner))
    return out, inner


def _clip(img, mask):
    a = ImageChops.multiply(img.getchannel("A"), mask)
    img = img.copy()
    img.putalpha(a)
    return img


def stroke(d, pts, width, fill):
    """A polyline with round caps and joins."""
    d.line(pts, fill=fill, width=width, joint="curve")
    r = width / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def text(d, xy, s, f, fill):
    d.text(xy, s, font=f, fill=fill, anchor="mm")


def on_badge(metal, kind, t, draw, clip=True):
    base, inner = badge(metal, kind, t)
    g = layer()
    draw(ImageDraw.Draw(g), g)
    if clip:
        g = _clip(g, inner)
    return Image.alpha_composite(base, g)


def shift(img, dx, dy):
    out = layer()
    out.paste(img, (int(dx), int(dy)), img)
    return out


# ----------------------------------------------------------------------------- the pack
def e_buy(t):
    def draw(d, g):
        k = seg(t, 0.15, 0.42)
        k2 = seg(t, 0.42, 0.7)
        dy = -260 * ease(k) if k < 1 else 230 * (1 - back_out(k2))
        ink = GLYPH["mint"]
        stroke(d, [(C, 300 + dy), (C, 120 + dy)], 46, ink)
        stroke(d, [(118, 196 + dy), (C, 114 + dy), (282, 196 + dy)], 46, ink)
    return on_badge("mint", "circle", t, draw)


def e_sell(t):
    def draw(d, g):
        k = seg(t, 0.15, 0.42)
        k2 = seg(t, 0.42, 0.7)
        dy = 260 * ease(k) if k < 1 else -230 * (1 - back_out(k2))
        ink = GLYPH["red"]
        stroke(d, [(C, 100 + dy), (C, 280 + dy)], 46, ink)
        stroke(d, [(118, 204 + dy), (C, 286 + dy), (282, 204 + dy)], 46, ink)
    return on_badge("red", "circle", t, draw)


def e_whale(t):
    bob = 9 * math.sin(2 * math.pi * t)
    flick = 0.12 * math.sin(2 * math.pi * t + 1.2)

    def draw(d, g):
        ink = GLYPH["gold"]
        light = METALS["gold"][0] + (255,)
        y = 232 + bob
        # a round head on the right, the body tapering into a raised tail on the left
        d.ellipse((150, y - 84, 336, y + 70), fill=ink)
        tx, ty = 84, y - 44 + 10 * flick       # where the tail meets the flukes
        d.polygon([(196, y - 74), (tx + 6, ty - 6), (tx + 22, ty + 22), (206, y + 66)], fill=ink)
        d.polygon([(tx + 16, ty + 12), (tx - 50, ty - 30 + 20 * flick), (tx - 30, ty + 14),
                   (tx - 2, ty + 22)], fill=ink)
        d.polygon([(tx + 2, ty + 4), (tx + 4, ty - 70 - 20 * flick), (tx + 34, ty - 26),
                   (tx + 30, ty + 10)], fill=ink)
        d.chord((170, y - 10, 334, y + 66), 20, 160, fill=light)        # the belly
        d.ellipse((270, y - 34, 292, y - 12), fill=light)               # the eye
        k = seg(t, 0.05, 0.5)                                           # the spout
        if 0 < k < 1:
            h = 84 * math.sin(math.pi * k)
            for sx in (-30, 30):
                stroke(d, [(250, y - 84), (250 + sx * 0.3, y - 84 - h * 0.7), (250 + sx, y - 84 - h)], 15, ink)
    return on_badge("gold", "circle", t, draw)


def e_dev(t):
    f = font(FONT_MONO, 170, 800)

    def draw(d, g):
        text(d, (C - 4, C + 6), "</>", f, GLYPH["gold"])
        if (t * 4) % 1 < 0.6:  # a cursor
            d.rounded_rectangle((C + 120, C + 70, C + 150, C + 84), 6, fill=GLYPH["gold"])
    return on_badge("gold", "tile", t, draw)


def _dollar(metal):
    f = font(FONT_SANS, 250, 700)

    def make(t):
        pop = 1 + 0.08 * math.sin(math.pi * seg(t, 0.0, 0.25))

        def draw(d, g):
            tmp = layer()
            text(ImageDraw.Draw(tmp), (C, C + 10), "$", f, GLYPH[metal])
            if pop != 1:
                big = tmp.resize((int(S * pop), int(S * pop)), Image.LANCZOS)
                o = (S - big.width) // 2
                tmp = layer()
                tmp.paste(big, (o, o), big)
            g.alpha_composite(tmp)
        return on_badge(metal, "tile", t, draw)
    return make


def e_coin(t):
    w = math.cos(2 * math.pi * t)
    out = layer()
    d = ImageDraw.Draw(out)
    half = max(10, abs(w) * 168)
    edge = (98, 110, 106, 255)
    thick = 26 * (1 - abs(w))
    d.ellipse((C - half - thick, 32, C + half + thick, S - 32), fill=edge)
    top, bot, rim = METALS["silver"] if w >= 0 else METALS["mint"]
    d.ellipse((C - half - 10, 22, C + half + 10, S - 22), fill=rim + (255,))
    d.ellipse((C - half, 32, C + half, S - 32), fill=top + (255,))
    inner = max(4, half - 34)
    d.ellipse((C - inner, 66, C + inner, S - 66), outline=bot + (255,), width=16)
    if half > 60:
        f = font(FONT_SANS, 200, 700)
        letter = layer()
        text(ImageDraw.Draw(letter), (C, C + 8), "S" if w >= 0 else "$", f, INK)
        letter = letter.resize((max(1, int(S * half / 168)), S), Image.LANCZOS)
        out.alpha_composite(letter, ((S - letter.width) // 2, 0))
    return out


def e_wallet(t):
    def draw(d, g):
        ink = GLYPH["silver"]
        d.ellipse((C - 62, 92, C + 62, 216), fill=ink)
        d.ellipse((C - 132, 236, C + 132, 470), fill=ink)
        k = seg(t, 0.1, 0.4)
        if 0 < k < 1:  # a ping
            r = 40 + 120 * k
            d.ellipse((300 - r / 3, 96 - r / 3, 300 + r / 3, 96 + r / 3), outline=METALS["mint"][1] + (int(255 * (1 - k)),), width=10)
        d.ellipse((282, 78, 318, 114), fill=METALS["mint"][1] + (255,))
    return on_badge("silver", "circle", t, draw)


def e_mc(t):
    def draw(d, g):
        ink = GLYPH["silver"]
        mint = (12, 150, 90, 255)
        for i, (x, h) in enumerate(((112, 110), (C, 170), (288, 236))):
            # full at rest (the first frame is the still), then down and up again
            k = 1 - ease(seg(t, 0.45, 0.55)) if t < 0.55 else back_out(seg(t, 0.58 + i * 0.08, 0.8 + i * 0.06))
            hh = 30 + h * k
            d.rounded_rectangle((x - 30, 308 - hh, x + 30, 308), 12, fill=mint if i == 2 else ink)
        d.rounded_rectangle((70, 310, 330, 330), 10, fill=ink)
    return on_badge("silver", "tile", t, draw)


def e_curve(t):
    sw = 26 * math.sin(2 * math.pi * t)

    def draw(d, g):
        ink = GLYPH["silver"]
        d.polygon([(C, 92), (346, 168), (C, 244), (54, 168)], fill=ink)           # the board
        d.pieslice((112, 150, 288, 330), 0, 180, fill=ink)                         # the cap
        d.rectangle((112, 186, 288, 240), fill=ink)
        gold = METALS["gold"][1] + (255,)
        end = (318 + sw * 0.4, 262 + abs(sw) * 0.2)
        stroke(d, [(C, 168), (310, 186), end], 12, gold)                           # the tassel
        d.ellipse((end[0] - 18, end[1] - 6, end[0] + 18, end[1] + 40), fill=gold)
    return on_badge("silver", "circle", t, draw)


def _sparkle(d, cx, cy, r, rot, fill):
    pts = []
    for i in range(8):
        a = rot + i * math.pi / 4
        rr = r if i % 2 == 0 else r * 0.28
        pts.append((cx + rr * math.sin(a), cy - rr * math.cos(a)))
    d.polygon(pts, fill=fill)


def e_new(t):
    out = layer()
    d = ImageDraw.Draw(out)
    top, bot, rim = METALS["gold"]
    s = 0.86 + 0.14 * math.sin(2 * math.pi * t)
    rot = 0.25 * math.sin(2 * math.pi * t)
    _sparkle(d, C - 20, C + 20, 170 * s + 10, rot, rim + (255,))
    _sparkle(d, C - 20, C + 20, 170 * s, rot, bot + (255,))
    _sparkle(d, C - 20, C + 6, 120 * s, rot, top + (255,))
    k = (t + 0.5) % 1
    s2 = math.sin(math.pi * k)
    _sparkle(d, 318, 92, 70 * s2, 0, top + (255,))
    return out


def e_bag(t):
    def draw(d, g):
        ink = GLYPH["silver"]
        d.rounded_rectangle((148, 96, 252, 160), 22, outline=ink, width=22)       # handle
        d.rounded_rectangle((70, 140, 330, 316), 34, fill=ink)
        d.rectangle((70, 210, 330, 222), fill=METALS["silver"][1] + (255,))
        d.rounded_rectangle((176, 196, 224, 238), 10, fill=METALS["mint"][1] + (255,))
    return on_badge("silver", "tile", t, draw)


def e_exit(t):
    def draw(d, g):
        ink = GLYPH["red"]
        stroke(d, [(196, 108), (116, 108), (116, 292), (196, 292)], 34, ink)      # the door
        k = seg(t, 0.15, 0.45)
        k2 = seg(t, 0.45, 0.75)
        dx = 200 * ease(k) if k < 1 else -160 * (1 - back_out(k2))
        stroke(d, [(176 + dx, C), (300 + dx, C)], 38, ink)
        stroke(d, [(250 + dx, 150), (304 + dx, C), (250 + dx, 250)], 38, ink)
    return on_badge("red", "circle", t, draw)


def e_dex(t):
    def draw(d, g):
        ink = GLYPH["silver"]
        mint = (12, 150, 90, 255)
        k = ease(seg(t, 0.1, 0.45)) - ease(seg(t, 0.55, 0.9))
        a, b = 40 * k, -40 * k
        stroke(d, [(92 + a, 150), (300 + a, 150)], 34, ink)
        stroke(d, [(250 + a, 104), (304 + a, 150), (250 + a, 196)], 34, ink)
        stroke(d, [(308 + b, 254), (100 + b, 254)], 34, mint)
        stroke(d, [(150 + b, 208), (96 + b, 254), (150 + b, 300)], 34, mint)
    return on_badge("silver", "circle", t, draw)


def e_chart(t):
    pts = [(78, 286), (140, 230), (190, 254), (246, 168), (322, 110)]

    def draw(d, g):
        ink = GLYPH["silver"]
        k = 1 if t < 0.45 else ease(seg(t, 0.5, 0.95))
        total = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        want, run = total * k, [pts[0]]
        for i in range(len(pts) - 1):
            seg_len = math.dist(pts[i], pts[i + 1])
            if want >= seg_len:
                run.append(pts[i + 1])
                want -= seg_len
            else:
                f = want / seg_len
                run.append((pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f,
                            pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f))
                break
        if len(run) > 1:
            stroke(d, run, 30, ink)
        x, y = run[-1]
        d.ellipse((x - 26, y - 26, x + 26, y + 26), fill=(12, 150, 90, 255))
    return on_badge("silver", "tile", t, draw)


def e_tx(t):
    a = 2 * math.pi * t
    dx, dy = 18 * math.cos(a), 18 * math.sin(a)

    def draw(d, g):
        ink = GLYPH["silver"]
        cx, cy = 176 + dx, 176 + dy
        d.ellipse((cx - 84, cy - 84, cx + 84, cy + 84), outline=ink, width=34)
        stroke(d, [(cx + 66, cy + 66), (cx + 140, cy + 140)], 46, ink)
        d.arc((cx - 52, cy - 52, cx + 52, cy + 52), 200, 260, fill=(12, 150, 90, 255), width=14)
    return on_badge("silver", "circle", t, draw)


def e_day(t):
    def draw(d, g):
        ink = GLYPH["silver"]
        d.ellipse((76, 76, 324, 324), outline=ink, width=28)
        a = 2 * math.pi * t
        stroke(d, [(C, C), (C + 96 * math.sin(a), C - 96 * math.cos(a))], 24, (12, 150, 90, 255))
        b = 2 * math.pi * (t / 12 + 0.33)
        stroke(d, [(C, C), (C + 62 * math.sin(b), C - 62 * math.cos(b))], 30, ink)
        d.ellipse((C - 20, C - 20, C + 20, C + 20), fill=ink)
    return on_badge("silver", "circle", t, draw)


def e_rocket(t):
    shake = 4 * math.sin(2 * math.pi * t * 6)
    lift = -14 * math.sin(2 * math.pi * t)

    def draw(d, g):
        ink = GLYPH["gold"]
        x, y = C + shake, 200 + lift
        flame = 50 + 26 * abs(math.sin(2 * math.pi * t * 3))
        d.polygon([(x - 34, y + 92), (x, y + 92 + flame), (x + 34, y + 92)], fill=(255, 77, 109, 255))
        d.polygon([(x - 18, y + 92), (x, y + 92 + flame * 0.6), (x + 18, y + 92)], fill=(255, 236, 160, 255))
        d.polygon([(x - 50, y + 40), (x - 92, y + 108), (x - 40, y + 96)], fill=ink)   # fins
        d.polygon([(x + 50, y + 40), (x + 92, y + 108), (x + 40, y + 96)], fill=ink)
        d.ellipse((x - 54, y - 140, x + 54, y + 100), fill=ink)                         # body
        d.ellipse((x - 26, y - 56, x + 26, y - 4), fill=METALS["gold"][0] + (255,))     # window
    return on_badge("gold", "circle", t, draw)


def e_more(t):
    rot = 90 * ease(seg(t, 0.1, 0.4))

    def draw(d, g):
        tmp = layer()
        dd = ImageDraw.Draw(tmp)
        stroke(dd, [(C, 110), (C, 290)], 48, GLYPH["mint"])
        stroke(dd, [(110, C), (290, C)], 48, GLYPH["mint"])
        g.alpha_composite(tmp.rotate(-rot, resample=Image.BICUBIC, center=(C, C)))
    return on_badge("mint", "circle", t, draw)


DRAW = {
    "buy": e_buy, "sell": e_sell, "whale": e_whale, "dev": e_dev, "cash": _dollar("mint"),
    "sold": _dollar("gold"), "coin": e_coin, "wallet": e_wallet, "mc": e_mc, "curve": e_curve,
    "new": e_new, "bag": e_bag, "exit": e_exit, "dex": e_dex, "chart": e_chart, "tx": e_tx,
    "day": e_day, "rocket": e_rocket, "more": e_more,
}


def frames(name):
    for i in range(FRAMES):
        yield DRAW[name](i / FRAMES).resize((100, 100), Image.LANCZOS)


def encode(name, path: Path) -> int:
    raw = b"".join(f.tobytes() for f in frames(name))
    for crf in (30, 36, 42, 48):
        cmd = [find_ffmpeg(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
               "-s", "100x100", "-r", str(FPS), "-i", "-", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
               "-b:v", "0", "-crf", str(crf), "-deadline", "good", "-row-mt", "1",
               "-auto-alt-ref", "0", "-an", str(path)]
        subprocess.run(cmd, input=raw, check=True)
        if path.stat().st_size <= 60_000:
            return path.stat().st_size
    raise SystemExit(f"{name}: over 64 KB even at crf 48")


def sheet(path: Path) -> None:
    """Every emoji at three moments, big, on Telegram's dark and light backgrounds, and
    once at the size it shows in a post."""
    names = list(DRAW)
    cell = 120
    w = cell * 6 + 40
    img = Image.new("RGBA", (w * 2, cell * len(names) + 20), (0, 0, 0, 255))
    for half, bg in enumerate(((24, 33, 43), (255, 255, 255))):
        ImageDraw.Draw(img).rectangle((half * w, 0, half * w + w, img.height), fill=bg + (255,))
        for r, name in enumerate(names):
            for c, tt in enumerate((0.0, 0.3, 0.75)):
                fr = DRAW[name](tt).resize((100, 100), Image.LANCZOS)
                img.alpha_composite(fr, (half * w + 10 + c * cell, 10 + r * cell))
            small = DRAW[name](0.0).resize((22, 22), Image.LANCZOS)
            img.alpha_composite(small, (half * w + 10 + 3 * cell + 40, 10 + r * cell + 40))
    img.convert("RGB").save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet")
    ap.add_argument("--only")
    a = ap.parse_args()
    missing = set(PACK) ^ set(DRAW)
    if missing:
        raise SystemExit(f"pack and drawings differ: {sorted(missing)}")
    if a.sheet:
        sheet(Path(a.sheet))
        print("sheet", a.sheet)
        return
    OUT.mkdir(parents=True, exist_ok=True)
    for name in DRAW:
        if a.only and name != a.only:
            continue
        size = encode(name, OUT / f"{name}.webm")
        print(f"{name:8} {size / 1024:5.1f} KB")


if __name__ == "__main__":
    main()
