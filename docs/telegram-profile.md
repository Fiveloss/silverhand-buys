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

The testnet bot (@silverhandbuysTNbot) takes `assets/brand/avatar-testnet.png` and
[`assets/brand/botfather-description-testnet.gif`](../assets/brand/botfather-description-testnet.gif).
That picture and its menu screens (`assets/menu/en`, English only) are drawn in the trade card's
vibe/vibe style: the card's header with the TESTNET pill, a bar filling to graduation and a small
chart rising with it, a seamless 3 s loop:

    python tools/make_screens.py            # the menu screens
    python tools/make_screens.py --brand    # the description picture
