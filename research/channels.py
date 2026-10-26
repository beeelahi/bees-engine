"""Parallel-channel research (daily). No look-ahead: at each bar i the channel is fitted only on swing points
confirmed by bar i (pivot needs R bars after it), over the previous W bars.

Channel fit: slope = least-squares line through the confirmed swing lows (and swing highs, averaged);
lower line = parallel line under all swing lows, upper line = parallel line over all swing highs.
A channel is valid when: >= 2 swing lows touch the lower line and >= 2 swing highs touch the upper line
(within 15% of the width), >= 90% of closes in the window sit inside it, width >= 3 x ATR.

Signals (entry at the next open):
  bounce_long   low reaches the lower line (within 10% of width), close back inside and green -> long,
                stop 0.5 ATR under the lower line, target the upper line (-10% of width)
  fade_short    high reaches the upper line, close back inside and red -> short, mirror
  breakout_long close above the upper line by >= 0.5 ATR on >= 1.5x volume -> long, stop back inside
                (upper line - 0.5 ATR), target = one channel width above the break (measured move)
  breakdown_short mirror below the lower line
Costs: 0.1% fee per side + slippage by liquidity. Time stop 60 bars.
"""
import sys, statistics as stt
from collections import defaultdict
import _env
import load, bot_sim as S
from bot_core import day_num, excluded_tokens, segments

W = int(sys.argv[1]) if len(sys.argv) > 1 else 120
TOPN = int(sys.argv[2]) if len(sys.argv) > 2 else 300
P = 5
SPLIT = day_num("2022-01-01")
rows, tok = load.load(_env.path("daily_v2.csv.gz"), _env.path("pairs_v2.csv"))
data, reg = S.prepare(rows, tok)
excl = excluded_tokens(set(tok.values()))
byday = defaultdict(list)
for s, F in data.items():
    if tok.get(s) in excl: continue
    for i, d in enumerate(F["dn"]):
        v = F["vavg20i"][i]
        if v and v >= 2e6 and F["seg_bar"][i] >= 200: byday[d].append((v, s))
rank = {}
for d, L in byday.items():
    L.sort(reverse=True)
    for k, (v, s) in enumerate(L[:TOPN]): rank[(s, d)] = k + 1


def piv(x, high):
    out = []
    for p in range(P, len(x) - P):
        v = x[p]; a = x[p - P:p]; b = x[p + 1:p + P + 1]
        if high and all(v > z for z in a) and all(v >= z for z in b): out.append(p)
        if not high and all(v < z for z in a) and all(v <= z for z in b): out.append(p)
    return out


def slope(pts):
    n = len(pts)
    if n < 2: return None
    mx = sum(p[0] for p in pts) / n; my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else None


def channel(h, l, c, i, ph, pl):
    """Fit on confirmed pivots in [i-W, i-P]. Returns (m, lo_b, hi_b, width) or None. Lines: y = m*x + b."""
    lows = [(p, l[p]) for p in pl if i - W <= p <= i - P]
    highs = [(p, h[p]) for p in ph if i - W <= p <= i - P]
    if len(lows) < 2 or len(highs) < 2: return None
    m1, m2 = slope(lows), slope(highs)
    if m1 is None or m2 is None: return None
    m = (m1 + m2) / 2
    lo_b = min(y - m * x for x, y in lows)
    hi_b = max(y - m * x for x, y in highs)
    width = hi_b - lo_b
    if width <= 0: return None
    tol = 0.15 * width
    if sum(1 for x, y in lows if y - m * x - lo_b <= tol) < 2: return None
    if sum(1 for x, y in highs if hi_b - (y - m * x) <= tol) < 2: return None
    inside = sum(1 for k in range(i - W, i) if lo_b - 0.02 * width <= c[k] - m * k <= hi_b + 0.02 * width)
    if inside < 0.9 * W: return None
    return m, lo_b, hi_b, width


