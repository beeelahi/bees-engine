# Data dictionary

The engine keeps one SQLite database (`data/binance.db`, WAL mode) on the server. It is never committed; this page
documents its shape. Research scripts work from read-only exports of it (see `research/_env.py`).

## Market data

| Table | Key | What it holds |
|---|---|---|
| `pairs` | symbol | Every USDT spot pair ever seen: base token, status, first/last seen, delisting date, backfill state. |
| `daily` | symbol, date | Closed daily candles since listing: OHLC, base and USDT volume, trades, taker-buy volume, open/close times. Delisted pairs are kept. |
| `candles` | symbol, tf, period_start | Weekly and monthly bars aggregated from `daily` (with a completeness flag). |
| `candles_4h` | symbol, open_ms | 4-hour candles, 3 years, for pairs with real volume. |
| `h4_state`, `h4_audit`, `h4_anomalies` | | 4h download progress; daily re-aggregation check (4h must sum to the daily candle exactly). |
| `ticker_snapshots` | snap_date, symbol | Daily 24h ticker snapshot (price, quote volume). |
| `gaps`, `anomalies`, `series_info` | | Confirmed missing days, data anomalies, series restarts after long trading halts. |
| `supply`, `fear_greed` | | Circulating supply (for estimated market cap) and the Crypto Fear & Greed index. |

## Derivatives (Binance USDT-M perpetuals, public)

| Table | Key | What it holds |
|---|---|---|
| `funding` | symbol, time_ms | Every 8-hour funding settlement since the contract listed (rate, mark price). |
| `oi_daily` | symbol, date | Daily open interest (contracts and USDT). Binance keeps 30 days; the database keeps everything since collection began. |
| `ls_daily` | symbol, date | Daily global long/short account ratio. Same retention note. |
| `deriv_state` | symbol | Download progress (full-history flag, last settlement). |

## Analytics

| Table | What it holds |
|---|---|
| `indicators` | 50+ indicator columns per pair, timeframe and bar, with a warm-up flag (values are only used once enough history exists). |
| `dictionary` | Every indicator column: family, label, description, warm-up length per timeframe. |
| `signals` | Signal events (crosses, breakouts, divergences, ...): direction, volume confirmation, provisional flag for unclosed bars. |
| `signal_stats` | Each signal's forward returns and edge vs. the average pair at 7/30/90 days, split up to 2021 / 2022 on. |
| `forward` | Forward returns and max gain / drawdown per pair-day (the baseline every signal is measured against). |
| `latest` | One row per pair: latest close, returns, liquidity, universe flag, and the latest value of every indicator. |
| `watch_hits`, `watch_stats` | The volume-surge watchlist rule and its record vs. baseline. |
| `highlight_log` | Append-only log of everything the Highlights page ever showed (first-seen timestamps). |

## Bots

| Table | What it holds |
|---|---|
| `bot_trades_log` | Append-only record of every paper trade the first time it appeared; every run re-derives the account and must match it (integrity check). |
| `bot_runs` | One row per run: equity, open positions, integrity result. |

## Private, separate databases

`data/live.db` (live execution engine positions and orders) and `data/mm.db` (market-maker fills and grid state)
hold account data and stay on the server.
