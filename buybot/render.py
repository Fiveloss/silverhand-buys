"""Message text for posts. Pure functions: no Telegram, no network, easy to test.

All user-controlled strings (token names, ad text) are HTML-escaped: posts are
sent with parse_mode=HTML.
"""

import math
from html import escape

from .db import Ad, Chat
from .i18n import plural, t

SELL_EMOJI = "🔴"


def fmt_num(x: float) -> str:
    """Human number: 1,234 / 12.35 / 0.004567 (4 significant digits under 1)."""
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.2f}".rstrip("0").rstrip(".")
    if x == 0:
        return "0"
    digits = max(2, 3 - int(math.floor(math.log10(abs(x)))))
    return f"{x:.{digits}f}".rstrip("0").rstrip(".")


def fmt_compact(x: float) -> str:
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= limit:
            return f"{x / limit:.2f}".rstrip("0").rstrip(".") + suffix
    return fmt_num(x)


def fmt_usd(x: float) -> str:
    if x >= 100_000:
        return "$" + fmt_compact(x)
    if x >= 100:
        return f"${x:,.0f}"
    return f"${x:,.2f}"


def short_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def progress_bar(p: float, cells: int = 10) -> str:
    full = min(cells, max(0, round(p * cells)))
    return "▰" * full + "▱" * (cells - full)


def footer(pro: bool, ad: Ad | None, bot_username: str, variant: int = 0, lang: str = "en") -> str:
    if pro:
        return "\n\n<i>NFA · DYOR</i>"
    if ad:
        line = f'{t(lang, "footer_ad")} <a href="{escape(ad.url, quote=True)}">{escape(ad.text)}</a>'
    elif variant % 2:
        line = f'📣 <a href="https://t.me/{bot_username}?start=ads">{t(lang, "footer_advertise")}</a>'
    else:
        line = f'⚡ <a href="https://t.me/{bot_username}?startgroup=true">{t(lang, "footer_free")}</a>'
    return f"\n\n{line}\n<i>via @{bot_username} · NFA · DYOR</i>"


def _amount(value: float, symbol: str, usd: float | None) -> str:
    s = f"<b>{fmt_num(value)} {escape(symbol)}</b>"
    return s + (f" ({fmt_usd(usd)})" if usd is not None else "")


def buy_text(p: dict, chat: Chat, more: list[dict] | None = None, *, explorer: str = "",
             test: bool = False, sell: bool = False) -> str:
    """A buy post, or with sell=True a sell post: the same layout with red marks
    and the quote received instead of spent. p["buyer"] is the seller for a sell.

        🟢 TST Buy!
        ┃ 💵 Spent $1,050 · 0.35 ETH
        ┃ 🪙 Got 2.1M TST
        ┃ 👤 Buyer 0x3f92…10a9
        ┃ 📊 MC $78,000
        ┃ 🎓 Curve ▰▰▰▰▱▱▱▱▱▱ 43%
    """
    lang = getattr(chat, "lang", "en")
    sym = escape(p["symbol"])
    icon = SELL_EMOJI if sell else chat.emoji
    lines = []
    if test:
        lines.append(t(lang, "post_test"))
    title = f"{icon} <b>{sym} {t(lang, 'post_sell' if sell else 'post_buy')}</b>"
    if p.get("dev"):
        title += " · 👨‍💻 <b>DEV</b>"
    whale = getattr(chat, "whale_usd", 0)
    if not sell and whale and (p.get("usd") or 0) >= whale:
        title += f" · 🐋 <b>{t(lang, 'post_whale')}</b>"
    lines.append(title)  # one mark says buy or sell; the size is in the numbers below

    body = []
    tokens = f"<b>{fmt_compact(p['tokens'])}</b> {sym}"
    if sell:
        body += [f"{t(lang, 'post_sold')} {tokens}",
                 f"{t(lang, 'post_received')} " + _money(p["quote"], p["quote_symbol"], p.get("usd"))]
    else:
        body += [f"{t(lang, 'post_spent')} " + _money(p["quote"], p["quote_symbol"], p.get("usd")),
                 f"{t(lang, 'post_got')} {tokens}"]
    if p.get("buyer"):
        who = short_addr(p["buyer"])
        if explorer:
            who = f'<a href="{explorer}/address/{p["buyer"]}">{who}</a>'
        body.append(f"{t(lang, 'post_seller' if sell else 'post_buyer')} {who}")
        note = holder_line(p.get("holder"), lang)
        if note:
            body.append(note)
    if p.get("mc_usd") is not None:
        body.append(f"📊 MC <b>{fmt_usd(p['mc_usd'])}</b>")
    elif p.get("mc_quote") is not None:
        body.append(f"📊 MC <b>{fmt_compact(p['mc_quote'])} {escape(p['quote_symbol'])}</b>")
    if p.get("venue") == "v4":
        body.append("🦄 Uniswap v4")
    elif p.get("progress") is not None:
        body.append(f"{t(lang, 'post_curve')} {progress_bar(p['progress'])} {p['progress'] * 100:.0f}%")
    if more:
        total = sum(m["quote"] for m in more)
        usds = [m.get("usd") for m in more]
        usd = sum(usds) if all(u is not None for u in usds) else None
        word = (plural(len(more), lang, ("sell", "sells"), ("продажа", "продажи", "продаж")) if sell else
                plural(len(more), lang, ("buy", "buys"), ("покупка", "покупки", "покупок")))
        body.append(t(lang, "post_more", n=len(more), word=word) + " "
                    + _money(total, p["quote_symbol"], usd))
    lines.append("<blockquote>" + "\n".join(body) + "</blockquote>")
    return "\n".join(lines)