res = defaultdict(list)
for s, F in data.items():
    if tok.get(s) in excl: continue
    for a, b in segments(F["dn"], F["c"]):
        if b - a < W + 60: continue
        h, l, c, o, v = F["h"][a:b], F["l"][a:b], F["c"][a:b], F["o"][a:b], F["v"][a:b]
        atr = F["atr20"][a:b]; va = F["vavg20"][a:b]
        ph, pl = piv(h, True), piv(l, False)
        n = len(c); i = W
        while i < n - 1:
            if (s, F["dn"][a + i]) not in rank or atr[i] is None:
                i += 1; continue
            ch = channel(h, l, c, i, ph, pl)
            if not ch:
                i += 1; continue
            m, lo_b, hi_b, w = ch
            lo, hi = m * i + lo_b, m * i + hi_b
            sig = None
            if l[i] <= lo + 0.1 * w and c[i] > lo and c[i] > o[i]:
                sig = ("bounce_long", 1, lo - 0.5 * atr[i], hi - 0.1 * w)
            elif h[i] >= hi - 0.1 * w and c[i] < hi and c[i] < o[i]:
                sig = ("fade_short", -1, hi + 0.5 * atr[i], lo + 0.1 * w)
            elif c[i] > hi + 0.5 * atr[i] and c[i - 1] <= m * (i - 1) + hi_b and va[i] and v[i] >= 1.5 * va[i]:
                sig = ("breakout_long", 1, hi - 0.5 * atr[i], c[i] + w)
            elif c[i] < lo - 0.5 * atr[i] and c[i - 1] >= m * (i - 1) + lo_b and va[i] and v[i] >= 1.5 * va[i]:
                sig = ("breakdown_short", -1, lo + 0.5 * atr[i], c[i] - w)
            if not sig:
                i += 1; continue
            kind, sg, stop, tgt = sig
            ent = o[i + 1]
            risk = abs(ent - stop)
            if risk <= 0 or sg * (tgt - ent) <= 0 or sg * (ent - stop) <= 0:
                i += 1; continue
            slip = S.slippage(F["vavg20i"][a + i] or 0)
            px, why, j = None, "time", i + 1
            for j in range(i + 1, min(i + 61, n)):
                if (sg == 1 and l[j] <= stop) or (sg == -1 and h[j] >= stop):
                    px = min(o[j], stop) if sg == 1 else max(o[j], stop); why = "stop"; break
                if (sg == 1 and h[j] >= tgt) or (sg == -1 and l[j] <= tgt):
                    px = tgt; why = "target"; break
            if px is None: px = c[j]
            pnl = sg * (px - ent) - (ent + px) * (0.001 + slip)
            res[kind].append(dict(day=F["dn"][a + i], r=pnl / risk, pct=100 * pnl / ent, why=why, reg=reg.get(F["dn"][a + i], 0),
                                  rr=abs(tgt - ent) / risk, slope=m / c[i] * 100))
            i = j + 1                      # one trade at a time per pair

def show(name, lst):
    for per, sel in (("<=2021", [t for t in lst if t["day"] < SPLIT]), ("2022+", [t for t in lst if t["day"] >= SPLIT])):
        if not sel: print(f"{name:26s} {per:7s} n=0"); continue
        R = [t["r"] for t in sel]; gw = sum(x for x in R if x > 0); gl = -sum(x for x in R if x <= 0)
        print(f"{name:26s} {per:7s} n={len(sel):5d} win={100*sum(1 for x in R if x>0)/len(R):5.1f}% avgR={sum(R)/len(R):+.2f} "
              f"PF={gw/gl if gl else 99:.2f} medPct={stt.median(t['pct'] for t in sel):+5.1f}% target={100*sum(1 for t in sel if t['why']=='target')/len(sel):4.1f}% RR={sum(t['rr'] for t in sel)/len(sel):.2f}")
print(f"window {W}, top {TOPN}")
for k in ("bounce_long", "fade_short", "breakout_long", "breakdown_short"):
    L = res[k]; show(k, L)
    show(k + " rising ch.", [t for t in L if t["slope"] > 0.05])
    show(k + " falling ch.", [t for t in L if t["slope"] < -0.05])
    show(k + " BTC up", [t for t in L if t["reg"] == 1])
