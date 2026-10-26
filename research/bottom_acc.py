"""Bottom-accumulation research (daily, top-N liquid, no look-ahead).
Base: over the last B days the range high/low <= R (contained), and the base low sits >= DD below the prior
2-year high (deep drawdown). Signal: first close above the base high on >= V x the 20-day average volume.
Measured: 30/60/90-day returns from the next open, win %, and a trade with stop = base low - 0.5 ATR
(or tighter: base mid) and no target (exit after 90 days), in R."""
import sys, statistics as st
from collections import defaultdict
import _env
import load, bot_sim as S
from bot_core import day_num, excluded_tokens, segments, _prev_max, _prev_min

TOPN = 150
SPLIT = day_num("2022-01-01")
rows, tok = load.load(_env.path("daily_v2.csv.gz"), _env.path("pairs_v2.csv"))
data, reg = S.prepare(rows, tok)
excl = excluded_tokens(set(tok.values()))
byday = defaultdict(list)
for s, F in data.items():
    if tok.get(s) in excl: continue
    for i, d in enumerate(F["dn"]):
        v = F["vavg20i"][i]
        if v and v >= 1e6 and F["seg_bar"][i] >= 250: byday[d].append((v, s))
rank = {}
for d, L in byday.items():
    L.sort(reverse=True)
    for k, (v, s) in enumerate(L[:TOPN]): rank[(s, d)] = k + 1


def study(B, R, DD, V, need_rank=True):
    out = []
    for s, F in data.items():
        if tok.get(s) in excl: continue
        for a, b in segments(F["dn"], F["c"]):
            if b - a < 400: continue
            o, h, l, c, v = (F[k][a:b] for k in ("o", "h", "l", "c", "v"))
            atr = F["atr20"][a:b]
            hb, lb = _prev_max(h, B), _prev_min(l, B)
            h2y = _prev_max(h, 730)
            last = -999
            for i in range(400, len(c) - 1):
                if need_rank and (s, F["dn"][a + i]) not in rank: continue
                if not (hb[i] and lb[i] and h2y[i] and atr[i]): continue
                if hb[i] / lb[i] > R or lb[i] > (1 - DD) * h2y[i]: continue
                va = sum(v[i - 20:i]) / 20
                if not (c[i] > hb[i] and c[i - 1] <= hb[i] and va > 0 and v[i] >= V * va): continue
                if i - last < 30: continue
                last = i
                ent = o[i + 1]
                stop = lb[i] - 0.5 * atr[i]
                fw = {H: (c[min(len(c) - 1, i + H)] / ent - 1) * 100 for H in (30, 60, 90)}
                # trade: stop at base low, else hold 90 days
                px, why = c[min(len(c) - 1, i + 90)], "time"
                for j in range(i + 1, min(len(c), i + 91)):
                    if l[j] <= stop: px, why = min(o[j], stop), "stop"; break
                risk = ent - stop
                out.append(dict(day=F["dn"][a + i], sym=s, r=(px - ent) / risk if risk > 0 else 0, risk_pct=100 * risk / ent,
                                reg=reg.get(F["dn"][a + i], 0), **{f"f{H}": x for H, x in fw.items()}))
    return out


def show(name, L):
    for per, sel in (("<=2021", [x for x in L if x["day"] < SPLIT]), ("2022+", [x for x in L if x["day"] >= SPLIT])):
        if not sel: print(f"  {name:38s} {per:6s} n=0"); continue
        R = [x["r"] for x in sel]; gw = sum(r for r in R if r > 0); gl = -sum(r for r in R if r <= 0)
        print(f"  {name:38s} {per:6s} n={len(sel):4d} med30 {st.median(x['f30'] for x in sel):+6.1f}% med90 {st.median(x['f90'] for x in sel):+6.1f}% "
              f"win90 {100*sum(1 for x in sel if x['f90']>0)/len(sel):4.0f}% | trade PF {gw/gl if gl else 99:4.2f} avgR {sum(R)/len(R):+.2f} risk {st.median(x['risk_pct'] for x in sel):4.1f}%")


for B, R_, DD, V in ((90, 2.0, 0.0, 2.0), (90, 1.8, 0.7, 2.0), (120, 1.8, 0.75, 2.0), (180, 2.0, 0.75, 2.0), (120, 1.6, 0.8, 1.5), (180, 1.8, 0.8, 1.5), (60, 1.5, 0.8, 2.0)):
    L = study(B, R_, DD, V)
    print(f"base {B}d, range <= {R_}x, drawdown >= {DD:.0%}, volume >= {V}x")
    show("all", L)
    show("BTC above 200d", [x for x in L if x["reg"] == 1])
