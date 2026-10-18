# Changelog

## 0.5.0

- 🚨 DEV SELL: when the token's creator (the Pons deployer, the Flap or pool token's
  creator) sells, the post is marked DEV SELL and its wallet line says how much of the
  creator's bag went and what they still hold. Dev sells are posted in every chat of
  the token, also with sells off and under the chat's minimum, and never merged into
  other sells. A sale through a router or a trading bot counts: it is told by the
  creator's own Transfer in that transaction. Pool and Flap tokens cost no extra
  request (their Transfers are read anyway); Pons tokens on the curve add one
  eth_getLogs to the tick's batch, asking only for Transfers their creators sent.
- `/scan` (admins in groups and channels; anyone in the private chat, one new token a
  minute) and a 🛡 Scan button under every post (a pop-up for whoever taps it): the share
  of the top 10 holders (leaving out the curve, Flap's Portal, the pools, Uniswap's
  PoolManager and routers, 0x0, 0x…dead and the token contract), holders, snipers (wallets
  that got the token from the market in its first 50 blocks, ~5 s) and what they still
  hold, what the dev holds, sold and sent to other wallets, how many wallets sold, and a
  copy-ticker warning when an older token the bot knows (stock tokens and other quotes,
  tokens chats tracked) has the same symbol. Read from the token's Transfer logs since
  its first mint, in windows under the RPC's 10,000-log cap, with the shown balances read
  back from the chain in one batch; about fifteen requests for a young token, kept ten
  minutes, one scan at a time. A history over 50,000 transfers shows only the snipers
  and the dev's balance. Honeypots are not checked (that needs a simulated sale), and the
  answer says so.

## 0.4.0

- Silverhand custom emoji: 19 animated badges (buy, sell, whale, market cap, curve,
  wallet…) drawn by `tools/make_emoji.py`. The owner's `/emojipack` uploads them as the
  bot's emoji set; then posts in groups show the badges in place of plain emoji, and
  the post buttons carry them as icons. Channels keep plain emoji (Telegram allows a
  bot's custom emoji there only with a Fragment username). Needs Telegram Premium on
  the account that owns the bot.
- Post buttons have colours: Chart green, 24h blue (Telegram clients from Feb 2026).
- Fewer requests to the public RPC, which sits behind Cloudflare: the reads of one
  watcher tick go out as one JSON-RPC batch, the watcher polls at most every 5 s on the
  public endpoint, and a Cloudflare challenge (HTTP 403) pauses requests for a minute
  instead of retrying into a longer block.
- The deploy's preflight makes about ten requests (RPC, quote tokens, stock feeds in
  batches, ETH price, bot token); its ~150 live checks of events and pools moved to
  `tools/preflight.py --full`, since on the server they shut the running bot out.
- Memory (the 300 MB cap killed the bot three times in a week): cards are drawn one at a
  time and a card's 2x layers are dropped as soon as they are used (its peak ~50 → ~25 MB
  besides ffmpeg); after each card the freed memory goes back to the system; history
  backfills run one at a time; the service gets `MemoryHigh=260M` (the kernel slows the
  bot down before `MemoryMax` kills it) and fixed glibc mmap/trim thresholds.
- Looking for a token's pools asks each factory on its own, from its first pool: two
  factories in one query cut the RPC's span to 100k blocks, ~900 calls for one search.

- No more Free and Pro: every chat gets everything. Posts carry no ad line and no
  "via @bot", "Advertise here" or "Free buy alerts" line, only "NFA · DYOR".
- Your own GIF, photo or video (`/media`) works in every chat.
- Pro, ad slots and Telegram Stars payments are gone (`/pro`, `/advertise`,
  `/grantpro`, `/refund`, `/ads`, the Pro and ad pages of the owner's panel).
  `/paysupport` became `/support`, at most one message every 10 minutes per person.
- The database drops the empty ads and payments tables; `chats.pro_until` stays,
  unused, so an older version can still read the database.

## 0.3.0

- `/track` takes any token with a Uniswap v2, v3 or v4 pool (Ramses v3 too), not
  only Pons launches. The bot finds the token's pools from their creation events,
  follows up to six of the deepest and looks again every hour. Only pools from
  Uniswap's own factories count. Posts say where a trade happened (🦄 Uniswap v3).
- Flap.sh launches: buys and sells on the curve with its progress, the graduation
  post, then the Uniswap v2 pair. A Flap curve's quote is read per token (ETH or a
  token; stock tokens are seen on mainnet).
