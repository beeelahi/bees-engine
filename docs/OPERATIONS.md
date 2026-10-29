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

## Market making with real money: go-live runbook

Everything below is done by Bilal on the server unless it says ops. Nothing trades until step 5.

1. **Key.** On MEXC create an API key with spot trading ON, withdrawals OFF, bound to the server's IP
   (89.167.60.181). On the server: `python3 ~/BinanceData/bin/mm_setup.py` (hidden input; it refuses a key that can
   withdraw). Never paste a key anywhere else.
2. **Coins.** Fund the MEXC spot wallet with each position (about $25,000 of each coin; 40% of it, $10,000, is
   market-made) and the 300,000 ZIG for the ZIG ladder. No USDT is needed: buys are paid from what the sells raise.
3. **Plan and check.** ops `mm-plan` prints the per-coin plan from today's prices and writes `mm.json.proposed`.
   On the server, `python3 ~/BinanceData/bin/mm_plan.py --apply` copies it into `mm.json` after a typed APPLY
   (the old file is backed up; trading stays off). Then ops `mm-preflight` must say **GO**: key can trade and cannot
   withdraw, clock in sync, every coin listed and tradable, the book tight, the wallet holds each ladder's coins,
   every ladder under the ceilings ($15,000 a coin, $120,000 in all), no stray bot orders.
4. **Prove the order path (with Bilal's go).** On the server: `python3 ~/BinanceData/bin/mm_preflight.py --probe`
   places two tiny post-only orders that cannot trade (one at twice the price, placed and cancelled; one at the
   best bid, which MEXC must refuse or cancel untraded) and reports how MEXC answered.
5. **Switch on.** Set `"enabled": true` in `mm.json` and ops `mm-start`. Telegram (when set up) says "Market maker
   started"; ops `mm-status` shows the heartbeat, each coin's state and its units.
6. **Watch.** ops `mm-status` / `mm-log` at least daily for the first two weeks; ops `health` fails when the
   market maker runs but has had no heartbeat for 5 minutes.
7. **Stop.** ops `mm-stop` sets MM_STOP: the bot cancels its own orders and waits (it never market-sells).
   `mm-start` resumes from where it stopped (state is in `data/mm.db`).

What protects the money (bin/mm_bee.py, bin/mm_bot.py): every order is written down before it is sent and checked
with the exchange when a reply is lost (never sent twice); an order the exchange no longer knows is set aside and
reported ("unknown", fix by hand); orphan bot orders are cancelled at start; no action while the book is 15%+ off
the last 1h close; a 25%+ move in an hour pulls the sells for 2 hours; at most 6 upside-stop buy-backs per coin per
day; a buy that may take liquidity keeps 0.2% back for the fee; one coin's errors never stop the others (5 in a row:
10-minute pause and an alert); the bot refuses to start on a key that can withdraw.

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
