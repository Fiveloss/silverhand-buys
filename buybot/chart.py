"""The price chart posted with each buy or sell: market-cap candles of a token's
recent trades, drawn with Pillow in the bot's colors. Trades in, PNG or MP4 out.

Each trade is a point (time, price after the trade, size in quote units, side).
A candle opens at the previous close, so the chart has no gaps between candles,
and a candle without trades is a flat dash at that close.

render() gives a still picture. animate() gives a card in the style of the bot's
intro animations, a three-second loop at 30 fps that Telegram plays like a GIF:
signal tapes run along the top and bottom (green for a buy, red for a sell), the
avatar sits in a spinning ring, a condensed title with depth catches a glint and
glitches for a moment, the amount glows, and over the chart a scan line sweeps,
packets run along the last price and the trade's candle pings. Everything still
is drawn once at 2x; each frame only adds the moving parts and streams to ffmpeg.
"""

import io
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

W, H = 1000, 520   # the still chart's size; drawn at 2x and scaled down for smooth lines
S = 2
SLOTS = 72         # candles across the chart; fewer are drawn from the right
INTERVALS = [(15, "15s"), (30, "30s"), (60, "1m"), (300, "5m"), (900, "15m"), (3600, "1h")]

BG = (10, 13, 12)
GRID = (26, 32, 29)
TEXT = (222, 230, 226)
DIM = (118, 130, 124)
UP = (45, 245, 154)
DOWN = (255, 77, 109)
BRAND = "SILVERHAND BUYS"
AVATAR = Path(__file__).resolve().parent.parent / "assets" / "brand" / "avatar.png"


def _mix(a, b, k):
    return tuple(round(x * k + y * (1 - k)) for x, y in zip(a, b))


VOL_UP, VOL_DOWN = _mix(UP, BG, 0.38), _mix(DOWN, BG, 0.38)
# per side: (main, light, deep, extrusion, tape background)
PALETTE = {1: (UP, (200, 255, 228), (6, 128, 74), (3, 44, 26), (5, 12, 9)),
           -1: (DOWN, (255, 205, 214), (150, 18, 44), (52, 6, 16), (14, 5, 7))}

_FONTS = {
    "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/segoeuib.ttf"],
    "mono": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "C:/Windows/Fonts/consola.ttf"],
    "monob": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "C:/Windows/Fonts/consolab.ttf"],
}


def _font(kind: str, size: int):
    for path in _FONTS[kind]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


@dataclass
class Candle:
    start: int
    open: float
    high: float
    low: float
    close: float
    volume: float  # quote units
    trades: int


def pick_interval(span: float) -> tuple[int, str]:
    """The shortest candle that shows the span in about SLOTS candles; a span a
    little longer loses its oldest part rather than halving the detail."""
    for seconds, label in INTERVALS:
        if span / seconds <= SLOTS * 1.2:
            return seconds, label
    return INTERVALS[-1]


