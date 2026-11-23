"""Case study for STF HNS: Bilal's gold 15-minute trade (20 Nov 2026). Prints the XAUUSDT 15m candles around it, the
swing points for several swing sizes, and why each candidate shoulder / head / shoulder set passes or fails the
detector's rules. Read-only, no writes. Usage: python3 research/stf_case.py"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin"))
import hs as HS  # noqa: E402
import stf_hns as S  # noqa: E402

A, B = "2026-11-19 18:00", "2026-11-21 06:00"
f = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%m-%d %H:%M")  # noqa: E731
rows = S.klines("fapi", "XAUUSDT", "15m", 1000)
t, o, h, l, c = S._series(rows)
ks = [k for k in range(len(t)) if A <= datetime.fromtimestamp(t[k] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") <= B]
print(f"XAUUSDT 15m {f(t[ks[0]])} .. {f(t[ks[-1]])} UTC ({len(ks)} candles): time open high low close")
for k in ks:
    print(f"{f(t[k])} {o[k]:.2f} {h[k]:.2f} {l[k]:.2f} {c[k]:.2f}")
for P in (2, 3, 4, 5):
    ph = [k for k in HS.pivots(h, P, True) if ks[0] <= k <= ks[-1]]
    pl = [k for k in HS.pivots(l, P, False) if ks[0] <= k <= ks[-1]]
    print(f"P{P} swing highs: " + ", ".join(f"{f(t[k])} {h[k]:.1f}" for k in ph))
    print(f"P{P} swing lows:  " + ", ".join(f"{f(t[k])} {l[k]:.1f}" for k in pl))
    for j in range(2, len(ph)):
        a, b, d = ph[j - 2], ph[j - 1], ph[j]
        if not h[b] > max(h[a], h[d]):
            continue
        t1 = min(range(a + 1, b), key=lambda x: l[x]); t2 = min(range(b + 1, d), key=lambda x: l[x])
        m = (l[t2] - l[t1]) / (t2 - t1); nk = l[t1] + m * (b - t1); H = h[b] - nk
        why = []
        if d - a > S.SPAN: why.append("too long")
        if abs(h[a] - h[d]) > 0.35 * H: why.append(f"shoulders differ {abs(h[a]-h[d]):.1f} > 35% of height {H:.1f}")
        if h[b] - max(h[a], h[d]) < 0.2 * H: why.append(f"head only {h[b]-max(h[a],h[d]):.1f} above shoulders (< 20% of {H:.1f})")
        r = (b - a) / max(1, d - b)
        if not 0.4 <= r <= 2.5: why.append(f"head off-centre ({r:.2f})")
        if abs(l[t1] - l[t2]) > 0.5 * H: why.append("troughs too uneven")
        print(f"   P{P} TOP? LS {f(t[a])} {h[a]:.1f} H {f(t[b])} {h[b]:.1f} RS {f(t[d])} {h[d]:.1f} neck {l[t1]:.1f}->{l[t2]:.1f} height {H:.1f}: {'; '.join(why) or 'PASSES'}")
