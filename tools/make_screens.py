"""The testnet bot's screens in the trade card's style (buybot/card.py, theme "vibe"):
the private chat's menu animations and the @BotFather description picture.

    python tools/make_screens.py            -> assets/menu/en/<screen>.mp4 (MENU in make_intro.py)
    python tools/make_screens.py --brand    -> assets/brand/botfather-description-testnet.gif/.mp4/.png
    python tools/make_screens.py --still 0.3 --only start   (a PNG to check, nothing encoded)

A three-second loop at 640x360 with no seam, drawn at 2x and scaled down. The card's
header (avatar, name, the network and launchpad pills, TESTNET), the screen's title and
line, and on the right a small market cap chart whose candles rise while the bar under
the header fills to graduation; at the top the bar flashes, then everything fades back to
where the loop began. Premium motion: eased (never linear) moves of 350-600 ms, no overshoot.
Frames go straight into ffmpeg.
"""

import argparse
import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from buybot.card import THEMES, font  # noqa: E402
from make_intro import MENU, find_ffmpeg  # noqa: E402

PAL = THEMES["vibe"]
W, H, S = 640, 360, 2          # output size; drawn at S times that
AVATAR = ROOT / "assets" / "brand" / "avatar.png"
# the candles of the little chart: (open, close) in 0..1 of its height, a steady climb
CANDLES = [(.08, .16), (.16, .12), (.12, .22), (.22, .30), (.30, .27), (.27, .38), (.38, .47),
           (.47, .43), (.43, .55), (.55, .63), (.63, .60), (.60, .72), (.72, .83), (.83, .94)]
# the header already says SILVERHAND BUYS: the first screen and the description say what it does
FRONT = ("EVERY BUY, LIVE", "VIBE/VIBE LAUNCHES · IN YOUR CHAT")
FILL_END, FLASH_END = .72, .86     # the loop: fill to graduation, flash, fade back
START = .62                        # the first frame shows the chart nearly full (a preview)


def ease_io(p):
    p = min(max(p, 0.0), 1.0)
    return p * p * (3 - 2 * p)


def ease_out(p):
    p = min(max(p, 0.0), 1.0)
    return 1 - (1 - p) ** 3


def mix(a, b, k):
    """k of colour a over colour b."""
    return tuple(round(x * k + y * (1 - k)) for x, y in zip(a, b))


def spaced(d, xy, text, f, fill, tr):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill)
        x += d.textlength(ch, font=f) + tr
    return x


def spaced_w(d, text, f, tr):
    return sum(d.textlength(ch, font=f) for ch in text) + tr * max(0, len(text) - 1)


def circle(path, size):
    im = Image.open(path).convert("RGB")
    side = min(im.size)
    im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                  (im.width + side) // 2, (im.height + side) // 2)).resize((size, size), Image.LANCZOS)
    m = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(m).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    return im, m.resize((size, size), Image.LANCZOS)


