"""Message text for posts. Pure functions: no Telegram, no network, easy to test.

All user-controlled strings (token names, symbols) are HTML-escaped: posts are
sent with parse_mode=HTML.
"""

import math
from html import escape

from .db import Chat
from .i18n import plural, t
from .scan import BLOCKS_PER_SECOND, MAX_TRANSFERS, SNIPER_BLOCKS

SELL_EMOJI = "🔴"
DEV_SELL_EMOJI = "🚨"


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


def footer() -> str:
    """The end of every post, the same in every chat: no ad, no promotion."""
    return "\n\n<i>NFA · DYOR</i>"


def _amount(value: float, symbol: str, usd: float | None) -> str:
    s = f"<b>{fmt_num(value)} {escape(symbol)}</b>"
    return s + (f" ({fmt_usd(usd)})" if usd is not None else "")


def buy_text(p: dict, chat: Chat, more: list[dict] | None = None, *, explorer: str = "",
             test: bool = False, sell: bool = False) -> str:
    """The caption of a buy post, or with sell=True of a sell: a short summary under the
    card, which carries the chart and the numbers in full. p["buyer"] is the seller for a sell.

        🟢 BUY · $TST · 🐋 WHALE

        💵 $1,050 · 0.35 ETH
        ├ 🪙 Got 2.1M TST
        ├ 👤 0x3f92…10a9 · 🆕 New holder
        ├ 📊 MC $78.0K
        └ 🎓 Curve ▰▰▰▰▱▱▱▱▱▱ 43%

        CA <code>0x…</code>

    A sale by the token's creator (p["dev_sell"]) is a 🚨 DEV SELL, and its wallet line
    says how much of the creator's bag went (from their balance after it).
    """
    lang = getattr(chat, "lang", "en")
    sym = escape(p["symbol"])
    dev_sell = sell and p.get("dev_sell")
    lines = []
    if test:
        lines += [t(lang, "post_test"), ""]
    if dev_sell:
        title = f"{DEV_SELL_EMOJI} <b>{t(lang, 'post_dev_sell_tag')}</b> · <b>${sym}</b>"
    else:
        title = (f"{SELL_EMOJI if sell else chat.emoji} <b>{t(lang, 'post_sell_tag' if sell else 'post_buy_tag')}"
                 f"</b> · <b>${sym}</b>")
    if p.get("dev") and not dev_sell:
        title += " · 👨‍💻 <b>DEV</b>"
    whale = getattr(chat, "whale_usd", 0)
    if not sell and whale and (p.get("usd") or 0) >= whale:
        title += f" · 🐋 <b>{t(lang, 'post_whale')}</b>"
    lines += [title, ""]  # one mark says buy or sell; the size is in the numbers below

    lines.append(("💰 " if sell else "💵 ") + _money(p["quote"], p["quote_symbol"], p.get("usd")))
    tree = [f"{t(lang, 'post_sold' if sell else 'post_got')} <b>{fmt_compact(p['tokens'])}</b> {sym}"]
    if p.get("buyer"):
        who = short_addr(p["buyer"])
        if explorer:
            who = f'<a href="{explorer}/address/{p["buyer"]}">{who}</a>'
        note = dev_sold_line(p.get("holder"), lang) if dev_sell else holder_line(p.get("holder"), lang)
        tree.append(f"{'👨‍💻' if dev_sell else '👤'} {who}" + (f" · {note}" if note else ""))
    if p.get("mc_usd") is not None:
        tree.append(f"📊 MC <b>{fmt_usd(p['mc_usd'])}</b>")
    elif p.get("mc_quote") is not None:
        mc_symbol = p.get("mc_symbol") or p["quote_symbol"]
        tree.append(f"📊 MC <b>{fmt_compact(p['mc_quote'])} {escape(mc_symbol)}</b>")
    venue = venue_line(p)
    if venue:
        tree.append(venue)
    elif p.get("progress") is not None:
        tree.append(f"{t(lang, 'post_curve')} {progress_bar(p['progress'])} {p['progress'] * 100:.0f}%")
    if more:
        usds = [m.get("usd") for m in more]
        usd = sum(usds) if all(u is not None for u in usds) else None
        word = (plural(len(more), lang, ("sell", "sells"), ("продажа", "продажи", "продаж")) if sell else
                plural(len(more), lang, ("buy", "buys"), ("покупка", "покупки", "покупок")))
        line = t(lang, "post_more", n=len(more), word=word)
        if all(m.get("quote_symbol", p["quote_symbol"]) == p["quote_symbol"] for m in more):
            line += " " + _money(sum(m["quote"] for m in more), p["quote_symbol"], usd)
        elif usd is not None:  # trades in different quotes: only dollars add up
            line += f" <b>{fmt_usd(usd)}</b>"
        tree.append(line)
    lines += [("└ " if i == len(tree) - 1 else "├ ") + row for i, row in enumerate(tree)]
    token = p.get("token") or ""
    if token.startswith("0x") and len(token) == 42 and int(token, 16):
        lines += ["", f"CA <code>{token}</code>"]  # a tap copies it
    return "\n".join(lines)


