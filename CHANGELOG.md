# Changelog

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
