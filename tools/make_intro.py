"""Animated description picture for @BotFather (/setdescriptionpic) and the
animations of the bot's menu screens.

    python tools/make_intro.py          -> assets/brand/botfather-description.gif and .mp4
    python tools/make_intro.py --menu   -> assets/menu/<lang>/<screen>.mp4, one per screen in MENU

A three-second loop at 640x360 with no visible seam: signal tape at the top and
bottom, a grid with transaction packets and buys popping up over its nodes, the
avatar in a spinning ring, and a big condensed title with depth, a glint and a
short glitch. Every motion fits exactly into one period.

Needs Pillow and ffmpeg (pip install imageio-ffmpeg, or ffmpeg on PATH). Fonts:
Bahnschrift and Consolas on Windows, DejaVu elsewhere.
"""

import argparse
import math
import random
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "assets" / "brand"

# Brand colors (the same green as tools/make_brand.py).
BG = (4, 9, 7)
ACC = (45, 245, 154)
HI = (190, 255, 222)
DEEP = (6, 128, 74)
EX = (3, 44, 26)
INK = (236, 244, 240)
GLINT = (228, 255, 240)
TAPE_K = (5, 10, 8)

DISPLAY_FONTS = [
    ("C:/Windows/Fonts/bahnschrift.ttf", "Bold Condensed"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf", None),
]
MONO_FONTS = [
    "C:/Windows/Fonts/consolab.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
]
# Menu screens of the private chat (buybot/bot.py shows assets/menu/<lang>/<key>.mp4):
# lang -> key -> (title, line under it). "lang" is the language picker, the same in both.
MENU = {
    "en": {
        "start":   ("SILVERHAND BUYS", "EVERY BUY · LIVE IN YOUR CHAT"),
        "chats":   ("MY CHATS", "GROUPS · CHANNELS · SETTINGS"),
        "stats":   ("STATISTICS", "BUYS · SELLS · VOLUME · 24H"),
        "help":    ("HOW IT WORKS", "TRACK · TEST · SETTINGS"),
        "terms":   ("TERMS", "NO ADS · SOURCE · NFA"),
        "support": ("SUPPORT", "QUESTIONS · PROBLEMS"),
        "lang":    ("LANGUAGE", "ENGLISH · РУССКИЙ"),
    },
    "ru": {
        "start":   ("SILVERHAND BUYS", "КАЖДАЯ ПОКУПКА · В ВАШЕМ ЧАТЕ"),
        "chats":   ("МОИ ЧАТЫ", "ГРУППЫ · КАНАЛЫ · НАСТРОЙКИ"),
        "stats":   ("СТАТИСТИКА", "ПОКУПКИ · ПРОДАЖИ · ОБЪЁМ · 24Ч"),
        "help":    ("КАК ЭТО РАБОТАЕТ", "TRACK · TEST · НАСТРОЙКИ"),
        "terms":   ("УСЛОВИЯ", "БЕЗ РЕКЛАМЫ · ИСХОДНИКИ · NFA"),
        "support": ("ПОДДЕРЖКА", "ВОПРОСЫ · ПРОБЛЕМЫ"),
    },
}
MENU_EYEBROW = {"en": "// BUY BOT", "ru": "// БОТ ПОКУПОК"}
# Buys that pop up over the grid: decoration, not real trades.
POPS = ["+0.42 ETH", "+1.20 ETH", "+0.08 ETH", "+$360", "+2.05 ETH"]


def display_font(size):
    for path, variation in DISPLAY_FONTS:
        if Path(path).exists():
            f = ImageFont.truetype(path, size)
            if variation:
                try:
                    f.set_variation_by_name(variation)
                except Exception:
                    try:
                        f.set_variation_by_axes([700, 75])
                    except Exception:
                        pass
            return f
    sys.exit("No condensed font for the title (Bahnschrift ships with Windows 10+)")


def mono_font(size):
    for path in MONO_FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def mix(a, b, k):
    """k of color a over color b."""
    return tuple(round(x * k + y * (1 - k)) for x, y in zip(a, b))


def spaced(draw, xy, text, font, fill, tracking):
    """Letter-spaced text: Pillow has no tracking, so letters go one by one."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x


def spaced_width(draw, text, font, tracking):
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * max(0, len(text) - 1)


def vgradient(w, h, stops):
    """Vertical gradient through [(fraction, color), ...]."""
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        f = y / max(1, h - 1)
        for (f0, c0), (f1, c1) in zip(stops, stops[1:]):
            if f0 <= f <= f1:
                px[0, y] = mix(c1, c0, (f - f0) / max(1e-6, f1 - f0))
                break
    return col.resize((w, h))


def tape(w, band, phase):
    """Signal tape; phase 0..1 moves the stripes by exactly one period."""
    img = Image.new("RGB", (w, band), TAPE_K)
    d = ImageDraw.Draw(img)
    p = band * 2
    off = phase * p
    for x0 in range(-3 * p, w + 3 * p, p):
        x = x0 + off
        d.polygon([(x, 0), (x + p / 2, 0), (x + p / 2 - band, band), (x - band, band)], fill=ACC)
    return img


def circle_logo(path, size):
    logo = Image.open(path).convert("RGB")
    side = min(logo.size)
    logo = logo.crop(((logo.width - side) // 2, (logo.height - side) // 2,
                      (logo.width + side) // 2, (logo.height + side) // 2))
    logo = logo.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    return logo, mask.resize((size, size), Image.LANCZOS)


class Scene:
    def __init__(self, W, H, S, title, eyebrow, sub, logo_path, seed=7, pad_left=0):
        """pad_left keeps the logo and text clear of the image's left edge (in layout
        units), e.g. where X lays the profile picture over its header."""
        self.W, self.H, self.S = W, H, S
        self.w, self.h = w, h = W * S, H * S
        self.band = 12 * S
        self.title, self.eyebrow, self.sub = title, eyebrow, sub
        rnd = random.Random(seed)

        # Still background: glow, dot grid, addresses.
        bg = Image.new("RGB", (w, h), BG)
        glow = Image.new("L", (w // 8, h // 8), 0)
        gd = ImageDraw.Draw(glow)
        gw, gh = glow.size
        gd.ellipse((gw * .12, -gh * .55, gw * .88, gh * .62), fill=120)
        gd.ellipse((gw * .25, gh * .8, gw * .75, gh * 1.4), fill=55)
        glow = glow.filter(ImageFilter.GaussianBlur(gw * .09)).resize((w, h), Image.BILINEAR)
        bg = Image.composite(Image.new("RGB", (w, h), ACC), bg, glow.point(lambda v: v * .30))
        d = ImageDraw.Draw(bg)
        g = 28 * S
        dot = mix(ACC, BG, .2)
        for x in range(g, w, g):
            for y in range(g, h, g):
                d.rectangle((x - S // 2, y - S // 2, x + S // 2, y + S // 2), fill=dot)
        hexf = mono_font(26 * S)
        hexc = mix(ACC, BG, .07)
        for _ in range(5):
            s = "0x" + "".join(rnd.choice("0123456789abcdef") for _ in range(16))
            d.text((rnd.randint(-70 * S, w - 180 * S), rnd.randint(self.band, h - self.band - 36 * S)),
                   s, font=hexf, fill=hexc)
        rowc = mix(ACC, BG, .14)
        for _ in range(3):
            y = rnd.randint(2, h // g - 2) * g
            for x in range(0, w, 8 * S):
                d.rectangle((x, y, x + 2 * S, y + S - 1), fill=rowc)
        self.bg = bg

        # Nodes, packet paths and the buys that pop up over some nodes.
        cols, rows_ = w // g, h // g
        self.nodes = []
        for _ in range(9):
            nx = rnd.randint(1, cols - 1) * g
            ny = rnd.randint(2, rows_ - 2) * g
            self.nodes.append((nx, ny, rnd.random() * math.tau,
                               "".join(rnd.choice("0123456789abcdef") for _ in range(4))))
        self.packets = []
        for i in range(7):
            a, b = rnd.sample(self.nodes, 2)
            self.packets.append(((a[0], a[1]), (b[0], a[1]), (b[0], b[1]), i / 7, rnd.choice((1, 2))))
        self.node_font = mono_font(9 * S)
        self.pop_font = mono_font(11 * S)

        # Logo, title and captions.
        self.logo_size = 96 * S
        self.logo, self.logo_mask = circle_logo(logo_path, self.logo_size)
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        self.ey_font = mono_font(12 * S)
        self.sub_font = mono_font(13 * S)
        self.ey_track = 3 * S
        self.sub_track = 1.6 * S
        ey_w = spaced_width(probe, eyebrow, self.ey_font, self.ey_track)
        sub_w = spaced_width(probe, sub, self.sub_font, self.sub_track) + 14 * S
        gap = 22 * S
        avail = w - pad_left * S
        room = avail - 2 * 26 * S - self.logo_size - gap
        t_lines, self.t_font, boxes = self._fit_title(probe, title, room)
        line_gap = int(self.t_font.size * .06)
        tw = max(b[2] - b[0] for b in boxes)
        th = sum(b[3] - b[1] for b in boxes) + line_gap * (len(boxes) - 1)
        block_w = max(tw, ey_w, sub_w)
        total = self.logo_size + gap + block_w
        x0 = pad_left * S + int(avail - total) // 2
        self.logo_xy = (x0, (h - self.logo_size) // 2)
        tx = x0 + self.logo_size + gap
        ey_h, sub_h = 15 * S, 17 * S
        block_h = ey_h + 11 * S + th + 16 * S + sub_h
        ty0 = (h - block_h) // 2
        self.ey_xy = (tx, ty0)
        ty = ty0 + ey_h + 11 * S
        self.sub_xy = (tx, ty + th + 16 * S)
        self.title_rect = (tx, ty, tx + tw, ty + th)

        # Buys pop up over nodes of their own, placed clear of the logo, the
        # text and the tapes, so they never run under the captions.
        clear = (x0 - 14 * S, ty0 - 12 * S, x0 + total + 14 * S, self.sub_xy[1] + sub_h + 12 * S)
        spots = [(x, y) for x in range(g, w - 80 * S, g) for y in range(3 * g, h - g, g)
                 if (x + 90 * S < clear[0] or x - 14 * S > clear[2]
                     or y < clear[1] or y - 50 * S > clear[3])
                 and all((x, y) != n[:2] for n in self.nodes)]
        rnd.shuffle(spots)
        anchors = []
        for x, y in spots:
            if all(abs(x - ax) > 110 * S or abs(y - ay) > 70 * S for ax, ay in anchors):
                anchors.append((x, y))
            if len(anchors) == len(POPS):
                break
        self.nodes += [(x, y, rnd.random() * math.tau,
                        "".join(rnd.choice("0123456789abcdef") for _ in range(4))) for x, y in anchors]
        self.pops = [(x, y, i / len(POPS), text) for i, ((x, y), text) in enumerate(zip(anchors, POPS))]

        # Title mask and still layers: gradient with line texture, depth, glow.
        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        fill = Image.new("RGB", (w, h), ACC)
        y = ty
        for text_line, b in zip(t_lines, boxes):
            lw, lh = b[2] - b[0], b[3] - b[1]
            md.text((tx - b[0], y - b[1]), text_line, font=self.t_font, fill=255)
            fill.paste(vgradient(lw + 2, lh + 2, [(0, HI), (.5, ACC), (1, DEEP)]), (tx - 1, y - 1))
            y += lh + line_gap
        self.t_mask = mask
        x1, y1, x2, y2 = self.title_rect
        lines = Image.new("L", (w, h), 0)
        ld = ImageDraw.Draw(lines)
        for y in range(y1, y2 + 1, 4 * S):
            ld.rectangle((0, y, w, y + S - 1), fill=52)
        self.t_fill = Image.composite(Image.new("RGB", (w, h), (0, 0, 0)), fill, lines)
        self.t_glow = mask.filter(ImageFilter.GaussianBlur(14 * S)).point(lambda v: min(255, v * 1.1))

        # Scanlines: a dark line every three pixels.
        scan = Image.new("L", (w, h), 0)
        sd = ImageDraw.Draw(scan)
        for y in range(0, h, 3 * S):
            sd.rectangle((0, y, w, y + S - 1), fill=40)
        self.scan = scan

    def _fit_title(self, probe, title, room):
        """The largest size at which the title fits; one line or, when that
        comes out small, two lines split at the best word boundary."""
        S = self.S

        def fit(lines, top):
            size = top
            while True:
                f = display_font(size)
                boxes = [probe.textbbox((0, 0), ln, font=f) for ln in lines]
                if max(b[2] - b[0] for b in boxes) <= room or size <= 30 * S:
                    return size, f, boxes
                size -= 2 * S

        one = fit([title], 130 * S)
        words = title.split()
        if one[0] >= 68 * S or len(words) < 2:
            return [title], one[1], one[2]
        cut = min(range(1, len(words)),
                  key=lambda i: max(len(" ".join(words[:i])), len(" ".join(words[i:]))))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
        two = fit(lines, 84 * S)
        return (lines, two[1], two[2]) if two[0] > one[0] else ([title], one[1], one[2])

    def frame(self, t):
        """t is the fraction of the period, 0..1; frames at t=0 and t=1 match."""
        S, w, h = self.S, self.w, self.h
        img = self.bg.copy()
        d = ImageDraw.Draw(img)

        # Packets: a bright head and a tail along an L-shaped path.
        for p0, p1, p2, off, speed in self.packets:
            seg1 = abs(p1[0] - p0[0])
            seg2 = abs(p2[1] - p1[1])
            L = seg1 + seg2
            if L < 10:
                continue
            prog = ((t * speed + off) % 1.0) * 1.25   # the last quarter is a pause
            for k in range(12):
                dd = prog * L - k * 6 * S
                if dd < 0 or dd > L:
                    continue
                if dd <= seg1:
                    x = p0[0] + (p1[0] - p0[0]) * (dd / max(1, seg1))
                    y = p0[1]
                else:
                    x = p1[0]
                    y = p1[1] + (p2[1] - p1[1]) * ((dd - seg1) / max(1, seg2))
                col = mix(ACC, BG, (1 - k / 12) * .85)
                r = (2 if k else 3) * S
                d.rectangle((x - r, y - r, x + r, y + r), fill=col)

        # Nodes: frame, pulsing core, label.
        edge = mix(ACC, BG, .35)
        for nx, ny, ph, label in self.nodes:
            pulse = .5 + .5 * math.sin(math.tau * t * 2 + ph)
            core = mix(ACC, BG, .25 + .45 * pulse)
            r = 8 * S
            d.rectangle((nx - r, ny - r, nx + r, ny + r), outline=edge, width=S)
            d.rectangle((nx - 4 * S, ny - 4 * S, nx + 4 * S, ny + 4 * S), fill=core)
            d.text((nx + 12 * S, ny - 16 * S), label, font=self.node_font, fill=edge)

        # Buys: rise from their node and fade out, each at its own moment.
        for nx, ny, off, text in self.pops:
            k = (t - off) % 1.0
            if k < .5:
                f = k / .5
                a = min(1.0, f * 6) * (1 - f) ** 1.2
                d.text((nx - 14 * S, ny - 14 * S - f * 30 * S), text, font=self.pop_font,
                       fill=mix(ACC, BG, .95 * a))

        # Logo in a ring: the glow breathes, the arc turns.
        lx, ly = self.logo_xy
        ls = self.logo_size
        cx, cy = lx + ls / 2, ly + ls / 2
        breathe = .55 + .45 * (.5 + .5 * math.sin(math.tau * t))
        halo = Image.new("L", (w, h), 0)
        ImageDraw.Draw(halo).ellipse((lx - 10 * S, ly - 10 * S, lx + ls + 10 * S, ly + ls + 10 * S), fill=255)
        halo = halo.filter(ImageFilter.GaussianBlur(20 * S)).point(lambda v: v * .5 * breathe)
        img = Image.composite(Image.new("RGB", (w, h), ACC), img, halo)
        d = ImageDraw.Draw(img)
        rr = ls / 2 + 8 * S
        base = 360 * t
        steps = 48
        for i in range(steps):
            a0 = base + i * (150 / steps)
            d.arc((cx - rr, cy - rr, cx + rr, cy + rr), a0, a0 + 150 / steps + .8,
                  fill=mix(ACC, BG, i / steps), width=3 * S)
        d.ellipse((lx - 2 * S, ly - 2 * S, lx + ls + 2 * S, ly + ls + 2 * S), fill=(0, 0, 0))
        img.paste(self.logo, (lx, ly), self.logo_mask)

        # Title: glow, then depth, then the gradient, then the glint.
        img = Image.composite(Image.new("RGB", (w, h), ACC), img, self.t_glow.point(lambda v: v * .42))
        depth = 6 * S
        for k in range(1, depth, max(1, S // 2)):
            img.paste(EX, (0, k), self.t_mask.crop((0, 0, w, h - k)))
        img.paste((0, 0, 0), (0, depth), self.t_mask.crop((0, 0, w, h - depth)))
        img.paste(self.t_fill, (0, 0), self.t_mask)
        x1, y1, x2, y2 = self.title_rect
        span = (x2 - x1) + 240 * S
        sx = x1 - 120 * S + span * t
        band = Image.new("L", (w, h), 0)
        ImageDraw.Draw(band).polygon([(sx, y1 - 10 * S), (sx + 30 * S, y1 - 10 * S),
                                      (sx + 30 * S - 56 * S, y2 + 10 * S), (sx - 56 * S, y2 + 10 * S)],
                                     fill=150)
        band = ImageChops.multiply(band.filter(ImageFilter.GaussianBlur(9 * S)), self.t_mask)
        img = Image.composite(Image.new("RGB", (w, h), GLINT), img, band)

        # Glitch: for two short moments strips of the title slide sideways.
        if 0.70 <= t < 0.74 or 0.76 <= t < 0.78:
            rnd = random.Random(int(t * 1000))
            # Strips come from the top of the letters only: lower down is the
            # depth layer, and shifted it leaves a dirty black copy below.
            top = y1 + int((y2 - y1) * .72)
            for _ in range(2):
                gh = rnd.randint(5 * S, 12 * S)
                gy = rnd.randint(y1, max(y1 + 1, top - gh))
                dx = rnd.choice((-1, 1)) * rnd.randint(4 * S, 9 * S)
                strip = img.crop((x1 - 18 * S, gy, x2 + 18 * S, gy + gh))
                img.paste(Image.blend(strip, Image.new("RGB", strip.size, HI), .25), (x1 - 18 * S + dx, gy))

        d = ImageDraw.Draw(img)
        # The line above the title, and the one below with a blinking cursor.
        spaced(d, self.ey_xy, self.eyebrow, self.ey_font, ACC, self.ey_track)
        end = spaced(d, self.sub_xy, self.sub, self.sub_font, INK, self.sub_track)
        if (t * 4) % 1 < .5:
            d.rectangle((end + 4 * S, self.sub_xy[1] + 2 * S, end + 12 * S, self.sub_xy[1] + 16 * S), fill=ACC)

        img = Image.composite(Image.new("RGB", (w, h), (0, 0, 0)), img, self.scan)
        img.paste(tape(w, self.band, t), (0, 0))
        img.paste(tape(w, self.band, t), (0, h - self.band))
        return img.resize((self.W, self.H), Image.LANCZOS)


def find_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def encode(frames, W, H, fps, out_gif, out_mp4, crf=18):
    exe = find_ffmpeg()
    if not exe:
        sys.exit("ffmpeg is needed: pip install imageio-ffmpeg")
    raw = b"".join(f.tobytes() for f in frames)
    src = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-"]
    if out_gif:
        subprocess.run([exe, "-y", "-loglevel", "error", *src,
                        "-filter_complex",
                        "split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];"
                        "[b][p]paletteuse=dither=sierra2_4a",
                        "-loop", "0", str(out_gif)], input=raw, check=True)
    if out_mp4:
        subprocess.run([exe, "-y", "-loglevel", "error", *src,
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(crf),
                        "-preset", "slow", "-movflags", "+faststart", "-an", str(out_mp4)],
                       input=raw, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--title", default="SILVERHAND BUYS")
    ap.add_argument("--eyebrow", default="// BUY BOT")
    ap.add_argument("--sub", default="EVERY BUY · LIVE IN YOUR CHAT")
    ap.add_argument("--out", default=str(BRAND / "botfather-description"),
                    help="output path without extension")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--logo", default=str(BRAND / "avatar.png"))
    ap.add_argument("--no-mp4", action="store_true")
    ap.add_argument("--still", type=float, action="append", default=[],
                    help="also save the frame at this fraction of the loop as PNG (repeatable)")
    ap.add_argument("--menu", action="store_true", help="build the menu screens from MENU")
    ap.add_argument("--menu-dir", default=str(ROOT / "assets" / "menu"))
    ap.add_argument("--gif", action="store_true", help="with --menu: GIFs too, for previews")
    a = ap.parse_args()

    W = a.width - a.width % 2
    H = (W * 9 // 16) // 2 * 2
    n = max(2, round(a.fps * a.seconds))
    if a.menu:
        jobs = [(Path(a.menu_dir) / lang / key, MENU_EYEBROW[lang], title, sub, a.gif, True)
                for lang, screens in MENU.items() for key, (title, sub) in screens.items()]
        for lang in MENU:
            (Path(a.menu_dir) / lang).mkdir(parents=True, exist_ok=True)
    else:
        jobs = [(Path(a.out), a.eyebrow, a.title, a.sub, True, not a.no_mp4)]
    for out, eyebrow, title, sub, want_gif, want_mp4 in jobs:
        scene = Scene(W, H, 2, title, eyebrow, sub, a.logo)
        for t in a.still:
            scene.frame(t).save(out.with_name(f"{out.name}-{t:.2f}.png"))
        frames = [scene.frame(i / n) for i in range(n)]
        gif = out.with_suffix(".gif") if want_gif else None
        mp4 = out.with_suffix(".mp4") if want_mp4 else None
        encode(frames, W, H, a.fps, gif, mp4, crf=23 if a.menu else 18)
        print(", ".join(f"{p.name} {p.stat().st_size / 1024:.0f} KB" for p in (gif, mp4) if p)
              + f", title size {scene.t_font.size // 2}")


if __name__ == "__main__":
    main()
