"""Texts in English and Russian. Each key holds (English, Russian); t() picks one
and fills {placeholders}. A person's language is theirs (chosen on /start, changed
with /lang); a chat's language (/lang in the chat) is used for its posts and for
the bot's answers there.
"""

LANGS = ("en", "ru")
LANG_NAMES = {"en": "🇬🇧 English", "ru": "🇷🇺 Русский"}


def guess(code: str | None) -> str:
    """The language before the person picks: English, whatever Telegram's language_code
    says. The first /start asks anyway, with English first."""
    return "en"


def norm(lang: str | None) -> str:
    return lang if lang in LANGS else "en"


def plural(n: int, lang: str, en: tuple[str, str], ru: tuple[str, str, str]) -> str:
    """The word form for n: ('buy', 'buys') or ('покупка', 'покупки', 'покупок')."""
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return ru[0]
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return ru[1]
        return ru[2]
    return en[0] if n == 1 else en[1]


def t(lang: str | None, key: str, /, **kw) -> str:
    en, ru = TEXTS[key]
    s = ru if lang == "ru" else en
    return s.format(**kw) if kw else s


TEXTS: dict[str, tuple[str, str]] = {
    # ---- shared
    "not_affiliated": (
        "<i>Not affiliated with Robinhood or Pons. Not financial advice.</i>",
        "<i>Не связан с Robinhood и Pons. Не финансовый совет.</i>"),
    "done": ("✅ Done.", "✅ Готово."),
    "back": ("← Menu", "← Меню"),
    "back_chats": ("← My chats", "← Мои чаты"),
    "on": ("on", "вкл"),
    "off": ("off", "выкл"),

    # ---- language
    "lang_pick": (
        "🌐 <b>Choose your language</b>\nВыберите язык",
        "🌐 <b>Выберите язык</b>\nChoose your language"),
    "lang_set": ("✅ Language: English", "✅ Язык: русский"),
    "lang_chat_usage": (
        "Posts and my answers here are in {current}. Usage: /lang <code>en</code> or /lang <code>ru</code>",
        "Посты и мои ответы здесь: {current}. Использование: /lang <code>en</code> или /lang <code>ru</code>"),
    "lang_chat_set": (
        "✅ This chat now gets posts and answers in English.",
        "✅ Теперь посты и ответы в этом чате на русском."),

    # ---- welcome messages
    "group_welcome": (
        "👋 Thanks for adding me!\n\n"
        "I post every buy of your token here, live from Robinhood Chain.\n\n"
        "<b>Setup (admins):</b>\n"
        "1. /track <code>0xTokenAddress</code> (a Pons launch or any token with a Uniswap pool)\n"
        "2. /test to preview a post\n"
        "3. /settings to see the options · /lang <code>ru</code> for Russian\n\n"
        "No ads, nothing to buy. /media (reply to a GIF, photo or video) shows it on every buy.\n",
        "👋 Спасибо, что добавили!\n\n"
        "Я публикую здесь каждую покупку вашего токена прямо из Robinhood Chain.\n\n"
        "<b>Настройка (админы):</b>\n"
        "1. /track <code>0xАдресТокена</code> (запуск Pons или любой токен с пулом Uniswap)\n"
        "2. /test — пример поста\n"
        "3. /settings — настройки · /lang <code>en</code> для английского\n\n"
        "Без рекламы и без платных функций. /media (ответом на гифку, фото или видео) — "
        "показывать её в каждом посте.\n"),
    "channel_welcome": (
        "📢 Added to <b>{title}</b>.\n\n"
        "<b>Set it up right here</b>: commands in this chat apply to that channel.\n"
        "1. /track <code>0xTokenAddress</code> (a Pons launch or any token with a Uniswap pool)\n"
        "2. /test to post a sample in the channel\n"
        "3. /settings to see the options\n\n"
        "Commands posted in the channel itself work too: I answer there and remove the "
        "command and my answer a minute later.\n"
        "All your chats: /chats. Several channels? Pick this one with "
        "<code>/channel {chat_id} track 0x…</code>",
        "📢 Добавлен в <b>{title}</b>.\n\n"
        "<b>Настраивайте прямо здесь</b>: команды в этом чате применяются к каналу.\n"
        "1. /track <code>0xАдресТокена</code> (запуск Pons или любой токен с пулом Uniswap)\n"
        "2. /test — пример поста в канале\n"
        "3. /settings — настройки\n\n"
        "Команды прямо в канале тоже работают: я отвечу там и через минуту удалю "
        "и команду, и свой ответ.\n"
        "Все ваши чаты: /chats. Несколько каналов? Выберите этот: "
        "<code>/channel {chat_id} track 0x…</code>"),
    "channel_note": (
        "<i>This note and your command disappear in a minute.</i>",
        "<i>Это сообщение и ваша команда исчезнут через минуту.</i>"),

    # ---- help and home
    "group_help": (
        "<b>Commands (admins only)</b>\n"
        "/track <code>0x…</code> — post buys of this token\n"
        "/test — preview a post\n"
        "/settings — current settings\n"
        "/stats — buys, sells and volume of the last 24 h · /top — the biggest buyers\n"
        "/scan — top 10 holders, snipers, what the dev sold, copy ticker\n"
        "/sells <code>on</code> — post sells too (🔴), <code>off</code> to stop (🚨 dev sells always show)\n"
        "/chart <code>off</code> — posts without the price chart\n"
        "/lang <code>ru</code> — posts and answers in Russian\n"
        "/minbuy <code>25</code> — skip buys under $25\n"
        "/emoji <code>🚀</code> — the mark of a buy (a sell is 🔴)\n"
        "/whale <code>1000</code> — the 🐋 mark on buys from $1,000\n"
        "/media — reply to a GIF, photo or video to show it on every buy\n"
        "/untrack — stop posting\n\n"
        "The Chart button may carry a referral code; it costs you nothing.\n",
        "<b>Команды (только для админов)</b>\n"
        "/track <code>0x…</code> — публиковать покупки токена\n"
        "/test — пример поста\n"
        "/settings — текущие настройки\n"
        "/stats — покупки, продажи и объём за 24 ч · /top — крупнейшие покупатели\n"
        "/scan — топ-10 держателей, снайперы, что продал дев, тикер-копия\n"
        "/sells <code>on</code> — публиковать и продажи (🔴), <code>off</code> — выключить "
        "(🚨 продажи дева видны всегда)\n"
        "/chart <code>off</code> — посты без графика\n"
        "/lang <code>en</code> — посты и ответы на английском\n"
        "/minbuy <code>25</code> — не публиковать покупки меньше $25\n"
        "/emoji <code>🚀</code> — значок покупки (у продажи 🔴)\n"
        "/whale <code>1000</code> — метка 🐋 у покупок от $1,000\n"
        "/media — ответьте на гифку, фото или видео, чтобы показывать её в каждом посте\n"
        "/untrack — перестать публиковать\n\n"
        "Кнопка «Chart» может содержать реферальный код; вам это ничего не стоит.\n"),
    "help_private": (
        "In a channel, the same commands work from this chat (they apply to your current "
        "channel, see /chats) or posted in the channel itself.",
        "Для канала те же команды работают и отсюда (для текущего канала, см. /chats), "
        "и прямо в канале."),
    "home": (
        "👋 I'm a <b>buy bot for Robinhood Chain</b>.\n\n"
        "Add me to your token's group or channel and I'll post every buy there in real time: "
        "amount, buyer, market cap and a price chart. Pons launches on the bonding curve and "
        "after graduation, and any token with a Uniswap v2, v3 or v4 pool.\n\n"
        "• No ads and nothing to buy: every chat gets everything\n"
        "• Your own GIF, photo or video on every buy (/media)\n"
        "• Sells too if you want, and stats for your token\n"
        "• 🚨 Dev sells flagged, and /scan for holders, snipers and copy tickers\n\n"
        "Add me with a button below, then send /track <code>0xTokenAddress</code> in that chat.\n",
        "👋 Я <b>бот покупок для Robinhood Chain</b>.\n\n"
        "Добавьте меня в группу или канал токена, и я буду публиковать там каждую покупку "
        "в реальном времени: сумму, покупателя, капитализацию и график. Запуски Pons на "
        "бондинг-кривой и после миграции, а также любой токен с пулом Uniswap v2, v3 или v4.\n\n"
        "• Без рекламы и без платных функций: каждому чату — всё\n"
        "• Своя гифка, фото или видео в каждом посте (/media)\n"
        "• По желанию и продажи, а ещё статистика токена\n"
        "• 🚨 Продажи дева отмечены, а /scan покажет держателей, снайперов и тикеры-копии\n\n"
        "Добавьте меня кнопкой ниже, затем отправьте /track <code>0xАдресТокена</code> в том чате.\n"),

    # ---- menu buttons
    "btn_add_group": ("➕ Add to a group", "➕ Добавить в группу"),
    "btn_add_channel": ("📢 Add to a channel", "📢 Добавить в канал"),
    "btn_chats": ("📋 My chats", "📋 Мои чаты"),
    "btn_stats": ("📊 Stats", "📊 Статистика"),
    "btn_help": ("❓ Help", "❓ Помощь"),
    "btn_lang": ("🌐 Language", "🌐 Язык"),
    "btn_terms": ("📄 Terms", "📄 Условия"),
    "btn_support": ("🆘 Support", "🆘 Поддержка"),
    "btn_test": ("🧪 Test post", "🧪 Тестовый пост"),
    "btn_sells": ("🔴 Sells: {state}", "🔴 Продажи: {state}"),
    "btn_chart": ("📈 Chart: {state}", "📈 График: {state}"),
    "btn_chat_lang": ("🌐 Posts: {lang}", "🌐 Посты: {lang}"),
    "btn_refresh": ("🔄 Refresh", "🔄 Обновить"),
    "btn_top": ("🏆 Top", "🏆 Топ"),
    "top_title": ("🏆 <b>Top buyers of {symbol}</b> · 24 h", "🏆 <b>Топ покупателей {symbol}</b> · 24 ч"),
    "top_wallets": ("👥 {n} wallets bought in the last day", "👥 Кошельков, покупавших за сутки: {n}"),
    "top_none": ("🏆 No buys of {symbol} in the last day yet.", "🏆 За сутки покупок {symbol} пока нет."),

    # ---- my chats
    "chats_title": ("📋 <b>My chats</b>", "📋 <b>Мои чаты</b>"),
    "chats_none": (
        "📋 <b>My chats</b>\n\nNo chats yet. Add me to a group or channel with the buttons in the "
        "menu: I remember where you added me. For a chat added earlier, forward me any post from it.",
        "📋 <b>Мои чаты</b>\n\nПока нет чатов. Добавьте меня в группу или канал кнопками в меню: "
        "я запоминаю, куда вы меня добавили. Для чата, добавленного раньше, перешлите мне любой пост "
        "из него."),
    "chats_hint": (
        "Tap a chat to manage it here; ▶️ marks the one plain commands in this chat apply to.",
        "Нажмите на чат, чтобы управлять им отсюда; ▶️ — чат, к которому применяются команды "
        "в этом диалоге."),
    "chat_line": ("{kind} <b>{title}</b> — {token}", "{kind} <b>{title}</b> — {token}"),
    "no_token": ("no token yet", "токен не задан"),
    "chat_picked": ("▶️ Commands here now apply to {title}", "▶️ Команды здесь теперь для {title}"),

    # ---- settings
    "settings_title": ("⚙️ <b>Settings</b>", "⚙️ <b>Настройки</b>"),
    "set_token": ("Token: <b>{symbol}</b> <code>{address}</code> ({venue})",
                  "Токен: <b>{symbol}</b> <code>{address}</code> ({venue})"),
    "venue_v4": ("Uniswap v4", "Uniswap v4"),
    "venue_curve": ("bonding curve", "бондинг-кривая"),
    "set_live": ("Status: 🟢 live, chain read {s} s ago", "Статус: 🟢 работает, сеть прочитана {s} с назад"),
    "set_stale": ("Status: ⚠️ chain not read for {m} min", "Статус: ⚠️ сеть не читается {m} мин"),
    "set_no_token": ("Token: not set. Use /track <code>0x…</code>",
                     "Токен не задан. Используйте /track <code>0x…</code>"),
    "set_topic": ("Posts go to the topic where /track was sent", "Посты идут в тему, где отправили /track"),
    "set_minbuy": ("Min buy: {v}", "Мин. покупка: {v}"),
    "set_emoji": ("Buy mark: {e} · sell 🔴", "Значок покупки: {e} · продажи 🔴"),
    "set_sells_on": ("Sells: on 🔴", "Продажи: вкл 🔴"),
    "set_sells_off": ("Sells: off · /sells on", "Продажи: выкл · /sells on"),
    "set_chart_on": ("Chart: animated 🎞", "График: анимированный 🎞"),
    "set_chart_still": ("Chart: still 🖼 · /chart on for animated", "График: картинка 🖼 · /chart on — анимация"),
    "set_chart_off": ("Chart: off · /chart on", "График: выкл · /chart on"),
    "set_lang": ("Language: {lang} · /lang", "Язык: {lang} · /lang"),
    "set_media": ("Media: {m}", "Медиа: {m}"),
    "media_none": ("none", "нет"),

    # ---- actions
    "track_usage": ("Send the token's contract address: /track <code>0x…</code>",
                    "Отправьте адрес контракта токена: /track <code>0x…</code>"),
    "chain_down": ("⚠️ Robinhood Chain is not answering right now. Try again in a minute.",
                   "⚠️ Robinhood Chain сейчас не отвечает. Попробуйте через минуту."),
    "track_v4": ("🦄 Already trading on Uniswap v4.", "🦄 Уже торгуется на Uniswap v4."),
    "track_curve": ("🎓 On the bonding curve.", "🎓 На бондинг-кривой."),
    "track_flap": ("🎓 On its Flap.sh bonding curve.", "🎓 На бондинг-кривой Flap.sh."),
    "venue_flap": ("Flap.sh curve", "кривая Flap.sh"),
    "track_pools": ("🦄 Pools: {pools}", "🦄 Пулы: {pools}"),
    "track_not_token": ("❌ This address is not a token on Robinhood Chain.",
                        "❌ По этому адресу нет токена в Robinhood Chain."),
    "track_quote_unreadable": ("❌ Could not read the token's quote currency. Try again in a minute.",
                               "❌ Не удалось прочитать валюту пары токена. Попробуйте через минуту."),
    "track_no_pools": (
        "❌ <b>{symbol}</b> has no pool I can read yet. I follow Pons and Flap.sh launches and "
        "pools on Uniswap v2, v3, v4 and Ramses v3 with liquidity. "
        "Send /track again once it has one.",
        "❌ У <b>{symbol}</b> пока нет пула, который я умею читать. Я слежу за запусками Pons и "
        "Flap.sh и пулами Uniswap v2, v3, v4 и Ramses v3 с ликвидностью. "
        "Отправьте /track ещё раз, когда пул появится."),
    "tracking": (
        "✅ Tracking started: posting buys of <b>{symbol}</b> ({name}).\n{venue}\n"
        "Every new buy is posted the moment it lands on the chain{sells}\n\n"
        "Min buy: {minbuy} · /test posts a sample now · /settings",
        "✅ Отслеживание запущено: публикую покупки <b>{symbol}</b> ({name}).\n{venue}\n"
        "Каждая новая покупка появляется сразу, как только прошла в сети{sells}\n\n"
        "Мин. покупка: {minbuy} · /test — пример поста · /settings"),
    "tracking_sells_on": ("; sells too.", "; продажи тоже."),
    "tracking_sells_off": (". Sells too? /sells on", ". Нужны и продажи? /sells on"),
    "untracked": ("Stopped. Use /track <code>0x…</code> to start again.",
                  "Остановлено. /track <code>0x…</code> — начать снова."),
    "minbuy_usage": ("Usage: /minbuy <code>25</code> (dollars, 0 to 1,000,000; 0 posts every buy)",
                     "Использование: /minbuy <code>25</code> (доллары, от 0 до 1 000 000; 0 — все покупки)"),
    "minbuy_set": ("✅ Buys under {v} will be skipped.", "✅ Покупки меньше {v} публиковаться не будут."),
    "emoji_usage": ("Usage: /emoji <code>🚀</code> (one emoji or a short emoji combo)",
                    "Использование: /emoji <code>🚀</code> (одно эмодзи или короткая комбинация)"),
    "emoji_set": ("✅ Buy mark: {v}", "✅ Значок покупки: {v}"),
    "media_removed": ("✅ Media removed.", "✅ Медиа убрано."),
    "media_usage": ("Reply with /media to a GIF, photo or video.",
                    "Ответьте командой /media на гифку, фото или видео."),
    "media_set": ("✅ Saved. Every buy post will show this {kind}. /test to preview.",
                  "✅ Сохранено. Каждый пост будет с этим медиа ({kind}). /test — посмотреть."),
    "sells_state": ("Sells are {state}. Usage: /sells <code>on</code> or /sells <code>off</code>",
                    "Продажи: {state}. Использование: /sells <code>on</code> или /sells <code>off</code>"),
    "sells_on": ("✅ Sells are on: every sell is posted too, with a 🔴 line. Min buy applies to them.",
                 "✅ Продажи включены: каждая продажа тоже публикуется, с красной строкой 🔴. "
                 "Мин. сумма действует и на них."),
    "sells_off": ("✅ Sells are off: only buys are posted.", "✅ Продажи выключены: публикуются только покупки."),
    "chart_state": ("The chart is {state}. Usage: /chart <code>on</code> (animated), "
                    "/chart <code>still</code> (a picture) or /chart <code>off</code>",
                    "График: {state}. Использование: /chart <code>on</code> (анимированный), "
                    "/chart <code>still</code> (картинка) или /chart <code>off</code>"),
    "chart_on": ("✅ Chart on: each post comes with an animated price chart. /test to see it.",
                 "✅ График включён: каждый пост идёт с анимированным графиком. /test — посмотреть."),
    "chart_still": ("✅ Chart: a still picture with each post. /chart on for the animated one.",
                    "✅ График: картинка в каждом посте. /chart on — анимированный."),
    "chart_off": ("✅ Chart off: posts are text only.", "✅ График выключен: посты только текстом."),
    "state_animated": ("animated", "анимированный"),
    "state_still": ("a still picture", "картинка"),
    "test_first": ("First set the token: /track <code>0x…</code>", "Сначала задайте токен: /track <code>0x…</code>"),
    "test_failed": ("Could not post: {e}", "Не удалось опубликовать: {e}"),
    "test_sent": ("✅ Sample post sent. Real buys will look like this.",
                  "✅ Тестовый пост отправлен. Настоящие покупки будут выглядеть так же."),

    # ---- private chat and channels from it
    "not_in_chat": (
        "I'm not in a chat with id <code>{chat_id}</code>. Add me to it as an admin, or forward me "
        "any post from it and I'll pick it up.",
        "Меня нет в чате с id <code>{chat_id}</code>. Добавьте меня туда админом или перешлите "
        "мне любой пост оттуда."),
    "only_admins": ("Only admins of that chat can change its settings.",
                    "Менять настройки могут только админы этого чата."),
    "channel_usage": ("Usage: /channel <code>channel_id</code> <code>command</code> [args]\n"
                      "e.g. <code>/channel -1001234567890 track 0x…</code>",
                      "Использование: /channel <code>id_канала</code> <code>команда</code> [аргументы]\n"
                      "например <code>/channel -1001234567890 track 0x…</code>"),
    "no_current_chat": (
        "/{name} works in your group or channel: send it there and I answer there.\n\n"
        "To do it from here, pick a chat in /chats (or forward me any post from the channel). "
        "After that, plain commands here apply to that chat.",
        "/{name} работает в вашей группе или канале: отправьте её там, и я отвечу там же.\n\n"
        "Чтобы делать это отсюда, выберите чат в /chats (или перешлите мне любой пост из канала). "
        "После этого простые команды здесь применяются к нему."),

    # ---- stats
    "stats_title": ("📊 <b>{title}</b> · <b>{symbol}</b>", "📊 <b>{title}</b> · <b>{symbol}</b>"),
    "stats_window": ("<b>Last {h} h on the chain</b>", "<b>За последние {h} ч в сети</b>"),
    "stats_buys": ("🟢 {n} {word} · <b>{usd}</b> ({q})", "🟢 {n} {word} · <b>{usd}</b> ({q})"),
    "stats_sells": ("🔴 {n} {word} · <b>{usd}</b> ({q})", "🔴 {n} {word} · <b>{usd}</b> ({q})"),
    "stats_net": ("⚖️ Net flow: <b>{v}</b>", "⚖️ Чистый приток: <b>{v}</b>"),
    "stats_mc": ("📊 MC now <b>{now}</b> · high {hi} · low {lo}",
                 "📊 MC сейчас <b>{now}</b> · макс. {hi} · мин. {lo}"),
    "stats_change": ("📈 Change: <b>{v}</b>", "📈 Изменение: <b>{v}</b>"),
    "stats_whale": ("🐋 Biggest buy: <b>{v}</b>", "🐋 Крупнейшая покупка: <b>{v}</b>"),
    "stats_posted": ("<b>Posted in this chat</b>\nToday {d} · 7 days {w} · all time {a}",
                     "<b>Опубликовано в этом чате</b>\nСегодня {d} · 7 дней {w} · всего {a}"),
    "stats_no_trades": ("No trades in the last hours yet.", "За последние часы сделок пока нет."),
    "stats_no_token": ("📊 No token tracked here yet: /track <code>0x…</code>",
                       "📊 Здесь пока не отслеживается токен: /track <code>0x…</code>"),
    "stats_pick": ("📊 <b>Stats</b>\n\nPick a chat in 📋 My chats first.",
                   "📊 <b>Статистика</b>\n\nСначала выберите чат в 📋 Мои чаты."),
    "stats_usd_note": ("<i>≈ dollars at the current {sym} price</i>", "<i>≈ доллары по текущему курсу {sym}</i>"),

    # ---- terms, support
    "terms": (
        "<b>Terms</b>\n\n"
        "• The bot shows no ads and sells nothing: every chat gets every feature.\n"
        "• Numbers come from Robinhood Chain as they happen. A number the bot could not read is "
        "left out, never guessed.\n"
        "• The Chart button may carry a referral code.\n"
        "• The bot never asks for keys or seed phrases. Anyone who does is not us.\n",
        "<b>Условия</b>\n\n"
        "• Бот не показывает рекламу и ничего не продаёт: каждому чату доступно всё.\n"
        "• Цифры берутся из Robinhood Chain в момент сделки. Если число не удалось прочитать, "
        "его нет в посте: мы не подставляем догадки.\n"
        "• Кнопка «Chart» может содержать реферальный код.\n"
        "• Бот никогда не просит ключи или seed-фразы. Кто просит — это не мы.\n"),
    "terms_source": ("• Source code (AGPL-3.0): {url}\n", "• Исходный код (AGPL-3.0): {url}\n"),
    "support": (
        "🆘 <b>Questions or a problem?</b>\n\nSend /support followed by what happened{contact}.",
        "🆘 <b>Вопрос или проблема?</b>\n\nОтправьте /support и опишите, что случилось{contact}."),
    "support_contact": (" or write to {c}", " или напишите {c}"),
    "support_sent": ("Sent. You'll get an answer here.", "Отправлено. Ответ придёт сюда."),
    "support_failed": ("Couldn't deliver it right now. Please try again later.",
                       "Не удалось доставить. Попробуйте позже."),
    "support_wait": ("You just wrote to us. Please wait {m} min before the next message.",
                     "Вы только что писали. Подождите {m} мин до следующего сообщения."),

    # ---- posts
    "post_test": ("🧪 <b>TEST POST</b> · sample numbers", "🧪 <b>ТЕСТОВЫЙ ПОСТ</b> · примерные цифры"),
    "post_buy": ("Buy!", "Покупка!"),
    "post_buy_tag": ("BUY", "ПОКУПКА"),
    "post_sell_tag": ("SELL", "ПРОДАЖА"),
    "post_sell": ("Sell!", "Продажа!"),
    "post_got": ("🪙 Got", "🪙 Получено"),
    "post_sold": ("🪙 Sold", "🪙 Продано"),
    "post_curve": ("🎓 Curve", "🎓 Кривая"),
    "post_new_holder": ("🆕 New holder", "🆕 Новый держатель"),
    "post_whale": ("WHALE", "КИТ"),
    "card_new_high": ("NEW HIGH", "НОВЫЙ МАКСИМУМ"),
    "card_curve": ("CURVE", "КРИВАЯ"),
    "card_curve_full": ("BONDING CURVE", "БОНДИНГ-КРИВАЯ"),
    "post_level_mc": ("🚀🚀🚀\n<b>{symbol}</b> hit <b>{level}</b> market cap!",
                      "🚀🚀🚀\n<b>{symbol}</b>: капитализация достигла <b>{level}</b>!"),
    "post_level_curve": ("🎓 <b>{symbol}</b>: the bonding curve is <b>{pct}%</b> full\n{bar}",
                         "🎓 <b>{symbol}</b>: бондинг-кривая заполнена на <b>{pct}%</b>\n{bar}"),
    "whale_usage": ("Buys from {v} get the 🐋 mark. Usage: /whale <code>1000</code> (dollars, 0 turns it off)",
                    "Покупки от {v} получают метку 🐋. Использование: /whale <code>1000</code> (доллары, 0 — выключить)"),
    "whale_set": ("✅ Buys from {v} get the 🐋 mark.", "✅ Покупки от {v} получают метку 🐋."),
    "whale_off": ("✅ The 🐋 mark is off.", "✅ Метка 🐋 выключена."),
    "set_whale": ("Whale mark: from {v} · /whale", "Метка кита: от {v} · /whale"),
    "post_sold_all": ("🚪 Sold the whole bag", "🚪 Продал всё"),
    "post_dev_sell_tag": ("DEV SELL", "ДЕВ ПРОДАЁТ"),
    "post_dev_sold": ("🚨 sold {pct} of the bag · holds {share}", "🚨 продал {pct} своей позиции · держит {share}"),
    "post_position": ("💼 {change} · holds {share}", "💼 {change} · держит {share}"),
    "post_more": ("➕ {n} more {word}", "➕ ещё {n} {word}"),
    "post_grad": (
        "🎓🎓🎓\n<b>{symbol} graduated!</b>\n\nThe bonding curve is full ({raised} raised). "
        "Trading moves to {dex}, and buys there keep showing up here.",
        "🎓🎓🎓\n<b>{symbol} вышел с кривой!</b>\n\nБондинг-кривая заполнена (собрано {raised}). "
        "Торги переходят на {dex}, и покупки оттуда тоже будут здесь."),

    # ---- the owner's control panel
    "btn_panel": ("🛠 Control panel", "🛠 Панель управления"),
    "adm_title": ("🛠 <b>Control panel</b>", "🛠 <b>Панель управления</b>"),
    "adm_summary": (
        "💬 Chats: <b>{active}</b> active · {tracking} tracking\n"
        "🪙 Tokens tracked: {tokens} · queue {outbox}\n"
        "👤 Users: {users}\n"
        "{watcher}",
        "💬 Чатов: <b>{active}</b> активных · {tracking} с токеном\n"
        "🪙 Токенов отслеживается: {tokens} · очередь {outbox}\n"
        "👤 Пользователей: {users}\n"
        "{watcher}"),
    "adm_health": ("🧠 Memory {mb} MB of 300 · up {up} h · 💾 last backup {backup}",
                   "🧠 Память {mb} МБ из 300 · работает {up} ч · 💾 копия базы {backup}"),
    "adm_watch_ok": ("🟢 Chain read {s} s ago · block lag {lag}",
                     "🟢 Сеть прочитана {s} с назад · отставание {lag} бл."),
    "adm_watch_bad": ("⚠️ Chain not read for {m} min: {err}", "⚠️ Сеть не читается {m} мин: {err}"),
    "adm_btn_chats": ("💬 All chats", "💬 Все чаты"),
    "adm_btn_back": ("← Panel", "← Панель"),
    "adm_chats_title": ("💬 <b>All chats</b> ({n}) · page {page}", "💬 <b>Все чаты</b> ({n}) · стр. {page}"),
    "adm_none": ("Nothing here yet.", "Пока пусто."),
    "adm_chat_hint": ("Tap a chat to see its details.", "Нажмите на чат, чтобы посмотреть подробности."),
    "adm_chat": (
        "{kind} <b>{title}</b>\n<code>{chat_id}</code>\n\n"
        "Added by: {added_by}\nToken: {token}\nLanguage: {lang} · sells {sells} · chart {chart}\n"
        "Posted: today {d} · 7 days {w} · all time {a}",
        "{kind} <b>{title}</b>\n<code>{chat_id}</code>\n\n"
        "Добавил: {added_by}\nТокен: {token}\nЯзык: {lang} · продажи {sells} · график {chart}\n"
        "Опубликовано: сегодня {d} · 7 дней {w} · всего {a}"),
    "adm_unknown": ("unknown", "неизвестно"),
    "adm_not_owner": ("This is for the bot's owner.", "Это только для владельца бота."),

    # ---- the 📊 24h pop-up under posts (plain text, at most 200 characters)
    "pop": ("{symbol} · last {h} h\n🟢 {buys} {bw} · {bv}\n🔴 {sells} {sw} · {sv}\n"
            "📊 MC {mc} · {change}\n⬆ {hi} · ⬇ {lo}",
            "{symbol} · за {h} ч\n🟢 {buys} {bw} · {bv}\n🔴 {sells} {sw} · {sv}\n"
            "📊 MC {mc} · {change}\n⬆ {hi} · ⬇ {lo}"),
    "pop_none": ("{symbol}: no trades in the last hours yet.", "{symbol}: за последние часы сделок пока нет."),

    # ---- /scan and the 🛡 Scan button
    "btn_scan": ("🛡 Scan", "🛡 Скан"),
    "scan_usage": ("Send a token's contract address: /scan <code>0x…</code>",
                   "Отправьте адрес контракта токена: /scan <code>0x…</code>"),
    "scan_not_token": ("❌ That address is not a token.", "❌ По этому адресу нет токена."),
    "scan_no_history": ("❌ No mint of this token found on Robinhood Chain.",
                        "❌ Выпуск этого токена в Robinhood Chain не найден."),
    "scan_wait": ("⏳ One scan a minute, please: try again in {s} s.",
                  "⏳ Не чаще одного скана в минуту: попробуйте через {s} с."),
    "scan_title": ("🛡 <b>Scan · ${symbol}</b> · {name}", "🛡 <b>Скан · ${symbol}</b> · {name}"),
    "scan_top": ("👑 Top 10 wallets hold <b>{share}</b> of the supply",
                 "👑 У топ-10 кошельков <b>{share}</b> от выпуска"),
    "scan_top_list": ("   biggest: {list}", "   крупнейшие: {list}"),
    "scan_holders": ("👥 Holders: <b>{n}</b>", "👥 Держателей: <b>{n}</b>"),
    "scan_snipers": ("🎯 Snipers: <b>{n}</b> {word} bought in the first {s} s",
                     "🎯 Снайперы: <b>{n}</b> {word} купили в первые {s} с"),
    "scan_snipers_hold": (" · hold <b>{share}</b> now", " · сейчас у них <b>{share}</b>"),
    "scan_no_snipers": ("🎯 Snipers: none in the first {s} s", "🎯 Снайперы: в первые {s} с никого"),
    "scan_no_market": ("🎯 Snipers: no market opened for it yet", "🎯 Снайперы: рынка у токена ещё нет"),
    "scan_dev": ("👨‍💻 Dev {who}", "👨‍💻 Дев {who}"),
    "scan_dev_holds": ("holds <b>{share}</b>", "держит <b>{share}</b>"),
    "scan_dev_sold": ("🚨 sold <b>{pct}</b> of what they got", "🚨 продал <b>{pct}</b> полученного"),
    "scan_dev_kept": ("has not sold", "не продавал"),
    "scan_dev_moved": ("sent {pct} to other wallets", "переслал {pct} на другие кошельки"),
    "scan_dev_unknown": ("👨‍💻 Dev: unknown", "👨‍💻 Дев: неизвестен"),
    "scan_sellers": ("🔁 Wallets that sold: <b>{n}</b>", "🔁 Кошельков, которые продавали: <b>{n}</b>"),
    "scan_copy": ("⚠️ <b>Copy ticker</b>: an older token is also ${symbol}: {who}",
                  "⚠️ <b>Тикер-копия</b>: ${symbol} — это и тикер более старого токена: {who}"),
    "scan_no_copy": ("✅ Ticker: no older ${symbol} among the tokens I know",
                     "✅ Тикер: среди известных мне токенов нет более старого ${symbol}"),
    "scan_partial": ("⏳ Over {n} transfers: holders and the dev's sales were not counted.",
                     "⏳ Больше {n} переводов: держателей и продажи дева не считал."),
    "scan_footer": ("<i>Not checked: whether it can be sold (honeypot), which needs a simulated sale. "
                    "From the chain's Transfer logs, as of a minute ago.</i>",
                    "<i>Не проверено, можно ли его продать (honeypot): для этого нужна симуляция продажи. "
                    "По Transfer-логам сети, на минуту назад.</i>"),
    "pop_scan": ("🛡 ${symbol}\n{lines}\nHoneypot not checked", "🛡 ${symbol}\n{lines}\nHoneypot не проверялся"),
    "pop_scan_top": ("Top 10: {share}", "Топ-10: {share}"),
    "pop_scan_snipers": ("Snipers: {n}{hold}", "Снайперы: {n}{hold}"),
    "pop_scan_hold": (", hold {share}", ", у них {share}"),
    "pop_scan_dev": ("Dev: holds {share}{sold}", "Дев: держит {share}{sold}"),
    "pop_scan_dev_sold": (", sold {pct}", ", продал {pct}"),
    "pop_scan_copy": ("⚠ Copy of an older ${symbol}", "⚠ Копия более старого ${symbol}"),
    "pop_scan_running": ("🛡 Scanning ${symbol}… Tap again in about 20 s.",
                         "🛡 Сканирую ${symbol}… Нажмите ещё раз секунд через 20."),
    "pop_scan_failed": ("🛡 The chain did not answer. Try again in a minute.",
                        "🛡 Сеть не ответила. Попробуйте через минуту."),
}
