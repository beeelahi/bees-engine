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

* **Long-term channel breakouts** (`research/ltbo.py`, `research/ltbo_exit.py`; closes above the top of a 250+ day
  channel, 380 cases 2018-2026): up to 2021 almost all ran (median best gain +200% within 180 days, 80% doubled).
  Since 2022 the average one round-tripped (median -13% after 90 days), but three checks known at the breakout close
  separate them: RSI 55-75, a flat or falling channel, a close within 10% of the line. All three (grade A, 113 cases):
  median +9.6% after 90 days, 60% gained 50%+ and 38% doubled within 180 days; none of them (grade C): median -28%.
  A failed first weekly close: median -36%. Volume, 200-day trend, BTC trend and strength vs BTC did not help.

* **Bee's Shortlist rule** (`bin/shortlist_study.py`, full database): only with BTC above its 200-day average and
  the 20 most liquid pairs did it stay positive since 2022 (profit factor about 1.1, also when 'proven' signals were
  chosen with data up to 2021 only). This is the live rule (v2).

* **Channel calls** (`bin/calls_study.py`, every call replayed point in time, 2022 on, signals chosen on data up to
  2021): midline retests +0.22R and bottom entries +0.18R per call, midline-lost shorts +0.10R; overall profit factor
  1.05 (longs) and 1.22 (shorts). Midline reclaims (+0.04R) and breakouts (+0.03R) were dropped, breakdowns (-0.23R) too.

## Did not hold up

* Shortlist v1 (any liquid pair, no market filter, exit on any proven bearish signal): profit factor 0.77 since 2022
  over 11,197 trades. Replaced on 2026-10-27.

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


## Entries and exits of the daily breakout bots (29 Oct 2026, research/entry_study.py)

Question (Bilal): why wait for the next day; would a retracement limit, a stop order during the day, earlier partial
profit or an earlier break-even stop do better? Same simulator as the paper bots, all pairs including delisted ones,
costs included, data to 24 Oct 2026. CAGR and worst drawdown on the unseen period 2022-2026 (design 2018-2021 in brackets).

Timing first: the rule "bought at the next open" means 00:00 UTC, the same moment the signal close is known. The
price was never a day late; only the paper record and the page showed the trade a day late. Fixed with bin/bot_live.py.

| Leaders v3 | CAGR 2022+ | worst DD | (CAGR 2018-21) |
|---|---|---|---|
| now: open after the close, 1/3 at +2R then BE | +21.8% | -24.9% | (+35.3%) |
| 1/3 at +1R then BE | +18.3% | -21.4% | (+30.7%) |
| 1/2 at +1R then BE | +13.9% | -17.5% | (+24.4%) |
| 1/3 at +1.5R then BE | +20.3% | -25.6% | (+31.9%) |
| BE at +1R, no partial | +25.8% | -30.0% | (+45.2%) |
| no partial, no BE | +24.8% | -29.8% | (+53.0%) |
| limit 0.5 ATR under the close, 3 days | +13.0% | -24.5% | (+38.6%) |
| limit 1 ATR under the close, 5 days | +1.2% | -19.3% | (+19.7%) |
| stop order at the 20-day high, during the day | +1.5% | -36.2% | (+33.8%) |

Majors and Turbo show the same pattern (Majors now +12.1% / -19.0%; stop entry +7.4%; limit 0.5 ATR +2.9%. Turbo now
+39.0% / -37.2%; stop entry -4.7%; 1/3 at +1R +27.3% / -30.9%).

Reading: entering during the day on a stop order catches many breakouts that fail by the close (the close and the
volume confirmation are the edge). Waiting for a retracement misses the strongest moves, which rarely come back.
Taking profit earlier raises the win rate (42% -> 55%) and lowers the drawdown, but gives up more return than it saves
in drawdown. Decision: rules unchanged; entries and stops are now shown live, hour by hour.

## Cup & handle, double tops and bottoms (29 Oct 2026, research/patterns_bt.py)
Rules in bin/patterns.py. Liquid pairs (20-day volume >= $2M at the signal), no look-ahead (a swing point counts P candles later; weekly patterns only after the Sunday close), entry at the next daily open in the pattern's direction (top down, bottom and cup up). Horizon 30 days for daily patterns, 90 for weekly. "vs market" = the move minus the average liquid coin's, direction-adjusted. As a trade: stop at the invalid level, measured target, first hit wins, R capped at -1.5.

| Pattern, entry (2022 on) | Cases | Median move its way | Went its way | vs market | Beat market | Target hit | Avg R |
|---|---|---|---|---|---|---|---|
| Daily double top, near neckline | 281 | +8.5% | 65% | -0.2% | 49% | 30% | +0.07 |
| Daily double top, break | 323 | +9.0% | 68% | +0.8% | 53% | 37% | +0.25 |
| Daily double top, retest | 117 | +5.3% | 61% | -0.2% | 50% | 31% | +0.06 |
| Daily double bottom, near neckline | 804 | -5.0% | 43% | +0.8% | 53% | 47% | +0.16 |
| Daily double bottom, break | 1245 | -9.0% | 40% | -1.5% | 46% | 45% | +0.15 |
| Daily double bottom, retest | 324 | -6.1% | 42% | +0.7% | 52% | 40% | +0.07 |
| Daily cup & handle, handle | 304 | 0.0% | 50% | +1.9% | 55% | 19% | +0.16 |
| Daily cup & handle, breakout | 228 | -5.3% | 42% | -0.9% | 46% | 40% | +0.09 |
| Daily cup & handle, retest | 98 | -4.6% | 44% | +3.8% | 57% | 40% | +0.17 |
| Weekly double bottom, near neckline | 125 | -4.0% | 45% | +0.7% | 53% | 52% | +0.23 |
| Weekly double bottom, break | 284 | -17.4% | 34% | -1.7% | 45% | 50% | +0.09 |
| Weekly cup & handle, handle | 23 | +9.8% | 61% | -1.5% | 48% | 26% | +0.49 |
| Weekly cup & handle, breakout | 14 | +22.3% | 71% | -8.2% | 36% | 50% | +0.64 |

Honest read: since 2022 most altcoins fell, so tops "worked" and bottoms mostly did not in absolute terms; against the average coin none of these is a strong edge on its own (beat-market 45-57%). As trades with a stop at the invalid level and the measured target, every daily variant has a small positive average R (+0.06 to +0.25), driven by targets being hit 30-50% of the time. Weekly cups are rare (fewer than 25 cases each): treat their numbers as anecdotes. The cup roundness rule matters: with a looser rule (V shapes allowed) daily cups had about zero average R. Use these as context and levels (where it is wrong, where it targets), not as stand-alone signals.