def venue_line(p: dict) -> str | None:
    """Where a pool trade happened: 🦄 Uniswap v3, 💱 Ramses v3. None on a curve."""
    venue = p.get("venue")
    if venue not in ("v2", "v3", "v4"):
        return None
    dex = p.get("dex") or f"Uniswap {venue}"
    return f"{'🦄' if dex.startswith('Uniswap') else '💱'} {escape(dex)}"


def holder_line(h: dict | None, lang: str) -> str | None:
    """🆕 New holder · 💼 +35% · holds 0.12% · 🚪 Sold the whole bag."""
    if not h:
        return None
    if h.get("new"):
        return t(lang, "post_new_holder")
    if h.get("all"):
        return t(lang, "post_sold_all")
    change = h.get("change")
    if change is None:
        return None
    held = fmt_share(h.get("share", 0))
    pct = f"{'+' if change >= 0 else '−'}{min(abs(change) * 100, 9999):.0f}%"
    return t(lang, "post_position", change=pct, share=held)


def fmt_share(share: float) -> str:
    """A share of the supply: 1.20%, or &lt;0.01% (posts are HTML)."""
    return "&lt;0.01%" if share < 0.0001 else f"{share * 100:.2f}%"


def dev_sold_line(h: dict | None, lang: str) -> str | None:
    """🚨 Sold 35% of the bag · holds 1.20% / 🚪 Sold the whole bag. None when the
    creator's balance could not be read: then no share is shown rather than a guess."""
    if not h:
        return None
    if h.get("all"):
        return t(lang, "post_sold_all")
    change = h.get("change")
    if change is None or change >= 0:
        return None
    return t(lang, "post_dev_sold", pct=f"{min(-change * 100, 100):.0f}%", share=fmt_share(h.get("share", 0)))


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
    return t(lang, "post_grad", symbol=escape(p["symbol"]), raised=raised,
             dex=escape(p.get("dex") or "Uniswap v4"))


def buttons(p: dict, trade_url: str, explorer: str) -> list[tuple[str, str, str | None]]:
    """(label, link or callback, colour): the chart is the green one, the 24h pop-up blue;
    the scan's pop-up last."""
    out = []
    if trade_url:
        out.append(("📈 Chart", trade_url.format(token=p["token"]), "success"))
    if explorer and p.get("tx"):
        out.append(("🔍 Tx", f"{explorer}/tx/{p['tx']}", None))
    if p.get("token"):
        out.append(("🕐 24h", f"s:{p['token']}", "primary"))  # a pop-up for whoever taps it, not a message
        out.append(("🛡 Scan", f"x:{p['token']}", None))       # the /scan summary, a pop-up too
    return out


def _wallet(addr: str, explorer: str) -> str:
    who = short_addr(addr)
    return f'<a href="{explorer}/address/{addr}">{who}</a>' if explorer else who


