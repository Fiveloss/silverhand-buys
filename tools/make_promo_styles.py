"""The /promo style picker's samples: a short clip of each style, and one picture of them all.

    python tools/make_promo_styles.py   -> assets/promo-styles/<style>.mp4 (the album the picker
                                           sends, one clip a style) and assets/brand/promo-styles.jpg
                                           (all styles on one picture, the fallback)

Drawn from sample facts on the owner's demo token, $SHBUYS (its logo is the bot's avatar); the
picker's text says they are samples. Run it again after a style changes.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from buybot import promo, promo_styles, promo_video  # noqa: E402
from buybot.card import AVATAR, font  # noqa: E402

OUT = ROOT / "assets" / "brand" / "promo-styles.jpg"
CLIPS = ROOT / "assets" / "promo-styles"
CLIP_SIZE = (432, 540)   # 4:5, small: six of them go out in one album
CLIP_FPS = 24
SAMPLE = {"token": "0x7ca1e72987d3c1bf347e8f13398c208a528ebed5", "symbol": "SHBUYS", "name": "Silverhand Buys",
          "pair": "AAPL", "graduated": False, "progress": 0.439, "dex": "Uniswap v4", "target": 3.42,
          "raised": 1.5, "mc_quote": 2.76, "mc_usd": 5930.0, "mc_approx": True, "holders": 5, "buys_24h": 7,
          "launchpad": "vibe/vibe", "network": "Robinhood Chain testnet", "bot": "@silverhandbuysTNbot",
          "testnet": True}
TILE = (360, 450)
LABEL = 64
COLS = 3


def main():
    with Image.open(AVATAR) as im:
        logo = im.convert("RGBA").resize((410, 410), Image.LANCZOS)
    names = list(promo.STYLES)
    CLIPS.mkdir(parents=True, exist_ok=True)
    for name in names:
        data = promo_video.render(SAMPLE, logo, size=CLIP_SIZE, fps=CLIP_FPS, style=name, crf=26)
        path = CLIPS / f"{name}.mp4"
        path.write_bytes(data)
        print(path, len(data) // 1024, "KB")
    rows = (len(names) + COLS - 1) // COLS
    sheet = Image.new("RGB", (TILE[0] * COLS, (TILE[1] + LABEL) * rows), (5, 6, 7))
    d = ImageDraw.Draw(sheet)
    for i, name in enumerate(names):
        v = promo_video.video(SAMPLE, logo, name)
        frame = v.frame(getattr(v, "preview_at", 4.6)).resize(TILE, Image.LANCZOS)
        x, y = (i % COLS) * TILE[0], (i // COLS) * (TILE[1] + LABEL)
        sheet.paste(frame, (x, y))
        label = f"{i + 1}  {promo_styles.NAMES[name]}"
        d.text((x + 18, y + TILE[1] + 42), label, font=font("title", 30), fill=(238, 243, 241), anchor="ls")
        d.text((x + TILE[0] - 18, y + TILE[1] + 42), "sample", font=font("mono", 18), fill=(110, 120, 116),
               anchor="rs")
        d.line([(x, y), (x, y + TILE[1] + LABEL)], fill=(5, 6, 7), width=3)
    sheet.save(OUT, "JPEG", quality=86, optimize=True)
    print(OUT, OUT.stat().st_size // 1024, "KB", sheet.size)


if __name__ == "__main__":
    main()
