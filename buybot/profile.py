"""The bot's profile texts: the description shown in an empty chat and the short
"about" text. English is the default; users whose Telegram is in Russian see the
Russian ones. main.py syncs them on every start, so edit them here, not in @BotFather.
"""

from aiogram import Bot

# Telegram's limits, counted in UTF-16 units.
DESCRIPTION_MAX = 512
SHORT_DESCRIPTION_MAX = 120

# Key None is the default for every language without its own text.
DESCRIPTIONS = {
    None: (
        "⚡ Every buy of your token, posted in your chat the moment it lands on Robinhood Chain.\n"
        "\n"
        "💵 Amount, buyer and market cap\n"
        "🎓 Pons curves, then any Uniswap v2/v3/v4 pool\n"
        "🧹 No spam: bursts merge into one post\n"
        "🖼 Your own GIF in every post · no ads\n"
        "\n"
        "Start: add me to your group, then send /track 0xTokenAddress\n"
        "\n"
        "Not paper hands. Silver hands.\n"
        "Not affiliated with Robinhood or Pons. Not financial advice."
    ),
    "ru": (
        "⚡ Каждая покупка вашего токена — в вашем чате, как только она прошла в Robinhood Chain.\n"
        "\n"
        "💵 Сумма, покупатель и капитализация\n"
        "🎓 Кривые Pons и любые пулы Uniswap v2/v3/v4\n"
        "🧹 Без спама: серия покупок — один пост\n"
        "🖼 Своя гифка в каждом посте · без рекламы\n"
        "\n"
        "Старт: добавьте меня в группу и отправьте /track 0xАдресТокена\n"
        "\n"
        "Not paper hands. Silver hands.\n"
        "Не связан с Robinhood и Pons. Не финансовый совет."
    ),
}
SHORT_DESCRIPTIONS = {
    None: "Every buy of your token, live in your chat. Robinhood Chain. No ads. "
          "Not affiliated with Robinhood or Pons.",
    "ru": "Каждая покупка вашего токена — сразу в вашем чате. Robinhood Chain. Без рекламы. "
          "Не связан с Robinhood и Pons.",
}


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


async def sync_profile(bot: Bot) -> list[str]:
    """Set the texts Telegram does not have yet; returns what was changed.
    Reading first keeps restarts from spending the rate limit on these calls."""
    changed = []
    for lang, text in DESCRIPTIONS.items():
        if (await bot.get_my_description(language_code=lang)).description != text:
            await bot.set_my_description(description=text, language_code=lang)
            changed.append(f"description {lang or 'default'}")
    for lang, text in SHORT_DESCRIPTIONS.items():
        if (await bot.get_my_short_description(language_code=lang)).short_description != text:
            await bot.set_my_short_description(short_description=text, language_code=lang)
            changed.append(f"about {lang or 'default'}")
    return changed
