# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org).

## [Unreleased]

### Added
- Watchlist tab rebuilt (Bilal, 31 Oct): the volume-spike watchlist only (patterns and channel setups already live in Trade Ideas, where their track records moved): headline numbers (hits today / 7 / 30 days, the rule's 30-day record vs every coin-day), one sticky bar (period, search, sort, cards or table), a card per hit (the day it was added, volume spike, RSI, buyers' share, since-the-hit return on the live price, 7- and 30-day outcomes or when they are due) and a sortable table grouped by day; the rule's track record in a drawer.
- World tab (Bilal, 31 Oct), right after Overview: the latest session in one look (stocks by region incl. KSE-100 and Tadawul, commodities, currencies incl. USD/PKR, US / German / UK / Japanese yields, crypto, the 10 largest US companies), a risk-on / risk-off tally, VIX and crypto Fear & Greed, a US sector heat map (1D to 1Y), the week's high / medium impact economic calendar, and market-moving headlines tagged and ranked by likely impact (central banks, inflation & data, geopolitics & trade, energy, metals, earnings, crypto, Pakistan). bin/world_markets.py refreshes it hourly into data/world.db (every hourly price and every headline kept) and web_export/world.json. Sources: TradingView's public screener for prices (Yahoo and Stooq refuse the server), public RSS feeds and Google News for headlines (headline, source, time and link only), Forex Factory for the calendar, alternative.me for crypto sentiment. Ops: world-probe, world-refresh.
- Live bot engine (Binance), hardened from an independent review (30 Oct): deterministic client order ids and intent-first buys and sells (a lost reply is looked up, never bought or sold twice); the kill switch is re-read right before entries; protection ids saved before sending, missing or rejected protection placed again every run (never while an exit is under way); an exit never sells while its protection may still lock the coins, and what does not sell stays open; both OCO legs and partial fills are booked once each (fills_seen); HARD_CAP_USDT limits the USDT at work and positions are capped at 6 in code; a buy that fills nothing is void (no crash); one position's error never stops the others; management and FLATTEN_NOW cover every bot's positions, and FLATTEN_NOW also sets STOP_TRADING. tests/test_live_engine.py reproduces every finding.
- Market making, hardened for real money (Bilal, 30 Oct): every order is written to the database before it is sent; a lost reply is checked with the exchange instead of re-sent; an order the exchange does not know is set aside and reported, never guessed; orphan "bm" orders are cancelled at start; one open-orders call per coin per loop (far under MEXC's limits); a buy that may take liquidity keeps a 0.2% fee reserve so its resell never overdraws; at most 6 upside-stop buy-backs a coin a day; no action while the book is 15%+ off the last 1h close; a 25%+ move in an hour pulls the sells for 2 hours; one coin's errors never stop the others (5 in a row: 10-minute pause and an alert); code-level ceilings ($15,000 a coin, $120,000 in all, checked when a coin starts); the bot refuses to start on a key that can withdraw; private heartbeat for ops. New: mm_preflight.py (read-only GO / NO-GO with the real key), mm_plan.py (the per-coin plan from today's prices; --apply only by Bilal at the terminal, never switches trading on), ops mm-preflight / mm-plan / mm-status, health checks the market maker's heartbeat. Tests: lost replies, unknown orders, orphans, fee reserve, caps, sanity, circuit, isolation, ceilings, preflight, plan, and a two-week chaos run with network faults on every call.
### Changed
- New positions: 40% of each $25K position market-made ($10,000 in 20 units of $500), up from 25% (Bilal, 30 Oct). Paper runs restarted at the new size (the 25% runs are archived); the other-coin and tuning studies rerun at 40%.
- MM-Bee for new positions (Bilal, 29 Oct): optional upside stop "chase" (a sold unit buys back at the market once the price is 8% over its sell); research/mm_bee_tune.py (18 settings x 15 coins x falling / rising year, ops bee-tune) found MM-Bee Pro + buy back at +8% the most robust. Paper runs of that setup on MORPHO, ONDO, CAKE, UNI, PEPE, JUP, PENGU and TAO ($25K position, 25% market-made, public MEXC prices, simulated money), and a "New positions" card on MM Bots with the study and the paper results.
- Cup & handle and Double tops & bottoms (Bilal): bin/patterns.py scans every tracked pair (universe, not stablecoins, 20-day volume >= $200K or added on purpose) on the daily chart and on complete weekly candles; statuses near the line / handle, broken, retest, broken earlier; target = measured move; two new Trade Ideas sections with cards, a full table, the rules and the track record, and the patterns drawn on the pair chart (daily and weekly). Track record from research/patterns_bt.py (liquid pairs, 2022 on, next-open entry): see docs/RESEARCH.md.
- "Added" date on every listed entry (Bilal): channel calls, head and shoulders, channels, cup & handle, double tops / bottoms (bin/first_seen.py keeps the first day an entry was listed while it stays listed; long-term breakouts keep their breakout date), on the cards and in the tables.
### Changed
- Pair chart, cleaner: separate toggles for Patterns (on), Ideas and Bot trades (off by default); no backtest-trade dots; one marker per candle and label; at most two channels drawn (the second without a midline); channel and target lines no longer stretch the price scale; broken-earlier patterns drawn thin and dashed without a target; the setups' head-and-shoulders is not drawn twice; the legend names each pattern drawn with its line and target; a chart with a pattern in play opens zoomed on it; indicator lines (EMAs, Bollinger, cloud) no longer stretch the price scale either.
- MM-Bee periods 15-D, 1-M, 3-M, 6-M and 1-Y (Bilal): every backtest period ends at the latest hour and starts at the 00:00 UTC close that many days earlier, for all four bots and for the other-coin study; MM Bots and Backtest switch between them. Lighter data (compact hourly points, no duplicated run).
### Fixed
- binance_sync: a local `import subprocess` shadowed the module import (CI ruff F823) — removed before it deployed.
- MM-Bee tab, "MM Bots" (now the first section): a plain explainer for the team and for visitors: how a market-making bot earns on coins you hold (with a ladder diagram on today's numbers), what MM-Bee Pro would have done had it gone live on 13 Oct (simulated on real MEXC prices, rerun hourly), the full-year backtest, the four bots side by side, up/down/flat robustness, the same idea on 15 other coins, how to build one, where we really are (paper since 28 Oct), and the risks. Simulated figures are labelled as such; the live record is the paper trading.
- research/mm_bee_multi.py writes data/mm_bee_multi.json (also the last 15 days per coin); the engine refreshes it once a day in the background and publishes it with the MM-Bee data.
- MM-Bee Pro (paper variant bee_pro, backtest "pro"): first sell +4%, then every 1%, buy back 4.5% lower, resell 4.5% higher, recycling, BTC guard, no fast mode. Chosen from 243 settings (research/mm_bee_study4.py) as the best on all three ZIG paths over a year: +31,249 USDT ahead of holding on the real (falling) year, +65,292 on its mirror (rising), +17,516 on the flat version; both halves of the real year ahead (+21,028 / +9,145). Regime switching (recycling off in flat markets) was tested (study5) and was not consistently better, so it stays off.
- Engine deploys research/ (ops: bee-study runs the multi-coin MM-Bee study on the server).
- MM-Bee v2 (Bilal, 29 Oct): the base follows every 4-hour close; the 5 nearest orders move right after it, the rest 150 s later; orders move one at a time 2-4 s apart; a change under 0.3% leaves the order in place; sold units keep their buy-backs. BTC guard (BTC +2% in ~2 h while ZIG < +1%: sells pulled for 8 h) and fast mode (ZIG 10%+ in the 4h/day candle: base follows every 1h close until under 5%). Optional accumulation after the first sells.
- MM-Bee+ (paper variant bee_plus): v2 with bought-back units rejoining the sell ladder ("recycle"), no fast mode. Over a year of ZIG it was +20,175 USDT ahead of holding (v2 +12,758) and, on the same year mirrored into an uptrend, +42,942 (v2 -21,836).
- Backtest: v2, MM-Bee+ and v1 over 5 days, 15 days and 1 year, with BTC candles for the guard; the MM-Bee tab compares them and the two paper runs.
- research/mm_bee_study*.py: settings grid, recycle on real / mirrored / flat paths, guards.
- Bots live view (bin/bot_live.py -> bot_live.json, every engine run): a daily signal is shown entered at the 00:00 UTC open right after the close, with its stop, profit-take and break-even move checked on hourly candles; Fade 4h shorts are shown from the open after the signal candle. Before, the page showed "order for the next open" until the following close although the trade had been running since the open. View only: the paper record is unchanged.
- Simulator entry modes for research (bot_sim: entry = open / limit / stop) and research/entry_study.py; results in docs/RESEARCH.md (current rules kept: they beat every variant on the unseen period).
- MM-Bee backtest: second scenario "live 5 days ago" (from the 24 Oct 00:00 UTC daily close) beside the 15-day run; the tab switches between them and shows the wallet's own change next to holding.

### Fixed
- MM-Bee: after a 12-hour base revision the moved (or new) ladder is placed in the same cycle. Before, it waited one cycle; in the hourly backtest that let the price jump past the lower levels unsold.
- MM-Bee (bin/mm_bee.py): Bilal's sell-first ZIG ladder. Base = previous daily close; 20 sells of 15,000 ZIG from +3%, every 2% of the base; each filled sell is bought back 3.1% lower, each buy-back sold again 3% higher, looping; 12 hours without a fill revises the base to the latest hourly close (or, when every unit is sold, opens one more 300,000 ZIG ladder). Post-only, buys paid from sell proceeds, at most 10,000 USDT of the wallet's own on top. Runs on paper (bees-mm-paper, variant "bee") beside the grid settings; live only through mm.json with "mode": "bee" after approval.
- MM-Bee backtest (bin/mm_bee_backtest.py): the same code on MEXC 1-hour ZIG candles since 13 Oct 2026, conservative fills (only on a later candle that trades through), rerun every new hour.
- Website: MM-Bee tab (Live paper / Backtest) built from data/mmbee.json (bin/mm_site.py reads only the paper files and the backtest; never the live market maker).
- Browser smoke test: fails when the page script errors while loading or never starts.

### Changed
- Market-making grid (live code and paper): the orders nearest the market now always rest, as in the backtest. Before, after a level sold and was rebought while the price stayed above it, its slot stayed with a far level, so the price moving back through the nearest levels earned nothing (seen on the ZIG paper run: levels 1-3 idle, 4-13 resting). The farthest resting order is cancelled and re-armed to make room (at most 3 per cycle). Same for buy-first rungs. On 8 simulated price paths: 52% more completed rounds.
- Highlights is now **Trade Ideas** (its signal-based section is renamed Signal confluence). Latest uses the same
  cards as the sections, grouped by day, 36 at a time.
- Overview redesigned: Market today first; What matters today as a grid of tiles; a new Best trade ideas right now
  row (grade A channel calls, recent grade A long-term breakouts, necklines in play); Top gainers next to a new Top
  losers; Top 50 shows its top 10 with Show all 50; the low-value number tiles are gone.
- Highlights redesigned around cards: every section opens with a short title, one plain sentence and its filters,
  then one card per item (coin, side and grade badges, the status in words, price / stop / target or the section's
  key numbers in a 2 x 2 grid, the reasons as a short list, date and record in the footer; a coloured edge shows
  bullish or bearish). The dense tables, long explanations and track records moved into one "details" drawer per
  section. Latest is a clean feed with same-day Shortlist picks grouped into one line.

### Added
- Live checks for the daily ops review: `tools/live_check.py` (published data fresh, complete and consistent) and
  `tools/site_smoke.js URL` (every tab of the live site on desktop and phone, no JavaScript errors).
- Database upkeep (`db_maint.py`, once a day after the daily rebuild): quick integrity check of every database
  (full check of binance.db on Sundays), an online, verified backup of each (last 2 kept, skipped when disk is
  short), WAL checkpoint and planner statistics. Problems raise an alert; results show in the ops status.
- Website smoke test in CI: a synthetic data package is built with the real analytics, bots and export, then every
  tab, section and chart view is opened on a desktop and a phone screen; any JavaScript error, empty view or
  sideways scrolling fails the build, and the server only deploys builds that pass.

### Changed
- Fewer, clearer tabs: Top 50 now sits on the Overview (under Latest signals / Top performers, above Data health);
  Signals and Signal library are one Signals tab (Latest signals | Signal library); Bot and Track record are one
  Trading bots tab (Portfolio | Each bot | Track record | Fade 4h). Old links (#top50, #library, #record) still work.
- Latest signals show each signal's own track record (30-day result vs the average pair, cases, Proven badge) and
  have a Proven only filter.
- Highlights redesigned and moved next to Overview: one header with section chips (Latest, Bee's Shortlist, Channel
  calls, Long-term breakouts, Patterns, Big 4h moves, Trade ideas, Channels, Bots), each with a live count; one section
  shows at a time and has its own link (#highlights/<section>). Latest is a single feed, newest first, grouped by day,
  with Bullish / Bearish, kind and coin filters: every posted highlight, every Bee's Shortlist pick and close, and
  every big 4-hour move. Bee's Shortlist is now a Highlights section (old #shortlist links still work) with live prices.

### Added
- 1-hour charts (1H) for the 30 most liquid pairs and ZIG (a full year for ZIG, in its own file), refreshed every hour; ZIG's 4H chart
  shows 200 days and its daily / weekly charts its complete history.
- 1H / 4H views show that timeframe's own numbers (last candle, 24 hours, 7 days, RSI, MACD, Supertrend, cloud,
  EMA 200, RVOL, when the last candle closed), refreshed with every new candle.
- Tables show the latest price (15-minute refresh) and a live 24h change column instead of the last daily close.
- ZIG/USDT tracked from MEXC like any Binance pair (`venues.py`): daily, 4h and 1h candles, the 24h ticker and the
  listing status come from MEXC's public API, reshaped into Binance rows (paged in explicit windows; trade count
  and taker volume left empty, never invented). Indicators, signals, channels, patterns and the website include it,
  labelled MEXC. The Binance bots never trade it and the Binance audits skip it. Channels and head-and-shoulders
  scans always include it (it is below the $2M a day liquidity bar the other coins must clear).
- Paper trading for the market maker (`mm_paper.py`, service `bees-mm-paper`): the exact live grid / quote code on a
  simulated MEXC account filled from real public trades and the order book (conservative: through-price fills,
  queue at the price, post-only rejections, taker fees). Base / tight / wide spacing run side by side. No API key is
  read; the MEXC client has a public-only mode that refuses every signed call. Ops: `mm-paper-start`,
  `mm-paper-report`, `mm-paper-log`, `mm-paper-stop`.
- Google sign-in that names beesells.xyz on Google's screen: a direct OpenID Connect ID-token request (random
  state, hashed nonce, no Google script, CSP unchanged) and `signInWithIdToken`. On once `google_client_id` is set in
  `web/auth.json`; until then the Supabase redirect is used.
- Accounts (off until `web/auth.json` is filled in): a first-visit welcome with Continue with Google, Create a free
  account, Sign in, or Explore as a guest (remembered); a Sign-in button in the header; **My dashboard** with your own
  coins list (live price, 24h / 7D / 30D, bull score, what's happening, a note per coin), "on my coins today", display
  name, start page, export my data, sign out everywhere and delete account; "Add to my coins" on every chart.
  Supabase Auth (PKCE) + Postgres with row-level security on every table (`supabase/migrations/`); only the public
  key ever reaches the page and the publisher refuses a secret key. Privacy notice and terms pages (contact
  gm@koreit.io). Live project configured in `web/auth.json`. Guide:
  `docs/ACCOUNTS.md`.
- Long-term channel breakout radar (`ltbo.py`): every close above a 250+ day channel gets a dossier, graded on the
  three checks that separated winners in testing, followed for 180 days (weekly close, since / best, line now, width
  target), in the Watchlist and Highlights, posted to the feed and Telegram. The last 180 days are filled in once,
  a few coins per run, the way they would have been seen at the time.
- Channel breaks are now detected against the channel as it stood before the break (refitting on the break candle
  absorbed it).
- Fade 4h (`paper4h.py`): an experimental 4-hour PAPER bot that shorts liquid, shortable coins after a 15%+ 4h
  pump, stop above the pump, out after 3 days; hash-chained ledger; shown on the Bot tab with its backtest
  (`research/bot4h.py`). 4h breakout longs were tested and rejected (lost in both halves).
- Head-and-shoulders scanner v2 (`hs.py`): tops and inverse patterns on the daily chart with two swing sizes,
  statuses near the neckline / broken / retesting / broken earlier, drawn on the chart (shoulders, head, extended
  neckline, target), in the Watchlist and Highlights with each entry's measured record, posted to the feed and Telegram.
- Big 4-hour moves (`moves_4h.py`): pumps / dumps, 30-day highs / lows on volume and big BTC / ETH moves on the
  200 most liquid coins, checked every 15 minutes, with their measured record (`research/moves_4h.py`); shown in
  Highlights and the Overview, and sent to Telegram.
- 1-hour candles (`sync_1h.py`) for every pair: one year of history, then accumulating every 15 minutes,
  audited against the daily candles.
- Channel calls (`chan_calls.py`): a daily channel entry confirmed by proven signals in the same direction and the
  bull score, graded A/B with stops and targets from the channel lines; kept only with a stop within 20% and 1.5+ R:R. Shown in the Watchlist (Channel setups) and
  Highlights; each new call is posted to the highlights feed and to Telegram.
- Channel detector v2: every clean parallel channel up to 7 years back (up to 3 per coin), anchored on real swing
  points from the first touch, with midline tracking (reclaim, retest, loss), bottom bounces and top rejections.
  Charts draw every channel extended left and right; each row shows its status's measured record vs the market.
- Automatic Bee's Shortlist (`auto_picks.py`): once per daily close the engine opens picks by the shortlist rules
  (bull score 7+, a proven bullish signal) and closes them on a stop hit, a proven bearish signal or delisting.
  Every open and close goes into a SHA-256-chained ledger; the head hash is published with the list.
- Bee's Shortlist picks live in the repository (`picks/picks.json`): every pick and close is a public, timestamped
  commit, added only through `tools/pick.py` with entry and stop taken from published data.
- `carry_study.py`: funding-rate carry study on the full funding history, runnable on the server (`carry-study`).
- Operations status shows which keys are present (names only) and the live / market-maker settings.

### Changed
- Channel calls use only the statuses with a measured edge (bottom entries and midline retests long, midline
  lost / top rejection short), graded by that record; midline reclaims, breakouts and breakdowns were dropped.
- Bee's Shortlist rules v3: long AND short ideas whatever BTC does, each tagged with BTC's position vs. its
  200-day at entry (longs: 20 most liquid pairs; shorts: any liquid pair, bull score 3 or less, a proven bearish
  signal). Replaces v2 the same day at the owner's request; the study numbers for each side are on the page.
- Bee's Shortlist rules v2: new picks only while BTC is above its 200-day average and only among the 20 most
  liquid pairs; exits on strong proven bearish signals only. v1 lost money since 2022 in the shortlist study.
- 1h and 4h top-ups are fetched in parallel (an hourly top-up took ~9 minutes).
- Back button: after clicking through to a pair (or another view), "Back" (or the browser's back, or Esc) returns to
  the exact row you clicked, scrolled into view and briefly highlighted.
- The site header shows the latest 15-minute refresh instead of the daily rebuild time.
- Deployment also ships `picks/`; parts missing from older releases are skipped on rollback.

## [1.0.0] - 2026-10-26

### Added
- Engine runs every 15 minutes on a dedicated server; live prices (`prices.json`) on every run.
- Light incremental publishing between daily full publishes (kilobytes per refresh instead of the full package).
- Pull-based continuous deployment gated on green GitHub checks and an on-server test run, with rollback.
- Restricted operations access for the private operations repository.
- Bee's Shortlist: read-only public copy (whitelisted fields only).
- Leading setups: daily head-and-shoulders tops, bottom-accumulation breakouts with a 14-day cluster gauge.
- Parallel channels on daily and 4-hour charts with track records, chart overlay and Telegram alerts.
- Futures data: full funding history since 2019, open interest and long/short ratios.
- Market maker for MEXC spot (grid mode with buy-first rungs).
- Spot execution engine with testnet, shadow and live modes and hard risk limits.
- Test suite (unit and end-to-end scenarios), CI on Python 3.9 and 3.12, CodeQL, gitleaks, private-file guard.

### Fixed
- Funding history download stored only recent settlements; now pages from the first settlement.
- 4-hour and derivatives downloads stall on network outages; consecutive-failure circuit breakers.
