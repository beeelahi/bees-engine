# Operations runbook

## Day to day

Nothing to do: the engine runs every 15 minutes, deploys itself from `main`, and reports to Telegram
(deployments, bot actions, setups, problems).

## Operations workflows (private `bees-ops` repository)

Actions → **Operate** → choose a command:

| Command | Effect |
|---|---|
| `status` | Deployed version, timers, services, last runs, site publish status, disk, active switches. |
| `health` | Fails (and notifies by email) if no good engine run for an hour. Also runs automatically every 30 minutes. |
| `logs` / `deploy-log` / `mm-log` | Last N lines (secrets scrubbed). |
| `run-sync` | Start an engine run now. |
| `deploy` | Check for a new release now (still requires green checks and passing tests). |
| `rollback` | Switch back to the previous release. |
| `mm-start` / `mm-stop` | Market maker on / pause (pause cancels its own orders; it never market-sells). |
| `trading-stop` / `trading-resume` | Live engine: block / allow new entries. |
| `carry-study` | Run the funding-carry study on the server's full funding history. |

The ops key can run only these commands (forced command, no shell).

## Picks (Bee's Shortlist)

Picks are public commits: `python3 tools/pick.py add SYMBOL --data <site data>` or `... close SYMBOL --reason "Stop hit"`,
committed as `picks: add SYMBOL` / `picks: close SYMBOL`. The server publishes them after the checks pass.

## Incidents

| Symptom | First step |
|---|---|
| Site data stale | `status`: is the timer active, did the last run fail? `logs 200` for the error. |
| Deployment not happening | `deploy-log`: checks still pending on GitHub, or tests failed on the server (the commit is skipped until a fix lands). |
| Bad release in production | `rollback`, then fix forward on `main`. |
| Exchange problem / unexpected trades | `trading-stop` and `mm-stop` immediately; investigate with `logs`. |
| Server unreachable | Hetzner console → server → Console (browser), log in as `bees` with the SSH key holder's help, or reboot from the console. |

## Secrets

Entered once on the server by the owner (Hetzner browser console or SSH), with hidden-input setup scripts, stored
as private files (mode 600), never in GitHub:

| File | Created by | Purpose |
|---|---|---|
| `.github_token` | owner (fine-grained token: contents write on the site repository only) | Publishing the website |
| `.telegram` | `python3 bin/alerts.py --setup` | Alerts |
| `.binance_testnet`, `.binance_live` | `python3 bin/live_setup.py testnet` / `live` | Execution engine (trading on, withdrawals off, IP-restricted) |
| `.mexc_live` | `python3 bin/mm_setup.py` | Market maker (trading on, withdrawals off, IP-restricted) |
