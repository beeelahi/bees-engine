"""Trace one reference pattern through the STF rules on the lab's cached candles (read-only).
Usage: python3 research/stf_ref.py BTC 1h "2026-11-23 14:00" [variant]"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stf_lab as L  # noqa: E402

S, HS = L.S, L.HS
code, tf, head = sys.argv[1:4]
var = sys.argv[4] if len(sys.argv) > 4 else "spike_head"
for k, v in dict(next(x for x in L.VARIANTS if x[0] == var)[2]).items():
    setattr(S, k, v)
rows = [tuple(r) for r in json.load(open(os.path.join(L.CACHE, f"{code}_{tf}.json")))["rows"]]
t, o, h, l, c = S._series(rows)
print(f"{code} {tf} {len(rows)} candles {L.utc(t[0])} .. {L.utc(t[-1])}; variant {var}")
hd = next(k for k in range(len(t)) if L.utc(t[k]) == head)
for k in range(hd - 3, len(t)):
    print(L.utc(t[k]), o[k], h[k], l[k], c[k])
for P in S.P_SIZES:
    pk = HS.pivots(h, P, True)
    print(f"P{P} swing highs after the head:", [L.utc(t[k]) for k in pk if k >= hd][:6])
    for p in S.find(h, l, c, P):
        if p["hd"] == hd:
            print(f"P{P} FOUND {p['kind']} LS {L.utc(t[p['lsh']])} RS {L.utc(t[p['rsh']])} known {L.utc(t[p['known']])} H {p['height']:.0f}")
            for i in range(p["known"], min(len(c), p["rsh"] + HS.MAX_WAIT + 2)):
                s = HS.state(p, o, h, l, c, i)
                print("  ", L.utc(t[i]), s and (s["status"], round(s["neck"]), s["brk"] and L.utc(t[s["brk"]])))
                if s is None or s["brk"] is not None:
                    break
print("breaks:", [(b["kind"], L.utc(b["bar_ms"])) for b in L.direct_breaks(rows) if b["head_ms"] == t[hd]])