- Dollar values for pairs with 35 Robinhood stock tokens (NVDA, TSLA, SPY…), read
  from their Chainlink feeds on the chain; Pons launches paired with a stock token
  get dollars too. A trade in a pool with another quote is converted into the
  token's main quote for the market cap and the chart.
- The buyer of a pool trade is where the tokens stop: bots' and routers' hops in
  the same transaction are followed.
- Fix: a wallet holding under 0.01% of the supply made the post invalid HTML, and
  Telegram refused it.
- Preflight checks the v2/v3/v4 pool facts, Flap's Portal and every stock feed live.

## 0.2.0

- The bot sets its description and about text itself, in each of its languages
  (`buybot/profile.py`).
- Animated description picture for @BotFather, made by `tools/make_intro.py`.
- Channels: commands posted in the channel work (answered silently, then removed),
  and plain commands in the private chat apply to the channel you last set up.
- Every command confirms what it did; `/settings` shows whether the chain is being read.
- `/sells on`: sells posted too, on the curve and on Uniswap v4, with price and
  progress from the event (checked against curve reserves, also in preflight).
- `/channel` takes the id as apps show it (without -100), @name or a link; a post
  forwarded from a channel picks that channel.
- Posts come with a market-cap candle chart drawn from the token's own trades
  (the last 6 hours are loaded when a token is tracked); `/chart off` for text only.
- A new post layout: dollars first, the numbers in a quote block; v4 trades name
  the wallet that sent the transaction.
- Languages everywhere: the first `/start` asks, `/lang` changes it; `/lang`
  in a chat sets the language of its posts. Command menus in each language.
- The private chat is a menu with an animated screen per section; **My chats**
  (`/chats`) lists where you added the bot, with buttons to manage each chat.
- `/stats`: the token's last 24 h (buys, sells, volume, market cap range, biggest buy)
  and what the bot posted in the chat. The owner's bot stats moved to `/botstats`.
- One mark per post (🟢 buy, 🔴 sell) instead of a line of emoji scaled by size;
  `/step` is gone.
- The owner's control panel (`/admin`): bot summary, Pro chats, all chats with
  Pro +7/+30/+365 days, forever or removed, ads to review, recent payments, memory,
  uptime and the last database copy (one a day in data/backups, the last seven kept).
  Chats the owner added are Pro for good.
- The chart is animated: a three-second loop Telegram plays like a GIF (a scan line,
  the trade's candle glowing, its amount rising, a LIVE dot). `/chart still` keeps
  a picture. A 📊 24h button under each post opens the token's day in a pop-up for
  whoever taps it, without posting anything in the chat.
- Forum topics: `/track` sent in a topic sends the posts there; if the topic is
  deleted, posts go back to the chat itself.
- The wallet behind a trade: 🆕 new holder, 💼 position +35% · holds 0.12% of the
  supply, or 🚪 sold the whole bag, from its balance right after the trade.
- Milestone posts: the market cap's first time past $10K, $25K, $50K, $100K … and
  the curve's past 50/75/90%, only on new highs. 🐋 on buys from `/whale` dollars.
- The animated chart is a card in the style of the intro animations, at 30 fps:
  signal tapes in the trade's color, the avatar in a spinning ring, a condensed
  title with depth, glint and glitch, the amount glowing, and over the chart a scan
  line, packets along the last price and a ping at the trade's candle.
- `/top`: the wallets that bought the most in the last day. v4 buys are named by the
  pool's Transfer of the token (one more eth_getLogs a tick), so every buy has a wallet.

## 0.1.0

First public release.

- Live buy posts for Pons v2 tokens on Robinhood Chain: on the bonding curve and,
  after graduation, in the token's Uniswap v4 pool.
- Market cap and curve progress computed from each trade, with no per-trade RPC calls.
- Graduation announcements; dev buys marked.
- Bursts merged into one post, one post per chat every 3 seconds at most.
- Group and channel setup; admin-only settings: minimum buy, emoji, emoji step, media.
- Pro per chat and reviewed ad slots, paid with Telegram Stars; refunds and `/paysupport`.
- Offline test suites and a live preflight check of every chain fact.
