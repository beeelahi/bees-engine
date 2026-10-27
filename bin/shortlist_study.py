"""
Track record of the automatic Bee's Shortlist rules (auto_picks.py) on the full history in the database (read-only).
Run on the server: `python3 bin/shortlist_study.py` (ops command `shortlist-study`).

Entry exactly as live: daily bull score >= 7, a proven bullish signal on the daily chart in the last 3 days or the
latest closed weekly candle, up to 25 new picks a day, not already held; entry at the day's close; stop = entry -
2 x ATR(14). Exit variants compared:
  live         stop, or any proven bearish signal after the entry day (exit at that close), or delisting
  strong_exit  stop, or a proven bearish signal whose 30-day edge is at least 2% (a strong one)
  stop_30d     stop, or 30 days held (no signal exits)
  stop_60d     stop, or 60 days held
'Proven' is decided twice: with the full history (what the site uses; slightly optimistic for the past), and with
data up to 2021 only, applied to 2022 on (a clean out-of-sample test). Universe: pairs with 20-day average volume of
$2M+ on the entry day. Costs 0.2% round trip.
"""
import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import date, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(ROOT, "data", "binance.db")
COST = 0.002


def proven_sets(con, early_only):
    bull, bear, strong = set(), set(), set()
    for sig, tf, d, n, e, e1, e2, n1, n2 in con.execute(
            "SELECT signal, tf, direction, n, edge_30d, edge_30d_early, edge_30d_late, n_early, n_late FROM signal_stats WHERE variant='all'"):
        if tf not in ("1d", "1w") or None in (e, e1, e2):
            continue
        if early_only:
            ok = (n1 or 0) >= 100 and ((d == "bullish" and e1 > 0) or (d == "bearish" and e1 < 0))
            edge = e1
        else:
            ok = (n or 0) >= 100 and (n1 or 0) >= 30 and (n2 or 0) >= 30 and (e > 0 and e1 > 0 and e2 > 0 or e < 0 and e1 < 0 and e2 < 0)
            ok = ok and ((d == "bullish" and e > 0) or (d == "bearish" and e < 0))
            edge = e
        if ok:
            (bull if d == "bullish" else bear).add((tf, sig))
            if d == "bearish" and abs(edge) >= 2:
                strong.add((tf, sig))
    return bull, bear, strong


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    print("Loading daily candles, indicators and signals ...", flush=True)
    px = defaultdict(dict)
    for s, d, lo, c, v in con.execute("SELECT symbol, date, low, close, volume_usdt FROM daily ORDER BY symbol, date"):
        px[s][d] = (lo, c, v or 0.0)
    adv = {}
    for s, m in px.items():
        ds = sorted(m)
        run = 0.0
        for i, d in enumerate(ds):
            run += m[d][2]
            if i >= 20:
                run -= m[ds[i - 20]][2]
            adv[(s, d)] = run / min(i + 1, 20)
    ind = {(s, d): (bs, atr) for s, d, bs, atr in con.execute(
        "SELECT symbol, period_start, bull_score, atr14_pct FROM indicators WHERE tf='1d' AND complete=1")}
    days = sorted({d for m in px.values() for d in m})
    split = "2022-01-01"
    for label, early_only in (("proven = full history (as the site)", False), ("proven = up to 2021 only, tested 2022 on", True)):
        bull, bear, strong = proven_sets(con, early_only)
        want = bull | bear
        sigs = defaultdict(list)      # date KNOWN -> [(symbol, tf, signal, direction)]; a weekly signal is known after
        for s, tf, ps, sig, d in con.execute(                       # its week closes (start + 6 days, Sunday)
                "SELECT symbol, tf, period_start, signal, direction FROM signals WHERE warm=1 AND provisional=0 AND tf IN ('1d','1w')"):
            if (tf, sig) in want:
                known = ps if tf == "1d" else (date.fromisoformat(ps) + timedelta(days=6)).isoformat()
                sigs[known].append((s, tf, sig, d))
        print(f"\n== {label}: {len(bull)} bullish and {len(bear)} bearish proven signals ({len(strong)} strong bearish)")
        res = {v: [] for v in ("live", "strong_exit", "stop_30d", "stop_60d")}
        held = {v: {} for v in res}
        last_week = ""
        for di, d in enumerate(days):
            if early_only and d < split:
                continue
            todays = sigs.get(d, [])
            wk = [x for x in todays if x[1] == "1w"]
            if wk:
                last_week = d
            # exits first
            for v, H in held.items():
                for s, pos in list(H.items()):
                    bar = px[s].get(d)
                    if bar is None:
                        if d > pos["last"]:
                            continue
                    else:
                        pos["last"] = d
                    out = None
                    if bar and bar[0] < pos["stop"]:
                        out = pos["stop"]
                    elif bar and v in ("live", "strong_exit") and any(x[0] == s and x[3] == "bearish" and (x[1], x[2]) in (bear if v == "live" else strong) for x in todays):
                        out = bar[1]
                    elif bar and v in ("stop_30d", "stop_60d") and pos["n"] >= (30 if v == "stop_30d" else 60):
                        out = bar[1]
                    if bar:
                        pos["n"] += 1
                    if out:
                        res[v].append((pos["d"], out / pos["entry"] - 1 - COST, pos["n"]))
                        del H[s]
            # entries: same candidate rule as auto_picks.py
            window = set(days[max(0, di - 2):di + 1])
            cands = {}
            for dd in window:
                for s, tf, sig, dr in sigs.get(dd, []):
                    if dr == "bullish" and tf == "1d" and (tf, sig) in bull:
                        cands.setdefault(s, set()).add(tf + sig)
            for s, tf, sig, dr in sigs.get(last_week, []) if last_week else []:
                if dr == "bullish" and tf == "1w" and (tf, sig) in bull and last_week > (date.fromisoformat(d) - timedelta(days=7)).isoformat():
                    cands.setdefault(s, set()).add(tf + sig)        # the latest closed week only (live: until the next one)
            pick = []
            for s, why in cands.items():
                bs, atr = ind.get((s, d), (None, None))
                if bs is None or bs < 7 or not atr or s not in px or d not in px[s] or adv.get((s, d), 0) < 2e6:
                    continue
                pick.append((-len(why), -bs, s, atr))
            pick.sort()
            for v, H in held.items():
                for _, _, s, atr in pick[:25]:
                    if s in H:
                        continue
                    c = px[s][d][1]
                    H[s] = {"d": d, "entry": c, "stop": c * (1 - 2 * atr / 100), "n": 0, "last": d}
        for v, L in res.items():
            for per, f in (("up to 2021", lambda x: x[0] < split), ("2022 on", lambda x: x[0] >= split)):
                T = [x for x in L if f(x)]
                if len(T) < 20:
                    continue
                r = [x[1] for x in T]
                print(f"  {v:12s} {per:10s} trades {len(T):5d}  win {100 * sum(x > 0 for x in r) / len(r):3.0f}%  "
                      f"avg {100 * st.mean(r):+6.2f}%  median {100 * st.median(r):+6.2f}%  avg hold {st.mean(x[2] for x in T):5.1f}d  "
                      f"PF {sum(x for x in r if x > 0) / max(1e-9, -sum(x for x in r if x < 0)):.2f}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