def build_candles(points: list[tuple[int, float, float, int]], now: int, interval: int) -> list[Candle]:
    """Candles for the last SLOTS intervals up to now, from the first one with a trade."""
    first_slot = (now // interval - SLOTS + 1) * interval
    prev_close = None
    for ts, price, _, _ in points:  # the close right before the window
        if ts >= first_slot:
            break
        prev_close = price
    out: list[Candle] = []
    i = 0
    inside = [p for p in points if p[0] >= first_slot]
    for k in range(SLOTS):
        start = first_slot + k * interval
        end = start + interval
        chunk = []
        while i < len(inside) and inside[i][0] < end:
            chunk.append(inside[i])
            i += 1
        if not chunk:
            if prev_close is not None:
                out.append(Candle(start, prev_close, prev_close, prev_close, prev_close, 0.0, 0))
            continue
        prices = [p[1] for p in chunk]
        o = prev_close if prev_close is not None else prices[0]
        out.append(Candle(start, o, max(o, *prices), min(o, *prices), prices[-1],
                          sum(p[2] for p in chunk), len(chunk)))
        prev_close = prices[-1]
    return out


def fmt_value(x: float, usd: bool, quote_symbol: str) -> str:
    """Axis and header numbers: $28.4K, or 16.2 ETH when there is no dollar price."""
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= limit:
            s = f"{x / limit:.3g}{suffix}"
            break
    else:
        s = f"{x:.4g}"
    return f"${s}" if usd else f"{s} {quote_symbol}"


def _draw(points, *, symbol: str, supply: float, usd_per_quote: float | None, quote_symbol: str,
          side: int = 0, now: int | None = None, w: int = W, h: int = H,
          header: bool = True) -> tuple[Image.Image, dict] | None:
    """The chart at 2x as RGB and a few facts about it (the last candle's place, the
    market cap and its change). Without the header it is a panel for the card."""
    now = int(now if now is not None else time.time())
    points = sorted(p for p in points if p[1] > 0 and p[0] <= now)
    if len(points) < 2:
        return None
    interval, label = pick_interval(now - points[0][0])
    scale = supply * (usd_per_quote or 1.0)  # price -> market cap
    usd = usd_per_quote is not None
    candles = build_candles(points, now, interval)
    if len(candles) < 2:
        return None

    img = Image.new("RGBA", (w * S, h * S), (*BG, 255))
    d = ImageDraw.Draw(img, "RGBA")
    left, right = 18 * S, (w - 96) * S
    top, bottom = (70 if header else 14) * S, (h - 34) * S
    vol_h = int((bottom - top) * 0.18)
    price_bottom = bottom - vol_h - 8 * S

    if not header:  # the card's dot grid runs on under the chart
        dot = _mix(UP, BG, 0.16)
        for x in range(28 * S, w * S, 28 * S):
            for yy in range(14 * S, h * S, 28 * S):
                d.rectangle([x - S // 2, yy - S // 2, x + S // 2, yy + S // 2], fill=dot)

    # watermark, like the big faint name behind trading-site charts (its own layer:
    # Pillow does not blend translucent text into an image)
    wm = _font("bold", (64 if header else 52) * S)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    tw = ld.textlength(BRAND, font=wm)
    ld.text(((left + right - tw) / 2, (top + price_bottom) / 2 - 36 * S), BRAND, font=wm,
            fill=(*UP, 16))
    img = Image.alpha_composite(img, layer)
    d = ImageDraw.Draw(img, "RGBA")

    lo = min(c.low for c in candles) * scale
    hi = max(c.high for c in candles) * scale
    if hi <= lo:
        hi, lo = hi * 1.01 + 1e-12, lo * 0.99
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad

    def y(v: float) -> float:
        return price_bottom - (v * scale - lo) / (hi - lo) * (price_bottom - top)

    # grid and the market-cap axis
    mono = _font("mono", 13 * S)
    for k in range(5):
        v = lo + (hi - lo) * (k + 0.5) / 5
        yy = price_bottom - (v - lo) / (hi - lo) * (price_bottom - top)
        d.line([(left, yy), (right, yy)], fill=GRID, width=S)
        d.text((right + 10 * S, yy - 8 * S), fmt_value(v, usd, quote_symbol), font=mono, fill=DIM)

    slot = (right - left) / SLOTS
    body = max(2 * S, slot * 0.64)
    x0 = right - len(candles) * slot
    vmax = max(c.volume for c in candles) or 1.0
    for n, c in enumerate(candles):
        cx = x0 + (n + 0.5) * slot
        up = c.close >= c.open
        col = UP if up else DOWN
        if c.trades:
            vh = c.volume / vmax * vol_h
            d.rectangle([cx - body / 2, bottom - vh, cx + body / 2, bottom], fill=VOL_UP if up else VOL_DOWN)
            d.line([(cx, y(c.high)), (cx, y(c.low))], fill=col, width=S)
            y1, y2 = sorted((y(c.open), y(c.close)))
            d.rectangle([cx - body / 2, y1, cx + body / 2, max(y2, y1 + S)], fill=col)
        else:
            d.line([(cx - body / 2, y(c.close)), (cx + body / 2, y(c.close))], fill=_mix(DIM, BG, 0.6), width=S)

    # time labels under the chart, every quarter, clear of the axis column
    for n in range(0, len(candles), max(1, len(candles) // 4)):
        cx = x0 + (n + 0.5) * slot
        if cx + 22 * S > right:
            continue
        stamp = time.strftime("%H:%M", time.gmtime(candles[n].start))
        d.text((cx - 18 * S, bottom + 8 * S), stamp, font=mono, fill=DIM)
    d.text((right + 10 * S, bottom + 8 * S), "UTC", font=mono, fill=DIM)

    # the last price: a dotted line and a tag on the axis
    last = candles[-1]
    tag_col = UP if last.close >= last.open else DOWN
    ly = y(last.close)
    for xx in range(int(left), int(right), 10 * S):
        d.line([(xx, ly), (xx + 5 * S, ly)], fill=_mix(tag_col, BG, 0.7), width=S)
    tag = fmt_value(last.close * scale, usd, quote_symbol)
    tag_w = d.textlength(tag, font=mono) + 12 * S
    d.rounded_rectangle([right + 4 * S, ly - 11 * S, right + 4 * S + tag_w, ly + 11 * S],
                        radius=4 * S, fill=tag_col)
    d.text((right + 10 * S, ly - 8 * S), tag, font=mono, fill=BG)

    # the trade being posted: an arrow at the last candle
    last_cx = x0 + (len(candles) - 0.5) * slot
    if side:
        col = UP if side > 0 else DOWN
        if side > 0:
            tip = y(last.low) + 8 * S
            d.polygon([(last_cx, tip), (last_cx - 9 * S, tip + 14 * S), (last_cx + 9 * S, tip + 14 * S)],
                      fill=col)
        else:
            tip = y(last.high) - 8 * S
            d.polygon([(last_cx, tip), (last_cx - 9 * S, tip - 14 * S), (last_cx + 9 * S, tip - 14 * S)],
                      fill=col)

    base_open = candles[0].open
    change = (last.close / base_open - 1) * 100 if base_open > 0 else None
    span = len(candles) * interval
    span_label = f"{span // 3600}h" if span >= 3600 else f"{math.ceil(span / 60)}m"
    mc = fmt_value(last.close * scale, usd, quote_symbol)

    if header:  # symbol and interval, market cap, change over the chart
        bold = _font("bold", 22 * S)
        head = f"{symbol} · {label}"
        d.text((left, 18 * S), head, font=bold, fill=TEXT)
        hx = left + d.textlength(head, font=bold) + 18 * S
        d.text((hx, 18 * S), "MC " + mc, font=bold, fill=UP if (change or 0) >= 0 else DOWN)
        hx += d.textlength("MC " + mc, font=bold) + 14 * S
        if change is not None:
            d.text((hx, 23 * S), f"{'▲' if change >= 0 else '▼'} {abs(change):.1f}% · {span_label}",
                   font=_font("mono", 15 * S), fill=UP if change >= 0 else DOWN)
        small = _font("bold", 13 * S)
        brand_w = d.textlength(BRAND, font=small)
        d.text((w * S - 18 * S - brand_w, 24 * S), BRAND, font=small, fill=UP)
        d.line([(left, 56 * S), (w * S - 18 * S, 56 * S)], fill=GRID, width=S)

    facts = {"left": left, "right": right, "top": top, "bottom": bottom, "price_y": ly,
             "cx": last_cx, "high": y(last.high), "low": y(last.low),
             "mc": mc, "change": change, "span": span_label, "interval": label}
    return img.convert("RGB"), facts


def render(points: list[tuple[int, float, float, int]], *, symbol: str, supply: float,
           usd_per_quote: float | None, quote_symbol: str, side: int = 0,
           now: int | None = None) -> bytes | None:
    """PNG bytes of the chart, or None when there is too little to draw.
    `side` marks the trade being posted: 1 buy, -1 sell, 0 none."""
    drawn = _draw(points, symbol=symbol, supply=supply, usd_per_quote=usd_per_quote,
                  quote_symbol=quote_symbol, side=side, now=now)
    if drawn is None:
        return None
    out = io.BytesIO()
    drawn[0].resize((W, H), Image.LANCZOS).save(out, "PNG", optimize=False)
    return out.getvalue()


# ----------------------------------------------------------------------------- the animated card

ANIM_W, ANIM_H = 800, 450
FPS, SECONDS = 30, 3.0
TAPE = 10          # height of the signal tapes
PANEL_Y = 108      # where the chart starts


def find_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _condensed(text: str, height: int, squeeze: float = 0.72) -> Image.Image:
    """A mask of `text` in a tall, narrow display face: the bold sans drawn big and
    pressed sideways, like the condensed titles of the intro animations."""
    font = _font("bold", height * 2)
    x0, y0, x1, y1 = font.getbbox(text)
    mask = Image.new("L", (x1 - x0 + 4, y1 - y0 + 4), 0)
    ImageDraw.Draw(mask).text((2 - x0, 2 - y0), text, font=font, fill=255)
    return mask.resize((max(1, int(mask.width * squeeze / 2)), height), Image.LANCZOS)


def _spaced(draw, xy, text, font, fill, tracking) -> float:
    """Letter-spaced text (Pillow has no tracking); returns where it ends."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x


def _vgradient(w: int, h: int, stops) -> Image.Image:
    col = Image.new("RGB", (1, h))
    px = col.load()
    for yy in range(h):
        f = yy / max(1, h - 1)
        for (f0, c0), (f1, c1) in zip(stops, stops[1:]):
            if f0 <= f <= f1:
                px[0, yy] = _mix(c1, c0, (f - f0) / max(1e-6, f1 - f0))
                break
    return col.resize((w, h))


def _title_layer(text: str, height: int, pal) -> tuple[Image.Image, Image.Image]:
    """The title's RGB and mask with gradient, a line texture and depth, like the intro."""
    main, light, deep, extrude, _ = pal
    mask = _condensed(text, height)
    depth = max(2, height // 11)
    wd, ht = mask.width, mask.height + depth
    rgb = Image.new("RGB", (wd, ht), (0, 0, 0))
    full = Image.new("L", (wd, ht), 0)
    for k in range(1, depth + 1):  # the extrusion under the letters
        rgb.paste(extrude if k < depth else (0, 0, 0), (0, k), mask)
        full.paste(255, (0, k), mask)
    fill = _vgradient(wd, mask.height, [(0, light), (0.5, main), (1, deep)])
    lines = Image.new("L", fill.size, 0)
    for yy in range(0, fill.height, 4):
        lines.paste(46, (0, yy, fill.width, yy + 1))
    fill = Image.composite(Image.new("RGB", fill.size, (0, 0, 0)), fill, lines)
    rgb.paste(fill, (0, 0), mask)
    full.paste(255, (0, 0), mask)
    return rgb, full


def _tape(width: int, phase: float, pal) -> Image.Image:
    """Signal tape; phase 0..1 moves the stripes by exactly one period."""
    main, *_, dark = pal
    img = Image.new("RGB", (width, TAPE), dark)
    d = ImageDraw.Draw(img)
    p = TAPE * 2
    off = phase * p
    for x0 in range(-3 * p, width + 3 * p, p):
        x = x0 + off
        d.polygon([(x, 0), (x + p / 2, 0), (x + p / 2 - TAPE, TAPE), (x - TAPE, TAPE)], fill=main)
    return img


def animate(points: list[tuple[int, float, float, int]], *, symbol: str, supply: float,
            usd_per_quote: float | None, quote_symbol: str, side: int = 0, amount: str = "",
            title: str | None = None, detail: str = "", now: int | None = None) -> bytes | None:
    """MP4 bytes of the animated card (no sound, so Telegram loops it like a GIF), or
    None when there is too little to draw or no ffmpeg. `title` is the big line
    (TST BUY!), `amount` the big number on the right (+$8.02) and `detail` the small
    one under it (0.003 ETH)."""
    exe = find_ffmpeg()
    if not exe:
        return None
    pal = PALETTE[1 if side >= 0 else -1]
    main, light, _, _, _ = pal
    panel_h = ANIM_H - PANEL_Y - TAPE - 2
    drawn = _draw(points, symbol=symbol, supply=supply, usd_per_quote=usd_per_quote,
                  quote_symbol=quote_symbol, side=side, now=now, w=ANIM_W - 12, h=panel_h, header=False)
    if drawn is None:
        return None
    panel, facts = drawn

    # ---- the still card at 2x
    card = Image.new("RGB", (ANIM_W * S, ANIM_H * S), BG)
    glow = Image.new("L", (ANIM_W // 8, ANIM_H // 8), 0)
    ImageDraw.Draw(glow).ellipse((-12, -10, 40, 20), fill=120)
    glow = glow.filter(ImageFilter.GaussianBlur(7)).resize(card.size, Image.BILINEAR)
    card = Image.composite(Image.new("RGB", card.size, main), card, glow.point(lambda v: v * .28))
    d = ImageDraw.Draw(card)
    dot = _mix(UP, BG, 0.16)
    for x in range(28 * S, ANIM_W * S, 28 * S):
        for yy in range((TAPE + 14) * S, PANEL_Y * S, 28 * S):
            d.rectangle([x - 1, yy - 1, x + 1, yy + 1], fill=dot)
    card.paste(panel, (6 * S, PANEL_Y * S))
    d.line([(18 * S, (PANEL_Y - 3) * S), ((ANIM_W - 18) * S, (PANEL_Y - 3) * S)], fill=GRID, width=S)
    base = card.resize((ANIM_W, ANIM_H), Image.LANCZOS)

    # ---- the header at 1x: avatar, eyebrow, title, market-cap line, amount
    d = ImageDraw.Draw(base)
    av_size, av_x, av_y = 62, 22, TAPE + 22
    logo = Image.open(AVATAR).convert("RGB").resize((av_size, av_size), Image.LANCZOS)
    logo_mask = Image.new("L", (av_size * 4, av_size * 4), 0)
    ImageDraw.Draw(logo_mask).ellipse((0, 0, av_size * 4 - 1, av_size * 4 - 1), fill=255)
    logo_mask = logo_mask.resize((av_size, av_size), Image.LANCZOS)
    tx = av_x + av_size + 22
    eyebrow = _font("monob", 11)
    _spaced(d, (tx, TAPE + 14), "// SILVERHAND BUYS", eyebrow, main, 2.6)
    title_rgb, title_mask = _title_layer(title or f"{symbol} {'BUY!' if side >= 0 else 'SELL!'}", 38, pal)
    amount_rgb, amount_mask = _title_layer(amount, 40, pal) if amount else (None, None)
    room = ANIM_W - tx - 24 - (amount_mask.width + 20 if amount_mask else 0)
    if title_mask.width > room:  # a long symbol: narrower, never over the amount

        title_rgb = title_rgb.resize((room, title_rgb.height))
        title_mask = title_mask.resize((room, title_mask.height))
    title_xy = (tx, TAPE + 30)
    base.paste(title_rgb, title_xy, title_mask)
    sub_font = _font("mono", 12)
    change = facts["change"]
    sub = f"MC {facts['mc']}"
    if change is not None:
        sub += f"  {'▲' if change >= 0 else '▼'} {abs(change):.1f}%  · {facts['span'].upper()}"
    sub_end = _spaced(d, (tx, TAPE + 80), sub.upper(), sub_font, TEXT, 1.4)
    if amount_mask:
        ax = ANIM_W - 24 - amount_mask.width
        amount_xy = (ax, TAPE + 24)
        pad = 18  # room for the blur, so the glow fades out instead of ending in a box
        amount_glow = Image.new("L", (amount_mask.width + 2 * pad, amount_mask.height + 2 * pad), 0)
        amount_glow.paste(amount_mask, (pad, pad))
        amount_glow = amount_glow.filter(ImageFilter.GaussianBlur(9))
        glow_xy = (amount_xy[0] - pad, amount_xy[1] - pad)
        base.paste(amount_rgb, amount_xy, amount_mask)
        if detail:
            df = _font("monob", 12)
            dw = sum(d.textlength(ch, font=df) + 1.4 for ch in detail) - 1.4
            _spaced(d, (ANIM_W - 24 - dw, TAPE + 80), detail, df, DIM, 1.4)

    # scanlines over the still card, a dark line every three pixels
    scan = Image.new("L", base.size, 0)
    sd = ImageDraw.Draw(scan)
    for yy in range(0, ANIM_H, 3):
        sd.line([(0, yy), (ANIM_W, yy)], fill=34)
    base = Image.composite(Image.new("RGB", base.size, (0, 0, 0)), base, scan)

    # ---- what moves, prepared once
    stripes = [_tape(ANIM_W, k / 8, pal) for k in range(8)]  # a period of the tape in 8 phases
    halo = Image.new("L", (av_size + 60, av_size + 60), 0)
    ImageDraw.Draw(halo).ellipse((24, 24, av_size + 36, av_size + 36), fill=255)
    halo = halo.filter(ImageFilter.GaussianBlur(12))
    halo_color = Image.new("RGB", halo.size, main)
    px0, py0 = 6, PANEL_Y
    left, right = px0 + facts["left"] / S, px0 + facts["right"] / S
    ptop, pbot = py0 + facts["top"] / S, py0 + facts["bottom"] / S
    cx, price_y = px0 + facts["cx"] / S, py0 + facts["price_y"] / S
    band_w = 80
    band_mask = Image.new("L", (band_w, int(pbot - ptop)))
    for x in range(band_w):
        band_mask.paste(int(40 * (x / band_w) ** 2), (x, 0, x + 1, band_mask.height))
    band_mask.paste(120, (band_w - 2, 0, band_w, band_mask.height))
    band_color = Image.new("RGB", band_mask.size, main)
    glint_font_mask = title_mask

    n = int(FPS * SECONDS)
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        proc = subprocess.Popen(
            [exe, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{ANIM_W}x{ANIM_H}", "-r", str(FPS), "-i", "-", "-an", "-c:v", "libx264",
             "-preset", "superfast", "-tune", "animation", "-crf", "23", "-pix_fmt", "yuv420p",
             "-threads", "1", "-movflags", "+faststart", path],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        for i in range(n):
            t = i / n
            frame = base.copy()
            d = ImageDraw.Draw(frame)

            # the tapes run
            tape = stripes[int(t * 8 * 3) % 8]
            frame.paste(tape, (0, 0))
            frame.paste(tape, (0, ANIM_H - TAPE))

            # the avatar: a breathing glow and a spinning arc
            breathe = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(math.tau * t))
            frame.paste(halo_color, (av_x - 30, av_y - 30), halo.point(lambda v: int(v * 0.7 * breathe)))
            acx, acy, rr = av_x + av_size / 2, av_y + av_size / 2, av_size / 2 + 6
            for k in range(36):
                a0 = 360 * t + k * (150 / 36)
                d.arc([acx - rr, acy - rr, acx + rr, acy + rr], a0, a0 + 150 / 36 + 1,
                      fill=_mix(main, BG, k / 36), width=3)
            d.ellipse([av_x - 2, av_y - 2, av_x + av_size + 2, av_y + av_size + 2], fill=(0, 0, 0))
            frame.paste(logo, (av_x, av_y), logo_mask)

            # the title: a glint once a loop, a short glitch
            gw = glint_font_mask.width
            band = Image.new("L", glint_font_mask.size, 0)
            gx = -60 + (gw + 120) * t
            ImageDraw.Draw(band).polygon([(gx, 0), (gx + 12, 0), (gx - 10, band.height), (gx - 22, band.height)],
                                         fill=120)
            band = ImageChops.multiply(band.filter(ImageFilter.GaussianBlur(4)), glint_font_mask)
            frame.paste(Image.new("RGB", band.size, light), title_xy, band)
            if 0.70 <= t < 0.735 or 0.76 <= t < 0.78:
                for s_i in range(2):
                    gy = title_xy[1] + 4 + ((i * 7 + s_i * 13) % max(1, glint_font_mask.height - 12))
                    dx = (4 + (i + s_i * 3) % 5) * (1 if (i + s_i) % 2 else -1)
                    strip = frame.crop((title_xy[0] - 10, gy, title_xy[0] + gw + 10, gy + 6))
                    frame.paste(strip, (title_xy[0] - 10 + dx, gy))

            # the amount glows
            if amount_mask:
                pulse = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(math.tau * t * 2))
                frame.paste(Image.new("RGB", amount_glow.size, main), glow_xy,
                            amount_glow.point(lambda v: int(v * 0.55 * pulse)))
                frame.paste(amount_rgb, amount_xy, amount_mask)

            # the cursor after the market-cap line, LIVE
            if (t * 4) % 1 < 0.5:
                d.rectangle([sub_end + 4, TAPE + 81, sub_end + 11, TAPE + 94], fill=main)
            on = (t * 3) % 1 < 0.6
            lx = ANIM_W - 70
            d.ellipse([lx, TAPE + 15, lx + 8, TAPE + 23], fill=DOWN if on else _mix(DOWN, BG, 0.3))
            d.text((lx + 13, TAPE + 12), "LIVE", font=eyebrow, fill=TEXT if on else DIM)

            # over the chart: the scan line, packets along the last price, the ping
            sx = int(left - band_w + (right - left + band_w) * t)
            crop = (max(0, int(left) - sx), 0, band_w - max(0, sx + band_w - int(right)), band_mask.height)
            if crop[2] > crop[0]:
                frame.paste(band_color.crop(crop), (max(sx, int(left)), int(ptop)), band_mask.crop(crop))
            for k in range(3):
                head = left + ((t + k / 3) % 1.0) * (cx - left)
                for tail in range(8):
                    x = head - tail * 5
                    if x < left:
                        break
                    c = _mix(main, BG, (1 - tail / 8) * 0.9)
                    r = 3 if tail == 0 else 2
                    d.rectangle([x - r, price_y - r, x + r, price_y + r], fill=c)
            for k in range(2):
                ping = (t * 2 + k / 2) % 1.0
                r = 6 + ping * 26
                c = _mix(main, BG, (1 - ping) * 0.9)
                d.ellipse([cx - r, price_y - r, cx + r, price_y + r], outline=c, width=2)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        err = proc.stderr.read()
        if proc.wait(timeout=90) != 0:
            raise RuntimeError(err.decode(errors="replace")[:300])
        return Path(path).read_bytes()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
