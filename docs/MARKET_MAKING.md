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
