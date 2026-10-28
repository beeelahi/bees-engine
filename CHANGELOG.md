# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org).

## [Unreleased]

### Added
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
