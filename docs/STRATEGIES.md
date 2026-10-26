# Strategies and evidence

Every number on the site comes from the same code that produces it daily. Two rules apply everywhere:

* **Out-of-sample split.** Rules were designed on data up to 2021; 2022 onward is the "unseen" period. A result
  only counts if it holds in the unseen period.
* **Costs and survivorship.** Backtests charge 0.1% fees per side plus liquidity-based slippage, and include coins
  that were later delisted.

## Paper bots ($100,000 paper portfolio)

| Bot | Status | Capital | Idea |
|---|---|---|---|
| Leaders v3 | proven | $40,000 | Volume-confirmed 20-day breakouts on the 10 most liquid coins, only while BTC is above its 200-day average; tight ATR stop, one-third profit at +2R, trailing exit. |
| Majors | proven | $30,000 | The same breakout rules on BTC and ETH only, larger positions. |
| Accumulation | experimental | $15,000 | Small and mid caps where volume builds near the top of a contained range, bought before the breakout. |
| Turbo | experimental | $15,000 | Leaders rules with 3x the risk per trade, at most 4 positions and up to 2x exposure. |

The bots are replayed from their inception date on every run; each trade is written once to an append-only log
and every later run must reproduce it exactly.

## Leading setups (Highlights)

| Setup | Rule (daily unless stated) | Evidence, 2022 on |
|---|---|---|
| Possible major top (weekly) | Weekly RSI bearish divergence at a 26-week high, the earlier peak overbought | Strongest: price lower 30 days later in most cases |
| Possible top / bottom | Daily RSI divergence at a 90-day extreme | Tops held up; bottoms did not |
| Head-and-shoulders top | Completed pattern, first close below the neckline | Price lower 30 days later in about two-thirds of cases (4h: no edge) |
| Bottom accumulation breakout | 80%+ below the 2-year high, tight 60-day base, volume breakout | Alone: close to a coin flip. When 5+ fire within 14 days: strong (market-breadth signal) |
| Channels (daily + 4h) | Every clean parallel channel up to 7 years back, up to 3 per coin: starts at its first touch, 2+ touches per line, 5+ in all; statuses at the edges, the midline (reclaim, retest, loss) and breaks | Since 2022 no status beats the average coin by much; best: long-term channel bottoms and midline retests in narrow channels (a few % better than the market, ~60% of the time). Use as the map; act when a proven signal agrees. 4h: no edge |

The live track record of each setup is recomputed from full history on every run and shown next to it.

## Live execution (`bin/live_engine.py`)

Spot only, one bot at a time, modes `off` / `testnet` / `shadow` / `live`. Hard limits in code: total capital cap,
maximum order size, daily loss limit, maximum drawdown, price-drift guard at entry. Protective orders are placed
on the exchange (OCO take-profit plus stop-limit), so positions stay protected if the server is down. Kill
switches: `STOP_TRADING` (no new entries) and `FLATTEN_NOW` (close everything the bot opened, never the
owner's other holdings).
