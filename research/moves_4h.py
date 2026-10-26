"""Big 4-hour moves on liquid coins: what happened next. Writes move_stats.json (copy it to bin/).

Same event rules as bin/moves_4h.py, on the 4h candle export (candles_4h.csv.gz). Liquid = average 4h volume over
the previous 30 candles >= $2M a day. Results from the next candle's open: 1 day (6 candles) and 3 days
(18 candles); 'vs market' = the coin's change minus the median liquid coin's change over the same candles.
"""
import csv
import gzip
import json
import statistics as st
import sys
from collections import defaultdict

import _env
sys.path.insert(0, _env.HERE + "/../bin")
import moves_4h as M

D = defaultdict(list)
with gzip.open(_env.path("candles_4h.csv.gz"), "rt") as f:
    rd = csv.reader(f)
    next(rd)
    for s, t, o, h, l, c, v in rd:
        D[s].append((int(t), float(o), float(h), float(l), float(c), float(v or 0)))
fwd, ev = defaultdict(dict), defaultdict(list)
for s, X in D.items():
    if s.split("USDT")[0] in M.STABLE:
        continue
    X.sort()
    for k in range(M.LOOK + 1, len(X) - 19):
        e = X[k + 1][1]
        vavg = sum(x[5] for x in X[k - 30:k]) / 30
        if e <= 0 or vavg * 6 < M.MIN_ADV:
            continue
        fwd[X[k][0]][s] = (X[k + 6][4] / e - 1, X[k + 18][4] / e - 1)
        kind = M.classify(X, k)
        if kind:
            ev[kind].append((X[k][0], s))
out, first, last = {}, min(fwd), max(fwd)
for kind, L in ev.items():
    R = []
    for t, s in L:
        b6, b18 = st.median(x[0] for x in fwd[t].values()), st.median(x[1] for x in fwd[t].values())
        r6, r18 = fwd[t][s]
        R.append((100 * r6, 100 * r18, 100 * (r18 - b18)))
    if len(R) < 30:
        continue
    out[kind] = {"n": len(R), "med1d": round(st.median(x[0] for x in R), 1), "up1d": round(100 * sum(x[0] > 0 for x in R) / len(R)),
                 "med3d": round(st.median(x[1] for x in R), 1), "up3d": round(100 * sum(x[1] > 0 for x in R) / len(R)),
                 "rel3d": round(st.median(x[2] for x in R), 1), "beat3d": round(100 * sum(x[2] > 0 for x in R) / len(R))}
    print(kind, out[kind])
from datetime import datetime, timezone
iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")   # noqa: E731
out["_meta"] = {"from": iso(first), "to": iso(last), "pairs": len(D)}
with open(_env.path("move_stats.json"), "w") as f:
    json.dump(out, f, separators=(",", ":"))
