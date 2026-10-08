<p align="center">
  <img src="assets/brand/banner.png" alt="Silverhand Buys: live buy alerts for Robinhood Chain token chats" width="100%">
</p>

<p align="center">
  <a href="https://t.me/SilverhandBuysBot"><b>Add @SilverhandBuysBot to your chat</b></a>
  ·
  <a href="#self-hosting">Self-host</a>
  ·
  <a href="#how-it-works">How it works</a>
</p>

<p align="center">
  <a href="https://github.com/Fiveloss/silverhand-buys/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/Fiveloss/silverhand-buys/actions/workflows/tests.yml/badge.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776ab?labelColor=0e1621">
  <img alt="aiogram 3" src="https://img.shields.io/badge/aiogram-3-2df59a?labelColor=0e1621">
  <img alt="Robinhood Chain 4663" src="https://img.shields.io/badge/chain-4663-2df59a?labelColor=0e1621">
  <a href="LICENSE"><img alt="AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0-c9d1da?labelColor=0e1621"></a>
</p>

---

**Silverhand Buys** is a Telegram buy bot for token communities on
[Robinhood Chain](https://robinhood.com/us/en/chain/). A token's admins add it to
their group or channel, and it posts every buy of their token the moment it lands
on-chain: on the bonding curve and, after graduation, on Uniswap v4.

<img src="assets/brand/post-card.gif" alt="An animated buy post" width="440" align="right">

- 💵 **Every buy, live**: amount paid, tokens received, the wallet (🆕 new holder or
  its position change), with dev buys and 🐋 whales marked; sells too if you want
- 🎞 **An animated card** with each post: the market-cap candles of the last hours,
  in the bot's style (or a still picture, or none)
- 📊 **Market cap and curve progress**, computed from the trade itself, and posts
  of their own when the market cap reaches a new round level or the curve fills
- 🎓 **Graduation announced**, then buys keep coming from the Uniswap v4 pool
- 📈 **Stats in the chat**: `/stats` for the day, `/top` for the biggest buyers, and
  a 📊 24h button under each post that answers only whoever taps it
- 🧹 **No spam**: at most one post per chat every 3 seconds; bursts are merged
- 🌐 **Your language**, per person and per chat; groups with topics work
- ⭐ **Pro**: no ad line and your own GIF, photo or video on every buy
- 🔒 **Privacy mode on**: the bot only reads its own commands, never your chat
- 🆓 **Free forever**, with one small ad line under each post

Supports **Pons v2** launches. Pairs with ETH, WETH and USDG get dollar values.

<br clear="right">

## Add it to your chat

1. Open [@SilverhandBuysBot](https://t.me/SilverhandBuysBot) and tap **Add to a group**.
2. In the group, as an admin: `/track 0xYourTokenAddress`
3. `/test` shows a sample post with your settings. That's it.

**Groups with topics:** send `/track` inside the topic where the posts should go.

**Channels:** add the bot as an admin with permission to post. Then either send
the commands in the private chat with the bot (they apply to your current chat,
picked in `/chats`), or post them in the channel itself: the bot answers there
silently and removes the command and its answer a minute later. Every command
gets a confirmation.

**The private chat** is a menu with an animated screen per section. On the first
`/start` the bot asks for your language (`/lang` changes it). **My chats**
(`/chats`) lists every group and channel you added the bot to, with buttons for a
test post, stats, sells, the chart, the posts' language and Pro.

| Command (admins only) | What it does |
|---|---|
| `/track 0x…` | post buys of this token |
| `/test` | preview a post with the current settings |
| `/minbuy 25` | skip buys under $25 |
| `/emoji 🚀` | the mark of a buy post (a sell is always 🔴) |
| `/whale 1000` | the 🐋 mark on buys from $1,000 (`0` turns it off) |
| `/sells on` · `/sells off` | post sells too (🔴 line), or only buys (the default) |
| `/chart on` · `still` · `off` | the market-cap candle chart with each post: animated (the default), a still picture, or none |
| `/stats` | the last 24 h: buys, sells, volume, market cap high and low, biggest buy, posts here |
| `/top` | the wallets that bought the most in the last 24 h |
| `/lang en` | the language of posts and answers in this chat |
| `/media` | reply to a GIF, photo or video to show it on every buy (Pro) |
| `/settings` · `/pro` · `/untrack` · `/help` | settings, Pro, stop, help |

Commands from non-admins are ignored, so nobody can flood your chat through the bot.

## Plans

| | Free | Pro |
|---|---|---|
| Every buy, market cap, curve progress | ✅ | ✅ |
| Filters and emoji | ✅ | ✅ |
| Ad line under posts | shown | **removed** |
| Your own GIF, photo or video | — | ✅ |

Pro is bought per chat, by the week, with **Telegram Stars**. Projects can also
book the ad line under posts (`/advertise` in the bot). Every ad is reviewed
before payment: no promised returns, no fake giveaways. Prices are set by
whoever runs the bot.

## How it works

```mermaid
flowchart LR
    RPC["Robinhood Chain<br/>public RPC"] -->|eth_getLogs every 3 s| W[watcher]
    W -->|buys and graduations,<br/>one transaction| DB[(SQLite outbox)]
    DB --> S[sender]
    S -->|≤ 1 post / 3 s per chat| TG[Telegram chats]
    TG -->|/track, settings, Stars| B[bot handlers] --> DB
```

- **Two sources of buys.** On the curve, each Pons v2 bonding curve emits a
  `CurveBuy` event. After graduation, the token trades in a Uniswap v4 pool
  whose PoolId the bot derives locally from the token address (Pons hook,
  fee 0, tick spacing 200). The factory's graduation event switches between
  the two.
- **Prices without extra calls.** A Pons curve is constant-product over a
  virtual reserve. With `k` read once, the price and the progress to graduation
  after a buy or a sell follow from the event alone. On Uniswap v4 the price
  comes from `sqrtPriceX96` in the swap event, and the buyer from the pool's
  Transfer of the token.
- **Gentle on the public RPC.** A handful of requests per 3-second tick, however
  many chats use the bot; only the few trades that get posted cost one or two
  more (the wallet's balance). There are backoffs on 429s, and log ranges are
  split when they get too big.
- **Nothing lost, nothing doubled.** A tick's posts and its last block are
  written in one transaction. After a long outage the bot skips ahead instead
  of flooding chats with stale buys.
- **Honest numbers.** A value the bot could not read is left out of the post,
  never shown as zero. Dollar values appear only for pairs it can price.

Every chain fact the bot relies on (addresses, event layouts, the curve model,
the PoolId derivation) is checked against real mainnet logs in
[`fixtures/`](fixtures/). [`tools/preflight.py`](tools/preflight.py) re-checks
them live before a server starts.

## Self-hosting

You need Python 3.10+, a bot token from [@BotFather](https://t.me/BotFather)
with **/setjoingroups** enabled, and a server that can reach
`rpc.mainnet.chain.robinhood.com` and `api.telegram.org`.

```bash
git clone https://github.com/Fiveloss/silverhand-buys.git buybot && cd buybot
bash deploy/install.sh        # creates .env and stops
nano .env                     # BOT_TOKEN, OWNER_IDS, prices
bash deploy/install.sh        # tests → live preflight → systemd service
journalctl -u buybot -f
```

`install.sh` refuses to start the bot unless the offline tests and the live
preflight pass. The systemd unit caps the bot at 300 MB of RAM and 30% of a
CPU, so it can share a small server.

<details>
<summary><b>Configuration</b> (<code>.env</code>)</summary>

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | from @BotFather |
| `OWNER_IDS` | — | Telegram ids that review ads and get payment and outage notices |
| `TRADE_URL` | GMGN token page | the Chart button; `{token}` is replaced, and it may carry your referral code |
| `PRO_PLANS` | `1:500,4:1500` | weeks:stars pairs for Pro |
| `AD_DAY_STARS` · `AD_MAX_DAYS` | `750` · `7` | ad slot price per day, longest booking |
| `SUPPORT_CONTACT` | — | shown by `/paysupport` |
| `SOURCE_URL` | — | link to your source, shown in `/terms` (see License) |
| `RPC_URL` · `EXPLORER_URL` | public RPC · Blockscout | chain endpoints |
| `POLL_SECONDS` · `RPC_MIN_INTERVAL` | `3` · `0.3` | how often to poll, spacing between RPC calls |

Owner commands in the private chat: `/admin` (the control panel: chats, Pro, ads,
payments, health), `/botstats`, `/ads`, `/grantpro <chat_id> <days>`, `/refund <charge_id>`.
</details>

## Development

```bash
python3 tests.py                  # standard library only, no network
pip install -r requirements.txt
python3 tests_bot.py              # the Telegram layer against a fake Bot API, no network
python3 tools/preflight.py        # live checks against Robinhood Chain
```

| Path | What lives there |
|---|---|
| `buybot/chain.py` | Pons v2 and Uniswap v4 facts, log decoders, PoolId, curve math |
| `buybot/watcher.py` | new blocks → buys and graduations → outbox |
| `buybot/sender.py` | outbox → Telegram: rate limits, merging, error handling |
| `buybot/bot.py` | commands, Telegram Stars payments, ads, owner tools |
| `buybot/render.py` | post text (pure functions) |
| `buybot/db.py` · `rpc.py` · `prices.py` · `tokens.py` | storage, JSON-RPC, ETH/USD, token lookup |

The chain facts and house rules the code relies on are in [docs/CHAIN.md](docs/CHAIN.md).
See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## Safety

The bot **never** asks for private keys or seed phrases and never trades. Anyone
who asks for them in its name is a scammer. To report a vulnerability, see
[SECURITY.md](SECURITY.md).

## Disclaimer

Silverhand Buys is not affiliated with Robinhood Markets, Inc. or Pons. Posts
show on-chain activity as it happens and are **not financial advice**.
Memecoins are extremely risky: do your own research.

## License

[AGPL-3.0](LICENSE). You can use, change and run this code. If you run a
modified copy as a service for others, you must offer them its source (set
`SOURCE_URL` so `/terms` links to it). The test fixtures include mainnet data
captured by the [realorrug](https://github.com/1xmint/realorrug) project
(Apache-2.0); see [`fixtures/README.md`](fixtures/README.md).
