"""Social images in the style of the intro animations: the X (Twitter) header,
the GitHub profile banner and the repository's social preview.

    python tools/make_social.py        -> assets/brand/social/*.png (and the banner GIF)

Each is a scene from tools/make_intro.py laid out at half size and drawn at twice
the resolution, so the captions stay readable on a wide image. X covers the
header's lower left with the avatar, so the header's content sits right of it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_intro  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "brand" / "social"
LOGO = ROOT / "assets" / "brand" / "avatar.png"

# name -> (layout width, layout height, final width, final height, title, eyebrow, line, pad_left)
IMAGES = {
    "x-header": (750, 250, 1500, 500, "SILVERHAND BUYS", "// TELEGRAM BUY BOT",
                 "EVERY BUY · LIVE IN YOUR CHAT · @SILVERHANDBUYSBOT", 170),
    "github-banner": (640, 200, 1280, 400, "SILVERHAND BUYS", "// OPEN SOURCE · AGPL-3.0",
                      "LIVE BUY ALERTS FOR TOKEN CHATS", 0),
    "repo-preview": (640, 320, 1280, 640, "SILVERHAND BUYS", "// TELEGRAM BUY BOT · OPEN SOURCE",
                     "EVERY BUY · LIVE IN YOUR CHAT", 0),
}


def scene(name: str) -> "make_intro.Scene":
    w, h, fw, fh, title, eyebrow, line, pad = IMAGES[name]
    sc = make_intro.Scene(w, h, 2 * fw // w, title, eyebrow, line, str(LOGO), pad_left=pad)
    sc.W, sc.H = fw, fh  # drawn at the layout's size times S, delivered at the final size
    return sc


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name in IMAGES:
        path = OUT / f"{name}.png"
        scene(name).frame(0.12).save(path, optimize=True)
        print("wrote", path.relative_to(ROOT))
    # the profile banner moves too: a short loop for the GitHub README
    sc = scene("github-banner")
    sc.W, sc.H = 960, 300
    frames = [sc.frame(i / 45) for i in range(45)]
    gif = OUT / "github-banner.gif"
    make_intro.encode(frames, sc.W, sc.H, 15, gif, None)
    print("wrote", gif.relative_to(ROOT), f"{gif.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
