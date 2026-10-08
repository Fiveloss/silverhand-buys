# Telegram profile

What to set in [@BotFather](https://t.me/BotFather) for the bot's profile, and
what the bot sets itself.

| BotFather command | Value |
|---|---|
| `/setname` | `Silverhand Buys` |
| `/setuserpic` | [`assets/brand/avatar.png`](../assets/brand/avatar.png) |
| `/setdescriptionpic` | [`assets/brand/botfather-description.gif`](../assets/brand/botfather-description.gif) (640×360, 3 s loop; send it as a GIF). A still version: [`botfather-description.png`](../assets/brand/botfather-description.png) |
| `/setjoingroups` | **Enable** |
| `/setprivacy` | leave **Enabled**: the bot only needs its commands |

The animation is made by `tools/make_intro.py` from the avatar
(`--title`, `--sub` and `--eyebrow` change its text).

## Set by the bot itself

On every start the bot sets its command menus in each of its languages (one for groups, one for the
private chat) and its profile texts, so `/setcommands`, `/setdescription` and
`/setabouttext` are not needed. Edits made there are overwritten on the next
restart: change the texts in [`buybot/profile.py`](../buybot/profile.py).

Both texts come in English, the default, and in each other language the bot speaks,
which Telegram shows to users whose app is in that language. The tests check them
against Telegram's limits (counted in UTF-16 units):

| Text | Where it shows | English |
|---|---|---|
| description | an empty chat with the bot, under the picture | 405 of 512 |
| about | the bot's profile and link previews | 113 of 120 |

Another language is one more key (its two-letter code) in `DESCRIPTIONS` and
`SHORT_DESCRIPTIONS`.