class Screen:
    def __init__(self, title: str, line: str):
        self.title, self.line = title, line
        w, h = W * S, H * S
        self.w, self.h = w, h
        # still layers: page with a soft lime glow, the card panel, the header
        bg = Image.new("RGB", (w, h), PAL["page"])
        glow = Image.new("L", (w // 8, h // 8), 0)
        ImageDraw.Draw(glow).ellipse((w // 8 * .45, -h // 8 * .6, w // 8 * 1.2, h // 8 * .55), fill=70)
        glow = glow.filter(ImageFilter.GaussianBlur(w // 8 * .08)).resize((w, h), Image.BILINEAR)
        bg = Image.composite(Image.new("RGB", (w, h), PAL["accent"]), bg, glow.point(lambda v: v * .18))
        d = ImageDraw.Draw(bg)
        self.panel = (14 * S, 92 * S, w - 14 * S, h - 14 * S)
        d.rounded_rectangle(self.panel, radius=14 * S, fill=PAL["card"], outline=PAL["line"], width=S)
        # header: avatar, brand, slogan; pills on the right
        av, mask = circle(AVATAR, 40 * S)
        bg.paste(av, (16 * S, 14 * S), mask)
        d.text((64 * S, 15 * S), "SILVERHAND BUYS", font=font("title", 17 * S), fill=PAL["text"])
        d.text((64 * S, 37 * S), "Not paper hands. Silver hands.", font=font("uir", 11 * S), fill=PAL["dim"])
        x = w - 16 * S
        for label, filled in (("TESTNET", True), ("vibe/vibe", False), ("Robinhood Chain testnet", False)):
            f = font("ui" if not filled else "title", 11 * S)
            tw = d.textlength(label, font=f)
            dot = 12 * S if label.startswith("Robinhood") else 0
            x0 = x - tw - dot - 20 * S
            box = (x0, 16 * S, x, 38 * S)
            if filled:
                d.rounded_rectangle(box, radius=7 * S, fill=PAL["accent"])
                d.text((x0 + 10 * S, 19 * S), label, font=f, fill=PAL["ink"])
            else:
                d.rounded_rectangle(box, radius=7 * S, fill=PAL["tile"], outline=PAL["line2"], width=S)
                if dot:
                    d.ellipse((x0 + 10 * S, 24 * S, x0 + 16 * S, 30 * S), fill=PAL["accent"])
                d.text((x0 + 10 * S + dot, 19 * S), label, font=f, fill=PAL["text"])
            x = x0 - 8 * S
        # the bar's track
        self.bar = (16 * S, 66 * S, w - 150 * S, 72 * S)
        d.rounded_rectangle(self.bar, radius=3 * S, fill=PAL["line"])
        # title and line in the panel's left half; they never move
        px0, py0, px1, py1 = self.panel
        size = 44 * S
        f_title = font("title", size)
        while d.textlength(title, font=f_title) > (px1 - px0) * .52 and size > 26 * S:
            size -= 2 * S
            f_title = font("title", size)
        self.f_title = f_title
        tb = d.textbbox((0, 0), title, font=f_title)
        self.title_xy = (px0 + 22 * S - tb[0], py0 + 70 * S - tb[1])
        self.title_box = (px0 + 22 * S, py0 + 70 * S, px0 + 22 * S + tb[2] - tb[0], py0 + 70 * S + tb[3] - tb[1])
        d.text(self.title_xy, title, font=f_title, fill=PAL["text"])
        f_line = font("mono", 11 * S)
        spaced(d, (px0 + 24 * S, self.title_box[3] + 16 * S), line, f_line, PAL["dim"], 1 * S)
        spaced(d, (px0 + 24 * S, py0 + 26 * S), "// BUY BOT", font("num", 11 * S), PAL["accent"], 2 * S)
        # chart area on the right: dashed graduation line, axis
        self.chart = (px0 + (px1 - px0) * .60, py0 + 24 * S, px1 - 22 * S, py1 - 26 * S)
        cx0, cy0, cx1, cy1 = self.chart
        for xx in range(int(cx0), int(cx1), 10 * S):
            d.line([(xx, cy0 + 6 * S), (min(cx1, xx + 5 * S), cy0 + 6 * S)], fill=mix(PAL["grad"], PAL["card"], .45), width=S)
        f_axis = font("num", 9 * S)
        d.text((cx0, cy0 - 8 * S), "GRADUATION", font=f_axis, fill=mix(PAL["grad"], PAL["card"], .7))
        d.line([(cx0, cy1), (cx1, cy1)], fill=PAL["line2"], width=S)
        self.bg = bg
        self.f_pct = font("num", 11 * S)

    def frame(self, t: float) -> Image.Image:
        """t: 0..1 of the loop; t=0 and t=1 are the same picture."""
        u = (t + START) % 1.0
        if u < FILL_END:
            level, flash, fade = ease_io(u / FILL_END), 0.0, 1.0
        elif u < FLASH_END:
            level, flash, fade = 1.0, 1 - (u - FILL_END) / (FLASH_END - FILL_END), 1.0
        else:
            level, flash, fade = 1.0, 0.0, 1 - ease_io((u - FLASH_END) / (1 - FLASH_END))
        img = self.bg.copy()
        d = ImageDraw.Draw(img)
        # the graduation bar and its percent
        bx0, by0, bx1, by1 = self.bar
        if level > .002 and fade > .01:
            col = mix(mix((255, 255, 236), PAL["accent"], flash * .8), PAL["line"], fade)
            d.rounded_rectangle((bx0, by0, bx0 + (bx1 - bx0) * level, by1), radius=3 * S, fill=col)
        pct = f"{level * 100:4.1f}%" if u < FILL_END else "GRADUATED"
        pcol = mix(PAL["accent"], PAL["page"], fade) if u >= FILL_END else PAL["accent"]
        end = spaced(d, (bx1 + 12 * S, by0 - 6 * S), pct, self.f_pct, pcol, 1 * S)
        if u < FILL_END:
            spaced(d, (end + 6 * S, by0 - 6 * S), "TO GRAD", self.f_pct, PAL["dim"], 1 * S)
        # candles: each rises in as the bar passes it, all fade together at the end
        cx0, cy0, cx1, cy1 = self.chart
        n = len(CANDLES)
        step = (cx1 - cx0) / n
        top = cy0 + 6 * S
        for i, (o, c) in enumerate(CANDLES):
            k = ease_out((level * n - i) / 1.2)
            a = k * fade
            if a <= .01:
                continue
            x = cx0 + step * i + step * .2
            yo = cy1 - (cy1 - top) * o
            yc = cy1 - (cy1 - top) * (o + (c - o) * k)
            up = c >= o
            col = mix(PAL["buy"] if up else PAL["sell"], PAL["card"], a)
            y0, y1 = sorted((yo, yc))
            d.rectangle((x, y0, x + step * .6, max(y1, y0 + 2 * S)), fill=col)
        # the last close: a pulsing dot (two pulses a loop)
        if level > .05 and fade > .01:
            i = min(n - 1, int(level * n))
            o, c = CANDLES[i]
            k = ease_out((level * n - i) / 1.2)
            x = cx0 + step * i + step * .5
            y = cy1 - (cy1 - top) * (o + (c - o) * k)   # on the candle's close as it rises
            r = (5 + 3 * (.5 + .5 * math.sin(2 * math.pi * 2 * t))) * S
            d.ellipse((x - r, y - r, x + r, y + r), outline=mix(PAL["accent"], PAL["card"], .6 * fade), width=S)
            d.ellipse((x - 3 * S, y - 3 * S, x + 3 * S, y + 3 * S), fill=mix(PAL["accent"], PAL["card"], fade))
        # a glint across the title with the graduation flash
        g = (u - FILL_END) / (FLASH_END - FILL_END)
        if 0 < g < 1:
            x0, y0, x1, y1 = self.title_box
            sx = x0 - 40 * S + (x1 - x0 + 80 * S) * ease_io(g)
            band = Image.new("L", img.size, 0)
            ImageDraw.Draw(band).polygon([(sx, y0 - 6 * S), (sx + 22 * S, y0 - 6 * S),
                                          (sx + 6 * S, y1 + 6 * S), (sx - 16 * S, y1 + 6 * S)], fill=200)
            text = Image.new("L", img.size, 0)
            ImageDraw.Draw(text).text(self.title_xy, self.title, font=self.f_title, fill=255)
            from PIL import ImageChops
            band = ImageChops.multiply(band.filter(ImageFilter.GaussianBlur(5 * S)), text)
            img = Image.composite(Image.new("RGB", img.size, PAL["accent"]), img, band)
        out = img.resize((W, H), Image.LANCZOS)
        img.close()
        return out


def encode(screen: Screen, frames: int, fps: int, mp4: Path | None, gif: Path | None) -> None:
    exe = find_ffmpeg()
    if not exe:
        sys.exit("ffmpeg is needed: pip install imageio-ffmpeg")
    src = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-"]
    outs = []
    if mp4:
        outs.append([exe, "-y", "-loglevel", "error", *src, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     "-crf", "20", "-preset", "slow", "-movflags", "+faststart", "-an", str(mp4)])
    if gif:
        outs.append([exe, "-y", "-loglevel", "error", *src, "-filter_complex",
                     "split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a",
                     "-loop", "0", str(gif)])
    procs = [subprocess.Popen(cmd, stdin=subprocess.PIPE) for cmd in outs]
    for i in range(frames):
        im = screen.frame(i / frames)
        raw = im.tobytes()
        im.close()
        for p in procs:
            p.stdin.write(raw)
    for p in procs:
        p.stdin.close()
        if p.wait() != 0:
            sys.exit("ffmpeg failed")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--brand", action="store_true", help="the @BotFather description picture instead of the menu")
    ap.add_argument("--only", help="one menu screen (start, chats, stats, help, terms, support, lang)")
    ap.add_argument("--still", type=float, action="append", default=[], help="save the frame at this point of the loop")
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--seconds", type=float, default=3.0)
    a = ap.parse_args()
    n = round(a.fps * a.seconds)
    if a.brand:
        out = ROOT / "assets" / "brand" / "botfather-description-testnet"
        jobs = [(out, *FRONT, True)]
    else:
        folder = ROOT / "assets" / "menu" / "en"
        folder.mkdir(parents=True, exist_ok=True)
        jobs = [(folder / key, *(FRONT if key == "start" else (title, line)), False)
                for key, (title, line) in MENU["en"].items() if not a.only or key == a.only]
    for out, title, line, brand in jobs:
        screen = Screen(title, line)
        if a.still:
            for t in a.still:
                screen.frame(t).save(out.with_name(f"{out.name}-{t:.2f}.png"))
            continue
        encode(screen, n, a.fps, out.with_suffix(".mp4"), out.with_suffix(".gif") if brand else None)
        if brand:
            screen.frame(0).save(out.with_suffix(".png"))
        sizes = ", ".join(f"{p.name} {p.stat().st_size // 1024} KB"
                          for p in (out.with_suffix(".mp4"), out.with_suffix(".gif")) if p.exists() and (brand or p.suffix == ".mp4"))
        print(sizes)


if __name__ == "__main__":
    main()
