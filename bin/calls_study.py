"""
Track record of the FIRST channel-call rules (v1, until 31 Oct 2026; the live v2 rules were chosen with research/calls_lab.py) on the server's full history (read-only). Long-running: start it
with the ops command `calls-study` (runs in the background) and read the result with `study-log`.

Point in time: channels are re-detected every REFIT days with only the candles closed then (chan.find); each day is
judged with chan.state, as the site does. A call needs, as live: an entry status, a proven signal in the same
direction (daily, last 3 days, or the latest closed weekly candle; weekly signals are known after their week closes),
the bull score on the right side (5+ long, 5 or less short), a stop within 20% and 1.5+ reward-to-risk to target 1.
The trade: entry at the call day's close, stop and target 1 as published; whichever is hit first within 60 days
(stop first if both in one day), else closed at day 60. Costs 0.2% round trip. 'Proven' is decided with the full
history and, separately, with data up to 2021 only (clean test of 2022 on).
"""
import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chan as C           # noqa: E402
import chan_calls as K     # noqa: E402
from shortlist_study import proven_sets   # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(ROOT, "data", "binance.db")
REFIT, COST, MIN_ADV, HOLD = 10, 0.002, 2e6, 60


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    print("loading ...", flush=True)
    bs = {(s, d): b for s, d, b in con.execute("SELECT symbol, period_start, bull_score FROM indicators WHERE tf='1d' AND complete=1")}
    sets = {lab: proven_sets(con, early) for lab, early in (("full", False), ("early", True))}
    want = set().union(*[a | b for a, b, _ in sets.values()])
    sig = defaultdict(list)
    for s, tf, ps, name, d in con.execute("SELECT symbol, tf, period_start, signal, direction FROM signals WHERE warm=1 AND provisional=0 AND tf IN ('1d','1w')"):
        if (tf, name) in want:
            known = ps if tf == "1d" else (date.fromisoformat(ps) + timedelta(days=6)).isoformat()
            sig[s].append((known, tf, name, d))
    syms = [s for (s,) in con.execute("SELECT symbol FROM pairs")]
    trades = defaultdict(list)
    for n_sym, s in enumerate(syms):
        d = con.execute("SELECT date, open, high, low, close, volume_usdt FROM daily WHERE symbol=? ORDER BY date", (s,)).fetchall()
        if len(d) < 400:
            continue
        dates = [r[0] for r in d]
        o, h, l, c, v = ([r[k] or 0.0 for r in d] for k in range(1, 6))
        S = sorted(sig.get(s, []))
        chans, last_call = [], -99
        for k in range(300, len(c) - 1):
            adv = sum(v[k - 20:k]) / 20
            if (k - 300) % REFIT == 0:
                chans = C.find(h, l, c, k) if adv >= MIN_ADV else []
            if not chans or adv < MIN_ADV or k - last_call < 5:
                continue
            for f in chans:
                stt = C.state(f, o, h, l, c, k)
                if not stt or stt["status"] not in K.LONG | K.SHORT:
                    continue
                side = "long" if stt["status"] in K.LONG else "short"
                b = bs.get((s, dates[k]))
                if b is None or (side == "long" and b < 5) or (side == "short" and b > 5):
                    continue
                lo, hi = C.lines(f, k)
                w, mid, close = hi - lo, (lo + hi) / 2, c[k]
                if side == "long":
                    line = {"near_bottom": lo, "bottom_bounce": lo, "mid_reclaim": mid, "mid_retest": mid, "breakout": hi}[stt["status"]]
                    stop = line - 0.1 * w
                    t1 = mid if close < mid - 0.05 * w else hi if close < hi - 0.05 * w else hi + w / 2
                else:
                    line = {"breakdown": lo, "mid_loss": mid, "top_reject": hi}[stt["status"]]
                    stop = line + 0.1 * w
                    t1 = mid if close > mid + 0.05 * w else lo
                sg = 1 if side == "long" else -1
                risk = abs(close - stop)
                if risk <= 0 or risk / close > K.MAX_RISK or (t1 - close) * sg <= 0 or abs(t1 - close) / risk < K.MIN_RR:
                    continue
                frm = dates[max(0, k - 2)]
                wk_from = (date.fromisoformat(dates[k]) - timedelta(days=7)).isoformat()
                for lab, (bull, bear, _) in sets.items():
                    if lab == "early" and dates[k] < "2022-01-01":
                        continue
                    want_d = "bullish" if side == "long" else "bearish"
                    pset = bull if side == "long" else bear
                    agree = {(tf, nm) for kn, tf, nm, dd in S if dd == want_d and (tf, nm) in pset and kn <= dates[k] and
                             ((tf == "1d" and kn >= frm) or (tf == "1w" and kn > wk_from))}
                    if not agree:
                        continue
                    out, R = None, None
                    for q in range(k + 1, min(len(c), k + HOLD + 1)):
                        if (sg == 1 and l[q] <= stop) or (sg == -1 and h[q] >= stop):
                            px_ = min(o[q], stop) if sg == 1 else max(o[q], stop)
                            out = px_
                            break
                        if (sg == 1 and h[q] >= t1) or (sg == -1 and l[q] <= t1):
                            out = t1
                            break
                    if out is None:
                        out = c[min(len(c) - 1, k + HOLD)]
                    ret = sg * (out / close - 1) - COST
                    R = sg * (out - close) / risk
                    grade = "A" if len(agree) >= 2 else "B"
                    trades[(lab, side)].append((dates[k], ret, R, grade, stt["status"]))
                last_call = k
                break
        if n_sym % 50 == 0:
            print(f"  {n_sym}/{len(syms)} pairs", flush=True)
    for (lab, side), L in sorted(trades.items()):
        for per, f in (("up to 2021", lambda x: x[0] < "2022-01-01"), ("2022 on", lambda x: x[0] >= "2022-01-01")):
            for g in ("all", "A"):
                T = [x for x in L if f(x) and (g == "all" or x[3] == g)]
                if len(T) < 15:
                    continue
                r = [x[1] for x in T]
                print(f"proven={lab:5s} {side:5s} {per:10s} grade {g:3s} calls {len(T):5d}  win {100 * sum(x > 0 for x in r) / len(r):3.0f}%  "
                      f"avg {100 * st.mean(r):+6.2f}%  avg R {st.mean(x[2] for x in T):+.2f}  "
                      f"PF {sum(x for x in r if x > 0) / max(1e-9, -sum(x for x in r if x < 0)):.2f}", flush=True)
        by = defaultdict(list)
        for x in L:
            if x[0] >= "2022-01-01":
                by[x[4]].append(x[2])
        print(f"proven={lab} {side} 2022 on by status: " + ", ".join(f"{k} {len(v)} avgR {st.mean(v):+.2f}" for k, v in sorted(by.items()) if len(v) >= 10), flush=True)


if __name__ == "__main__":
    sys.exit(main())
