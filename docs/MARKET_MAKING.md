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
