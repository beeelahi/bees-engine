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


## MM-Bee on other coins (research/mm_bee_multi.py, ops: bee-study, 29 Oct 2026)

$25,000 position per coin, 25% of it traded ($6,250 in 20 units), sell first, no wallet USDT; Binance 1h candles,
last 365 days; real path and its mirror image (the same moves in the other direction). USDT ahead of holding, and
the rounds' locked-in profit per year as a share of the traded $6,250:

| coin | real v2 | real MM-Bee+ | real Pro | mirror v2 | mirror MM-Bee+ | mirror Pro |
|---|---|---|---|---|---|---|
| MORPHO | +1,590 (50%) | +865 (47%) | -2,499 (12%) | +5,002 (80%) | +4,294 (73%) | +4,794 (80%) |
| ONDO | +915 (15%) | +427 (21%) | +189 (16%) | -3,198 (12%) | +1,366 (64%) | +146 (56%) |
| CAKE | +1,389 (22%) | +516 (15%) | +687 (14%) | +1,348 (37%) | +2,606 (44%) | +2,824 (46%) |
| UNI | +1,162 (35%) | -1,532 (34%) | -1,515 (36%) | +2,653 (42%) | +1,350 (31%) | +995 (25%) |
| JUP | +1,192 (21%) | +982 (31%) | +1,900 (39%) | -1,224 (20%) | +1,305 (36%) | +74 (22%) |
| PENGU | +446 (7%) | +2,240 (38%) | +2,685 (44%) | -9,989 (7%) | -7,804 (24%) | -9,814 (6%) |
| FLOKI | +701 (11%) | +1,440 (24%) | +1,417 (24%) | -12,264 (12%) | -10,406 (23%) | -8,237 (32%) |
| PEPE | +1,475 (27%) | +237 (30%) | +17 (29%) | -62 (24%) | +2,713 (57%) | +4,218 (77%) |
| DOGE | +743 (12%) | +950 (16%) | +887 (15%) | -8,506 (9%) | -3,617 (44%) | -3,133 (49%) |
| TAO | +624 (10%) | +946 (28%) | +1,266 (33%) | -3,752 (4%) | -1,839 (22%) | -375 (38%) |
| SUI | +689 (11%) | +1,250 (23%) | +1,329 (24%) | -10,019 (10%) | -7,242 (26%) | -8,580 (16%) |
| APT | +786 (13%) | +1,156 (20%) | +1,154 (19%) | -25,556 (12%) | -24,092 (17%) | -24,161 (15%) |
| SOL | +956 (15%) | +317 (11%) | +309 (10%) | -4,485 (5%) | -1,781 (19%) | -1,510 (20%) |
| ETH | +659 (11%) | +300 (9%) | +187 (9%) | -2,674 (12%) | +1,001 (37%) | +1,778 (40%) |
| BTC | +257 (12%) | -27 (3%) | -68 (2%) | +512 (16%) | +450 (10%) | +588 (10%) |

Reading:
* The locked-in profit is real on every coin (2-80% a year of the traded slice), but "ahead of holding" depends on
  direction: selling 25% of a coin into a big rally (mirror paths of APT, SUI, FLOKI, PENGU, DOGE: +144% to +383%)
  costs far more than the rounds earn. The market maker is income on a position, not a hedge.
* No single setting wins everywhere: Pro was tuned on ZIG and is weak on MORPHO and UNI; plain v2 is the steadiest
  on the real (mostly falling) year. Each coin needs its own choice, ideally from a longer history including a bull
  run. Best candidates on both paths here: MORPHO, CAKE (any setting), ONDO and JUP (MM-Bee+), PEPE and ETH
  (Pro or MM-Bee+), BTC (v2).

## New positions: upside stop ("chase") and trend widening (research/mm_bee_tune.py, ops: bee-tune, 29 Oct 2026)

Same 15 coins and sizing as above ($25K, 25% traded, 20 units, sell first, no wallet USDT), real year and mirror
year, 18 settings: the three bots, each with chase off / 8% / 15% (a sold unit buys back at the market once the price
is that far above its sell; taker fee 0.1%) and trend widening off / on (sell ladder twice as far and wide while the
4h close is 3%+ above its 30-candle EMA). USDT ahead of holding, summed over the 15 coins:

| setting | real (falling) year | mirror (rising) year | coins ahead on both |
|---|---|---|---|
| Pro + chase 8% | +3,778 | +15,739 | 9 / 15 |
| Pro + chase 15% | +6,280 | +5,888 | 11 / 15 |
| v2 + chase 8% | +4,369 | +6,864 | 9 / 15 |
| Pro | +8,705 | -41,578 | 3 / 15 |
| MM-Bee+ | +10,805 | -42,636 | 6 / 15 |
| v2 | +13,946 | -73,243 | 4 / 15 |

Reading:
* The upside stop is the fix that matters: it gives up a few thousand USDT in a falling year and saves tens of
  thousands in a rally (Pro: -41.6K -> +15.7K on the rising path). Trend widening alone barely helps.
* One setting for all new positions: Pro + chase 8% (best worst case; strongest in a rally, which is the bet when
  buying after a crash) or Pro + chase 15% (more coins ahead on both paths, better in a falling year).
* Per coin, the best setting was ahead on both paths for all 15 coins (+12.7K falling, +24.0K rising in total), but
  that is picked in-sample from 18 options: expect less live. Weak fits (little to earn): APT, ETH, SOL, BTC.
  Stronger fits: MORPHO, ONDO, CAKE, UNI, PEPE, JUP, PENGU, TAO.
* ZIG is different: on ZIG the upside stop lowered Pro on both paths (real +40.2K -> +17.2K, mirror +66.6K -> +41.8K
  with chase 8%), so ZIG stays on Pro without it.

### At 40% of the position ($10,000 a coin, 30 Oct 2026)

Same study, rerun with 40% of each $25,000 position market-made (and the upside stop held to the same USDT budget
as every buy). USDT ahead of holding, 15 coins together:

| setting | falling year | rising year | coins ahead on both |
|---|---|---|---|
| Pro + buy back at +8% | +8,679 | +22,289 | 11 / 15 |
| Pro + buy back at +15% | +12,287 | +4,596 | 10 / 15 |
| Pro | +17,070 | -80,032 | 3 / 15 |
| MM-Bee+ | +20,533 | -82,582 | 7 / 15 |
| v2 | +23,771 | -132,066 | 4 / 15 |

Pro + buy back at +8% stays the choice: the only setting clearly ahead in both directions. Selling 40% without the
upside stop would have cost up to $132K across the 15 coins in a rally.
