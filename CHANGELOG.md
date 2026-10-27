# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org).

## [Unreleased]

### Added
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