def holder_line(h: dict | None, lang: str) -> str | None:
    """🆕 New holder · 💼 Position +35% · holds 0.12% · 🚪 Sold the whole bag."""
    if not h:
        return None
    if h.get("new"):
        return t(lang, "post_new_holder")
    if h.get("all"):
        return t(lang, "post_sold_all")
    change = h.get("change")
    if change is None:
        return None
    share = h.get("share", 0)
    held = "<0.01%" if share < 0.0001 else f"{share * 100:.2f}%"
    pct = f"{'+' if change >= 0 else '−'}{min(abs(change) * 100, 9999):.0f}%"
    return t(lang, "post_position", change=pct, share=held)


def _money(value: float, symbol: str, usd: float | None) -> str:
    """Dollars first when known: <b>$1,050</b> · 0.35 ETH; else <b>0.35 XYZ</b>."""
    amount = f"{fmt_num(value)} {escape(symbol)}"
    return f"<b>{fmt_usd(usd)}</b> · {amount}" if usd is not None else f"<b>{amount}</b>"


def milestone_text(p: dict, lang: str = "en") -> str:
    """🚀 TST hit $50K market cap! / 🎓 TST: the bonding curve is 75% full."""
    sym = escape(p["symbol"])
    if p["what"] == "mc":
        return t(lang, "post_level_mc", symbol=sym, level=fmt_usd(p["level"]).replace(",000", "K")
                 if p["level"] < 1_000_000 else fmt_usd(p["level"]))
    pct = round(p["level"] * 100)
    return t(lang, "post_level_curve", symbol=sym, pct=pct, bar=progress_bar(p["level"]))


def graduation_text(p: dict, lang: str = "en") -> str:
    raised = _amount(p["raised"], p["quote_symbol"], p.get("usd"))
    return t(lang, "post_grad", symbol=escape(p["symbol"]), raised=raised)


def buttons(p: dict, trade_url: str, explorer: str) -> list[tuple[str, str]]:
    out = []
    if trade_url:
        out.append(("📈 Chart", trade_url.format(token=p["token"])))
    if explorer and p.get("tx"):
        out.append(("🔍 Tx", f"{explorer}/tx/{p['tx']}"))
    if p.get("token"):
        out.append(("📊 24h", f"s:{p['token']}"))  # a pop-up for whoever taps it, not a message
    return out


def pick_main(items: list[dict]) -> tuple[dict, list[dict]]:
    """The biggest buy is posted in full; the rest are summed in one line."""
    def size(p: dict) -> float:
        return p["usd"] if p.get("usd") is not None else p["quote"]
    main = max(items, key=size)
    rest = [p for p in items if p is not main]
    return main, rest


def sample_payload(token_symbol: str, quote_symbol: str, eth_usd: float | None) -> dict:
    usd = 0.35 * eth_usd if eth_usd else None
    return {"token": "0x" + "0" * 40, "symbol": token_symbol, "name": token_symbol,
            "quote_symbol": quote_symbol, "quote": 0.35, "usd": usd, "tokens": 2_100_000,
            "buyer": "0x3f927788d627d3561ff59bbfef67ebcbf0fb10a9", "dev": False, "tx": None,
            "mc_quote": 26.0, "mc_usd": 26.0 * eth_usd if eth_usd else None, "progress": 0.43,
            "venue": "curve"}
