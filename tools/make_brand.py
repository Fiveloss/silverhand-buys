"""README and profile images: banner, example post, BotFather description picture.

    python3 tools/make_brand.py

Needs Chromium or Chrome (set CHROME to its path; a full Chrome runs headless) and
an emoji font. Text is set in the bundled Instrument Sans (assets/fonts, OFL). The example post is produced by the bot's own renderer, so the
picture shows exactly what the bot sends.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buybot import render  # noqa: E402
from buybot.db import Chat  # noqa: E402

BRAND = ROOT / "assets" / "brand"
CHROME = os.environ.get("CHROME", "/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell")
FONT = "'Instrument Sans', 'Noto Color Emoji', 'Segoe UI Emoji', sans-serif"
FONT_FACE = """@font-face{font-family:'Instrument Sans';src:url('../fonts/InstrumentSans.ttf');
font-weight:400 700;font-stretch:75% 100%}"""

BG = """
background:
  radial-gradient(ellipse at 28% 50%, rgba(45,245,154,.20), transparent 55%),
  radial-gradient(ellipse at 80% 20%, rgba(45,245,154,.08), transparent 50%),
  linear-gradient(rgba(45,245,154,.05) 1px, transparent 1px) 0 0/32px 32px,
  linear-gradient(90deg, rgba(45,245,154,.05) 1px, transparent 1px) 0 0/32px 32px,
  #06080a;
