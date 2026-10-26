# Bee's Shortlist (public picks)

`picks.json` is the public record of Bee's own picks. Every change is a commit, so each pick's entry, stop and
timestamp are verifiable and cannot be quietly edited later. The server publishes it to beesells.xyz within minutes
of the commit passing its checks.

* **Entry** is the latest closed daily candle at the time of the pick (UTC); **stop** = entry - 2 x ATR(14, daily),
  fixed at approval. Picks are added and closed with `tools/pick.py`, never by hand.
* Only these fields are published: symbol, status, entry, entry_date, stop, risk_pct, approved_at, exit, exit_date,
  closed_at, close_reason.
* Not advice: these are one trader's positions, shown with their full record, losers included.
