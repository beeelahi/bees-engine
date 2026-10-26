# Architecture

## Runtime

One server (Hetzner Cloud, Ubuntu 24.04, EU region) runs three systemd units under the unprivileged user `bees`:

| Unit | Schedule | Job |
|---|---|---|
| `bees-sync.timer` → `bees-sync.service` | minute 3, 18, 33, 48 of every hour | The engine run (below). One run at a time (file lock); a long run delays the next instead of overlapping. |
| `bees-deploy.timer` → `bees-deploy.service` | every 5 minutes | Continuous deployment (see [Delivery](#delivery)). |
| `bees-mm.service` | always on | Market maker. Idles unless the private `mm.json` enables it. |

All services run with `NoNewPrivileges`, a read-only file system except the engine's own directory, private
`/tmp`, and no inbound network: the firewall allows SSH only (keys only, no passwords, no root login, fail2ban).

## The engine run (`bin/binance_sync.py`)

Every 15 minutes:

1. **Universe**: exchange info and 24h tickers; USDT spot pairs, stablecoin bases excluded, $100k 24h volume floor.
2. **Daily candles**: incremental download of closed daily candles for every pair, with gap repair and a weekly
   random re-download audit. Delisted pairs are kept so research has no survivorship bias.
3. **Analytics rebuild** (only when a new daily candle closed, or the engine version changed): indicators on
   daily, weekly and monthly bars, signals, signal statistics, leading setups, channels, the paper bots (replayed
   from inception and integrity-checked), and the web export. Followed by a **full publish**.
4. **4-hour candles**: incremental, audited against the daily candles (they must aggregate exactly).
5. **Derivatives**: funding history since 2019, daily open interest and long/short ratios for the 150 most
   liquid perpetuals.
6. **Market-making research**: order-book snapshots and candle history for the configured MEXC coins.
7. **Live prices**: one call to the public 24h ticker; `prices.json`.
8. **Light publish**: only files that changed are committed on top of the last full publish (kilobytes, not
   the 60 MB data package). The next full publish squashes these commits, so the site repository never grows.
9. **Live execution engine** (testnet, shadow or live per the private `live.json`).
10. **Alerts** to Telegram: bot actions, market-filter changes, setups, channel breaks, engine problems.

Every step after the core sync is isolated: a failure is logged and alerted, and never blocks the others.
Network outages trip circuit breakers (consecutive-failure limits) instead of stalling the run.

## Data

SQLite in WAL mode (`data/binance.db`, ~3 GB). See [DATA.md](DATA.md). The website is a static export: JSON
shards per 18 pairs, so a pair chart loads ~2 MB instead of everything.

## Delivery

```
push to main ──► GitHub checks (tests 3.9/3.12, lint, shellcheck, page syntax, systemd units, CodeQL, gitleaks,
                 private-file guard)
                       │ all green
server (every 5 min) ──► git fetch ──► unpack release ──► run the test suite on the server
                       │ pass
                       ──► wait for the engine lock ──► swap bin/ web/ deploy/ (previous kept) ──► install changed
                           systemd units ──► restart the market maker if running ──► Telegram: "Engine updated"
```

* The server never receives pushes and GitHub holds no server credential.
* A commit that fails its tests on the server is marked and never retried.
* `pull_deploy.sh --rollback` returns to the previous release; the last five releases are kept.

## Operations

The private `bees-ops` repository holds the operations workflows (status, logs, health, deploy, rollback, start or
stop the market maker, pause trading). They connect as `opsbot`, whose SSH key is restricted with a forced command
to the fixed list in `deploy/ops.sh`: it cannot open a shell, forward ports or run anything else. Outputs stay in
the private repository. See [OPERATIONS.md](OPERATIONS.md).

## Website

`web/index.html` is a single page with no build step. `publish_site.py` wraps it into a full document with a
strict Content-Security-Policy (inline script pinned by hash, the chart library pinned with Subresource
Integrity, no third-party connections except an optional Ask relay) and publishes to GitHub Pages.
