# Research log

What was tested, what held up, and what did not. Negative results are kept on purpose: they are why the live
rules look the way they do. Scripts are in `research/`.

## Held up out of sample

* **Volume-confirmed breakouts on the most liquid coins** (Leaders): positive before and after 2022; the edge
  disappears outside the top 10-20 coins by liquidity.
* **Weekly RSI bearish divergence at 26-week highs**: the strongest leading signal in the database.
* **Daily head-and-shoulders tops** (top 300 coins since 2022): 131 trades, 67% winners, profit factor 2.6.
* **Channel breakdowns** (daily): price lower 30 days later in about 2 of 3 cases since 2022 (7 of 10 on channels with
  6+ touches). Most of that is the falling altcoin market: against the average coin, breakdowns are not a short signal.
* **Clusters of bottom-base breakouts**: when many deeply-fallen coins break out of tight bases within two weeks,
  the following month has been strong. Few independent episodes, so treated as a breadth gauge.

* **Head-and-shoulders tops as shorts** (`research/hs_bt.py`, two swing sizes, 2022 on): near the neckline +0.47R
  average (68% moved down in 30 days, 138 cases), at the break +0.3R (70%, 305 cases). Mostly the falling market:
  against the average coin they are about even.

## Did not hold up

* Neckline **retests** as a better entry: since 2022, top retests -0.02R (83 cases), inverse retests -0.13R (170 cases,
  29% went up). Inverse head-and-shoulders bottoms lost money at every entry since 2022 (break: -0.2R, 34% up).

* Mean reversion, pullback entries, bear-market breakdown shorts, wider universes for the breakout bots.
* Tighter stops (1.0 ATR) to raise returns: worse in both periods.
* Inverse head-and-shoulders bottoms since 2022 (all filters).
* Mechanical channel trading (buy the bottom, sell the top): profitable before 2022, losing since.
* Channel statuses as stand-alone trades (detector v2, 27,500 events 2018-2026, `research/chan_bt.py`): since 2022,
  no status beats the average coin by much. Best: near the bottom of 365+ day channels (+2.3% vs the average coin
  over 30 days, 59% of cases) and midline retests in narrow channels (+4.4%, 62%, 203 cases). Coins near the top of
  long-term channels kept beating the market (+4.6%), so the top of a long channel is not a sell on its own.
* 4-hour versions of channels and head-and-shoulders: no reliable edge.
* "Pump" prediction for small caps: the best single filters raise the base rate several-fold but still fail ~98%
  of the time.

## Honest expectations

The highest-risk configuration tested (Turbo: 3% risk per trade, up to 2x exposure) returned roughly 35-40% a
year in the unseen period with drawdowns near 40%. Returns of 60-75% every year were not achievable with any
rule set that survived out-of-sample testing.
