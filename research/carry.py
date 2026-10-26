"""Funding-rate carry study: long spot + short USDT-M perpetual of the same coin (price risk hedged),
collecting funding every 8h. Enter when the trailing 7-day average funding (annualised) is above ENTER_APR,
exit when the trailing 3-day average falls below EXIT_APR. Costs per round trip: spot 0.1% x2 + futures 0.05% x2
+ 0.1% slippage allowance = 0.4% of the position. Usage: python3 carry.py funding.csv"""
import csv, sys, statistics as st
from collections import defaultdict
rows = defaultdict(list)
for r in csv.DictReader(open(sys.argv[1])):
    rows[r["symbol"]].append((int(r["time_ms"]), float(r["rate"])))
COST = 0.004
def run(enter_apr, exit_apr, since_ms=0):
    out = {}
    for s, xs in rows.items():
        xs = sorted(x for x in xs if x[0] >= since_ms)
        if len(xs) < 60: continue
        pos, earned, trips, days_in = False, 0.0, 0, 0
        for i in range(21, len(xs)):
            a7 = sum(r for _, r in xs[i - 21:i]) / 21 * 3 * 365 * 100
            a3 = sum(r for _, r in xs[i - 9:i]) / 9 * 3 * 365 * 100
            if not pos and a7 >= enter_apr:
                pos, trips = True, trips + 1; earned -= COST
            elif pos and a3 < exit_apr:
                pos = False
            if pos:
                earned += xs[i][1]; days_in += 1 / 3
        span = (xs[-1][0] - xs[21][0]) / 86400000
        if span > 30:
            out[s] = {"net_pct": earned * 100, "apr_on_capital_in_use": (earned / (days_in / 365)) * 100 if days_in > 5 else 0,
                      "share_of_time_in": days_in / span, "trips": trips, "span_days": span}
    return out
import time
for since_label, since in (("all history", 0), ("since 2024", int(time.mktime((2024, 1, 1, 0, 0, 0, 0, 0, 0)) * 1000)),
                           ("last 365d", int((time.time() - 365 * 86400) * 1000))):
    for ea, xa in ((10, 3), (15, 5), (25, 8), (40, 10)):
        res = run(ea, xa, since)
        if not res: continue
        aprs = [v["apr_on_capital_in_use"] for v in res.values() if v["share_of_time_in"] > 0.05]
        best = sorted(res.items(), key=lambda kv: -kv[1]["net_pct"])[:5]
        print(f"{since_label:11s} enter {ea:>3}% exit {xa:>2}%: coins {len(res)}, median APR while in {st.median(aprs) if aprs else 0:5.1f}%, "
              f"share in market {st.median(v['share_of_time_in'] for v in res.values())*100:4.0f}%, positive {100*sum(1 for v in res.values() if v['net_pct']>0)/len(res):3.0f}% | best: " +
              ", ".join(f"{k.replace('USDT','')} {v['net_pct']:.0f}%" for k, v in best))