"""
CHROME_TEXT = """
background: linear-gradient(180deg, #ffffff 0%, #d4dbe3 42%, #7d8896 52%, #eef2f6 72%, #9aa5b1 100%);
-webkit-background-clip: text; background-clip: text; color: transparent;
"""


def shot(html: str, out: Path, w: int, h: int) -> None:
    page = BRAND / f".{out.stem}.html"
    page.write_text(html, encoding="utf-8")
    headless = [] if "headless_shell" in CHROME else ["--headless=new"]
    subprocess.run([CHROME, *headless, "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                    "--allow-file-access-from-files", f"--screenshot={out}", f"--window-size={w},{h}",
                    page.as_uri()], check=True, capture_output=True)
    page.unlink()
    print("wrote", out.relative_to(ROOT))


def example_post() -> tuple[str, list[tuple[str, str]]]:
    chat = Chat(chat_id=0, title="", token="", min_usd=0, emoji="🟢", step_usd=150,
                media_kind=None, media_file_id=None, active=True, created_at=0)
    main = {"token": "0x" + "5" * 40, "symbol": "HANDS", "name": "Silver Hands",
            "quote_symbol": "ETH", "quote": 0.42, "usd": 1260.0, "tokens": 11_800_000,
            "buyer": "0x3f927788d627d3561ff59bbfef67ebcbf0fb10a9", "dev": False,
            "tx": "0x" + "a" * 64, "mc_quote": 16.2, "mc_usd": 48_600.0, "progress": 0.64,
            "venue": "curve"}
    more = [dict(main, quote=0.05, usd=150.0), dict(main, quote=0.12, usd=360.0),
            dict(main, quote=0.08, usd=240.0)]
    text = render.buy_text(main, chat, more, explorer="https://robinhoodchain.blockscout.com")
    text += render.footer()
    return text, render.buttons(main, "https://gmgn.ai/robinhood/token/{token}", "https://x")


def post_html(scale: float = 1.0) -> str:
    text, pairs = example_post()
    buttons = "".join(f'<div class="btn">{label}</div>' for label, _ in pairs)
    return f"""
<div class="tg" style="zoom:{scale}">
  <img class="ava" src="avatar.png">
  <div class="col">
    <div class="bubble"><div class="name">Silverhand Buys</div><div class="text">{text}</div>
      <div class="time">12:04</div></div>
    <div class="btns">{buttons}</div>
  </div>
</div>"""


POST_CSS = f"""
.tg{{display:flex;gap:12px;align-items:flex-end;font-family:{FONT}}}
.ava{{width:46px;height:46px;border-radius:50%}}
.col{{width:430px}}
.bubble{{background:#182533;border-radius:16px 16px 16px 4px;padding:10px 14px 8px;color:#e9eef3;
  font-size:16px;line-height:1.42;box-shadow:0 8px 30px rgba(0,0,0,.45)}}
.name{{color:#2df59a;font-weight:bold;font-size:15px;margin-bottom:4px}}
.text{{white-space:pre-wrap}}
.text a{{color:#6ab3f3;text-decoration:none}}
.time{{text-align:right;color:#6d7f8f;font-size:12px;margin-top:2px}}
.btns{{display:flex;gap:6px;margin-top:6px}}
.btn{{flex:1;text-align:center;background:rgba(24,37,51,.85);color:#e9eef3;border-radius:10px;
  padding:9px 0;font-size:15px}}
"""


def main() -> None:
    avatar = BRAND / "avatar.png"
    if not avatar.exists():
        raise SystemExit(f"missing {avatar}")

    shot(f"""<!doctype html><meta charset="utf-8"><style>{FONT_FACE}
body{{margin:0;width:1280px;height:640px;{BG}font-family:{FONT};color:#dfe6ec;overflow:hidden}}
.ava{{position:absolute;left:92px;top:110px;width:420px;height:420px;border-radius:50%;
  box-shadow:0 0 0 3px rgba(45,245,154,.55),0 0 70px rgba(45,245,154,.45)}}
.txt{{position:absolute;left:585px;top:150px;width:640px}}
h1{{margin:0;font-size:84px;letter-spacing:-2px;line-height:1.05;{CHROME_TEXT}}}
.tag{{font-size:31px;margin-top:22px;color:#cfd8e0;line-height:1.3}}
.slogan{{font-size:27px;margin-top:22px;color:#2df59a;font-style:italic}}
.chips{{margin-top:34px;display:flex;gap:12px;flex-wrap:wrap}}
.chip{{border:1px solid rgba(45,245,154,.45);color:#b9f7da;border-radius:999px;padding:8px 16px;font-size:20px}}
</style>
<img class="ava" src="avatar.png">
<div class="txt"><h1>Silverhand Buys</h1>
<div class="tag">Live buy alerts for Robinhood Chain token chats</div>
<div class="slogan">Not paper hands. Silver hands.</div>
<div class="chips"><span class="chip">No ads</span><span class="chip">Pons · Flap.sh</span>
<span class="chip">Uniswap v2/v3/v4</span><span class="chip">Open source</span></div></div>""",
         BRAND / "banner.png", 1280, 640)

    shot(f"""<!doctype html><meta charset="utf-8"><style>{FONT_FACE}
body{{margin:0;padding:34px 40px;{BG}background-color:#0e1621}}{POST_CSS}</style>{post_html()}""",
         BRAND / "post-example.png", 600, 440)

    shot(f"""<!doctype html><meta charset="utf-8"><style>{FONT_FACE}
body{{margin:0;width:640px;height:360px;{BG}font-family:{FONT};overflow:hidden}}
.ava{{position:absolute;left:44px;top:70px;width:220px;height:220px;border-radius:50%;
  box-shadow:0 0 0 2px rgba(45,245,154,.55),0 0 44px rgba(45,245,154,.45)}}
.txt{{position:absolute;left:300px;top:92px;width:310px}}
h1{{margin:0;font-size:40px;letter-spacing:-1px;{CHROME_TEXT}}}
p{{margin:14px 0 0;color:#cfd8e0;font-size:21px;line-height:1.35}}
.s{{color:#2df59a;font-style:italic;font-size:18px}}
</style><img class="ava" src="avatar.png"><div class="txt"><h1>Silverhand Buys</h1>
<p>Every buy of your token, live in your chat.</p><p class="s">Not paper hands. Silver hands.</p></div>""",
         BRAND / "botfather-description.png", 640, 360)


if __name__ == "__main__":
    main()
