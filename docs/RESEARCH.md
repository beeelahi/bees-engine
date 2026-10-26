# Research log

What was tested, what held up, and what did not. Negative results are kept on purpose: they are why the live
rules look the way they do. Scripts are in `research/`.

## Held up out of sample

* **Volume-confirmed breakouts on the most liquid coins** (Leaders): positive before and after 2022; the edge
  disappears outside the top 10-20 coins by liquidity.
* **Weekly RSI bearish divergence at 26-week highs**: the strongest leading signal in the database.
* **Daily head-and-shoulders tops** (top 300 coins since 2022): 131 trades, 67% winners, profit factor 2.6.
* **Channel breakdowns** (daily): price lower 30 days later in about 7 of 10 cases; few fail back into the channel.
* **Clusters of bottom-base breakouts**: when many deeply-fallen coins break out of tight bases within two weeks,
  the following month has been strong. Few independent episodes, so treated as a breadth gauge.

## Did not hold up

* Mean reversion, pullback entries, bear-market breakdown shorts, wider universes for the breakout bots.
* Tighter stops (1.0 ATR) to raise returns: worse in both periods.
* Inverse head-and-shoulders bottoms since 2022 (all filters).
* Mechanical channel trading (buy the bottom, sell the top): profitable before 2022, losing since.
* 4-hour versions of channels and head-and-shoulders: no reliable edge.
* "Pump" prediction for small caps: the best single filters raise the base rate several-fold but still fail ~98%
  of the time.

## Honest expectations

The highest-risk configuration tested (Turbo: 3% risk per trade, up to 2x exposure) returned roughly 35-40% a
year in the unseen period with drawdowns near 40%. Returns of 60-75% every year were not achievable with any
rule set that survived out-of-sample testing.
