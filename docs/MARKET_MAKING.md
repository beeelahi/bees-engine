# Market making (MEXC spot)

`bin/mm_bot.py` runs as its own service. The coin, sizes and limits live in the private `mm.json` on the server.

## Mechanics (grid mode, `bin/mm_grid_live.py`)

* **Sell ladder**: fixed-size sell orders spaced a fixed percentage apart above an anchor price. Each filled sell
  places a buy-back one step lower; when that fills, the profit is booked in USDT and the level re-arms.
* **Buy-first rungs**: the mirror image below the anchor, funded from USDT, each resold one step higher.
* **Hard limits**: an inventory floor (coins out never exceed start minus floor), a USDT budget (buy-backs owed by
  the sell ladder are always reserved first), a price band outside which no new orders are placed, and a cap on
  resting orders.
* **Post-only**: every quote is `LIMIT_MAKER`, so the bot never takes liquidity and can never trade with itself.
  Buy-backs fall back to a plain limit order only at or below the planned price.
* **Safety**: client order ids carry a fixed prefix, so the bot only ever touches its own orders; `MM_STOP`
  cancels them and pauses without selling anything; API keys that can withdraw are refused.

## Evidence

The exact live code was run against a simulated exchange over a full year of 5-minute candles of the target
coin, with conservative fills (price must trade 0.1% through an order). 1.5% spacing with buy-first rungs gave
the best result among 1.0-2.0% spacings, beat simply holding the same inventory by a wide margin, and never
breached the inventory floor. It does not protect against a falling coin price, and in strong rallies it sells
inventory early. Thin order books mean real fills will be slower than simulated; live results are the judge.

## Paper trading (`bin/mm_paper.py`, service `bees-mm-paper`)

Runs the exact live code (`mm_bot.MM` and `mm_grid_live.GridLive`) against a simulated account filled from MEXC's
real public trades and order book. It reads no API key (the client is public-only and refuses signed calls), so it
cannot place a real order. Settings come from the private `mm.json`; its `enabled` flag is ignored.

Three variants run side by side on the same data, each with its own wallet: **base** (as configured), **tight**
(grid step x 2/3) and **wide** (x 4/3), so live markets decide the spacing before real money does.

Fill model, deliberately conservative: an order fills in full only when a trade prints through its price (or the
opposite best price crosses it); at exactly its price it first waits behind the size already resting there; trades
printed before the order was placed never count; post-only orders that would cross are rejected; plain limits that
cross pay the taker fee. Fees default to MEXC standard spot (maker 0%, taker 0.05%), overridable with `paper_fees`.

Ops commands: `mm-paper-start`, `mm-paper-report` (value vs just holding, completed rounds, grid profit, fills,
fees, holdings, open orders per variant), `mm-paper-log`, `mm-paper-stop`. Pause without stopping: `MM_PAPER_STOP`.


## MM-Bee (sell-first ladder)

Rules and settings: see the header of `bin/mm_bee.py` (defaults in `mm_bee.DEFAULTS`; override per coin in mm.json with a `"bee"` block, or `"bee": false` to switch the paper run off). Interpretation choices, for Bilal to confirm before going live:

* the 2% gap is 2% of the base (his worked example: close 0.05 -> 0.0515, 0.0525, 0.0535); `"gap_from": "prev"` makes it 2% of the previous sell;
* "revise the base" uses the latest completed hourly close;
* "use more ZIG" opens one more ladder of 300,000 ZIG (`extra_ladders`: 1) when every unit is sold and 12 hours pass without a fill;
* a first-ladder sell the market is already above is not chased: it rests once the price is back under it.

Paper: variant `bee` in bees-mm-paper (wallet 600,000 ZIG + 10,000 USDT). Backtest: `bin/mm_bee_backtest.py`. Website: the MM-Bee tab.


## MM-Bee v2, MM-Bee+ and MM-Bee Pro (29 Oct 2026)

Three versions run side by side on paper (bees-mm-paper variants `bee`, `bee_plus`, `bee_pro`) and in the hourly
backtest (5 days, 15 days, 1 year), all on the MM-Bee tab:

| | rules |
|---|---|
| v2 (`bee`) | Bilal's rules: +3% first, every 2% of the base, buy back -3.1%, resell +3%; base follows each 4h close (5 nearest orders first, the rest 150 s later, one order at a time 2-4 s apart, moves under 0.3% skipped); BTC guard; 1h fast mode on 10%+ moves |
| MM-Bee+ (`bee_plus`) | v2 + recycling (bought-back units rejoin the sell ladder), no fast mode |
| MM-Bee Pro (`bee_pro`) | MM-Bee+ with +4% first, every 1%, buy back -4.5%, resell +4.5% |

How Pro was chosen (research/mm_bee_study2-5.py, ZIG 1h candles Nov 2025 - Oct 2026, ahead of holding in USDT):

| | real year (ZIG -51%) | mirror (same moves, up) | flat (trend removed) | Nov-Jun | Jul-Oct |
|---|---|---|---|---|---|
| v2 | +12,758 | -21,836 | +28,615 | +11,360 | +8,831 |
| MM-Bee+ | +20,175 | +42,942 | +17,412 | +12,831 | +5,884 |
| Pro | +31,249 | +65,292 | +17,516 | +21,028 | +9,145 |

* v2 without recycling sells everything into a rally (mirror path) and its bought-back units get stuck far above a
  falling price (real path: activity dies after a few months).
* Recycling sells a bought-back unit wherever the ladder puts it, which can be under that unit's buy price; the
  wallet still came out ahead on every path.
* Robustness: the 81 settings around Pro (first 3.5-5%, gap 0.75-1.25%, buy-back 4-5.5%, resell 4-5.5%;
  `mm_bee_study4.py --near-pro`) were ALL ahead of holding on all three paths (medians +29,404 / +63,575 / +15,707),
  so Pro sits on a broad plateau, not on a lucky spike. It is kept as is rather than tuned further.
* Regime switching (recycling off in flat markets, on in trends; 4h EMA 30-90 candles, bands 3-8%) was tested and
  was not consistently better, so it is off (`regimes` option in mm_bee.py).
* The BTC guard fired twice in the 30 days with hourly BTC data; it changed nothing in those backtests; it is kept
  as insurance. The fast mode slightly hurt; it is on only in v2 (Bilal's rule).
* Backtest fills are conservative (only on a later candle that trades through the price). Live market making on
  the real book will differ: queue position, partial fills, and other traders reacting to the orders.