def scan_text(s, lang: str = "en", explorer: str = "") -> str:
    """The /scan answer (scan.Scan): only what was read, each part left out or said to be
    unknown when it could not be.

        🛡 Scan · $TST · Test Token
        CA 0x…

        👑 Top 10 wallets hold 23.40% of the supply
           biggest: 0x12…ab 5.10% · 0x34…cd 3.20% · 0x56…ef 2.90%
        👥 Holders: 412
        🎯 Snipers: 6 wallets bought in the first 5 s · hold 8.20% now
        👨‍💻 Dev 0xab…cd · holds 1.20% · 🚨 sold 40% of what they got
        🔁 Wallets that sold: 37
        ✅ Ticker: no older $TST among the tokens I know
    """
    sym = escape(s.symbol)
    lines = [t(lang, "scan_title", symbol=sym, name=escape(s.name)), f"CA <code>{s.token}</code>", ""]
    if not s.complete:
        lines.append(t(lang, "scan_partial", n=f"{MAX_TRANSFERS:,}"))
    if s.top_share is not None:
        lines.append(t(lang, "scan_top", share=fmt_share(s.top_share)))
        if s.top:
            lines.append(t(lang, "scan_top_list", list=" · ".join(
                f"{_wallet(a, explorer)}{' 👨‍💻' if a == s.dev else ''} {fmt_share(share)}" for a, share in s.top)))
    if s.holders is not None:
        lines.append(t(lang, "scan_holders", n=f"{s.holders:,}"))
    secs = round(SNIPER_BLOCKS / BLOCKS_PER_SECOND)
    if s.launch_block is None:
        lines.append(t(lang, "scan_no_market"))
    elif s.snipers:
        line = t(lang, "scan_snipers", n=s.snipers, s=secs, word=plural(
            s.snipers, lang, ("wallet", "wallets"), ("кошелёк", "кошелька", "кошельков")))
        if s.sniper_share is not None:
            line += t(lang, "scan_snipers_hold", share=fmt_share(s.sniper_share))
        lines.append(line)
    elif s.snipers == 0:
        lines.append(t(lang, "scan_no_snipers", s=secs))
    if s.dev:
        parts = [t(lang, "scan_dev", who=_wallet(s.dev, explorer))]
        if s.dev_share is not None:
            parts.append(t(lang, "scan_dev_holds", share=fmt_share(s.dev_share)))
        if s.dev_sold is not None:
            parts.append(t(lang, "scan_dev_sold", pct=f"{s.dev_sold * 100:.0f}%") if s.dev_sold >= 0.005
                         else t(lang, "scan_dev_kept"))
        if s.dev_moved is not None and s.dev_moved >= 0.005:
            parts.append(t(lang, "scan_dev_moved", pct=f"{s.dev_moved * 100:.0f}%"))
        lines.append(" · ".join(parts))
    else:
        lines.append(t(lang, "scan_dev_unknown"))
    if s.sellers is not None:
        lines.append(t(lang, "scan_sellers", n=f"{s.sellers:,}"))
    if s.copies:
        lines += [t(lang, "scan_copy", symbol=escape(c_sym), who=_wallet(addr, explorer))
                  for c_sym, addr in s.copies[:3]]
    else:
        lines.append(t(lang, "scan_no_copy", symbol=sym))
    lines += ["", t(lang, "scan_footer")]
    return "\n".join(lines)


def scan_popup(s, lang: str = "en") -> str:
    """The 🛡 Scan pop-up: plain text, within Telegram's 200 characters."""
    rows = []
    if s.top_share is not None:
        rows.append(t(lang, "pop_scan_top", share=f"{s.top_share * 100:.1f}%"))
    if s.snipers is not None:
        hold = t(lang, "pop_scan_hold", share=f"{s.sniper_share * 100:.1f}%") if s.sniper_share else ""
        rows.append(t(lang, "pop_scan_snipers", n=s.snipers, hold=hold))
    if s.dev and s.dev_share is not None:
        sold = (t(lang, "pop_scan_dev_sold", pct=f"{s.dev_sold * 100:.0f}%")
                if s.dev_sold and s.dev_sold >= 0.005 else "")
        rows.append(t(lang, "pop_scan_dev", share=f"{s.dev_share * 100:.1f}%", sold=sold))
    if s.copies:
        rows.append(t(lang, "pop_scan_copy", symbol=s.copies[0][0]))
    return t(lang, "pop_scan", symbol=s.symbol, lines="\n".join(rows))[:200]


def pick_main(items: list[dict]) -> tuple[dict, list[dict]]:
    """The biggest buy is posted in full; the rest are summed in one line."""
    def size(p: dict) -> float:
        return p["usd"] if p.get("usd") is not None else p["quote"]
    main = max(items, key=size)
    rest = [p for p in items if p is not main]
    return main, rest


def sample_payload(token_symbol: str, quote_symbol: str, rate: float | None,
                   venue: str = "curve", dex: str | None = None) -> dict:
    """A /test post: a $1,050 buy at a $78K market cap (0.35 and 26 ETH at $3,000), in the
    token's own quote; without a dollar rate, 0.35 and 26 of the quote."""
    quote, mc = (1050 / rate, 78_000 / rate) if rate else (0.35, 26.0)
    return {"token": "0x" + "0" * 40, "symbol": token_symbol, "name": token_symbol,
            "quote_symbol": quote_symbol, "quote": quote, "usd": quote * rate if rate else None,
            "tokens": 2_100_000, "buyer": "0x3f927788d627d3561ff59bbfef67ebcbf0fb10a9", "dev": False,
            "tx": None, "mc_quote": mc, "mc_usd": mc * rate if rate else None,
            "progress": 0.43 if venue == "curve" else None, "venue": venue, "dex": dex}
