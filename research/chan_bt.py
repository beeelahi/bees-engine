"""Track record of the live channel detector (bin/chan.py), daily, top-300 liquid pairs, no look-ahead.
Events (one per kind per pair per 10 bars):
  near_bottom: close enters the lowest 15% of a great channel (inside)  -> did it reach the top zone before breaking down?
  near_top:    mirror                                                    -> did it reach the bottom zone before breaking out?
  breakout / breakdown: chan.state rules                                  -> 30-bar move, failed back inside within 5 bars?
Returns measured from the next open; 'reached other side' uses the channel lines frozen at the event, extended."""
import sys, statistics as st, json
from collections import defaultdict
import _env
import load, bot_sim as S
H4 = '--4h' in sys.argv
if H4: sys.argv.remove('--4h')
from bot_core import day_num, excluded_tokens, segments
import chan as C
from bot_core import _atr

TOPN = int(sys.argv[1]) if len(sys.argv) > 1 else 300
SPLIT = day_num("2022-01-01")
if H4:
    import load4h
    data, reg, tok, rank, SPLIT = load4h.get(topn=TOPN)
    excl = set()
else:
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

ev = defaultdict(list)
TFK = "4h" if H4 else "1d"
cfg = C.TF[TFK]
for s, F in data.items():
    if tok.get(s) in excl: continue
    for a, b in segments(F["dn"], F["c"]):
        if b - a < (400 if H4 else 250): continue
        o, h, l, c, v = (F[k][a:b] for k in ("o", "h", "l", "c", "v"))
        dn = F["dn"][a:b]
        atr = _atr(h, l, c, 20)
        ph, pl = C.pivots(h, cfg["P"], True), C.pivots(l, cfg["P"], False)
        n = len(c); last = {}
        prev = None
        for i in range(300 if H4 else 200, n - 1):
            if (s, dn[i]) not in rank:
                prev = None; continue
            f = C.best(h, l, c, atr, i, TFK, ph, pl)
            # breaks judged on the channel of the previous bar
            if prev and atr[i]:
                lo_i, hi_i = C.lines(prev, i); lo_p, hi_p = C.lines(prev, i - 1)
                va = sum(v[i - 20:i]) / 20
                kind = None
                if c[i] > hi_i + C.BREAK_ATR * atr[i] and c[i - 1] <= hi_p and v[i] >= C.BREAK_VOL * va: kind, sg, line = "breakout", 1, hi_i
                elif c[i] < lo_i - C.BREAK_ATR * atr[i] and c[i - 1] >= lo_p and v[i] >= C.BREAK_VOL * va: kind, sg, line = "breakdown", -1, lo_i
                if kind and i - last.get(kind, -99) >= 10:
                    last[kind] = i; ent = o[i + 1]
                    end = min(n - 1, i + 30)
                    back = any((c[k] < C.lines(prev, k)[1]) if sg == 1 else (c[k] > C.lines(prev, k)[0]) for k in range(i + 1, min(n, i + 6)))
                    mfe = max(h[i + 1:end + 1]) / ent - 1 if sg == 1 else 1 - min(l[i + 1:end + 1]) / ent
                    ev[kind].append(dict(day=dn[i], r30=sg * (c[end] / ent - 1) * 100, r10=sg * (c[min(n - 1, i + 10)] / ent - 1) * 100,
                                         mfe=mfe * 100, failed=back, reg=reg.get(dn[i], 0), slope=prev["m"] / c[i] * 100))
            if f:
                lo, hi = C.lines(f, i); w = f["w"]
                pos = (c[i] - lo) / w
                lo1, hi1 = C.lines(f, i - 1); pos1 = (c[i - 1] - lo1) / w
                for kind, cond, cond1 in (("near_bottom", 0 <= pos <= C.EDGE, pos1 > C.EDGE), ("near_top", 1 - C.EDGE <= pos <= 1, pos1 < 1 - C.EDGE)):
                    if cond and cond1 and i - last.get(kind, -99) >= 10:
                        last[kind] = i; ent = o[i + 1]; res = "neither"
                        for k in range(i + 1, min(n, i + 61)):
                            lk, hk = C.lines(f, k)
                            if kind == "near_bottom":
                                if c[k] < lk - 0.5 * atr[i]: res = "broke"; break
                                if h[k] >= hk - C.EDGE * w: res = "other_side"; break
                            else:
                                if c[k] > hk + 0.5 * atr[i]: res = "broke"; break
                                if l[k] <= lk + C.EDGE * w: res = "other_side"; break
                        sg = 1 if kind == "near_bottom" else -1
                        end = min(n - 1, i + 30)
                        ev[kind].append(dict(day=dn[i], r30=sg * (c[end] / ent - 1) * 100, r10=sg * (c[min(n - 1, i + 10)] / ent - 1) * 100,
                                             res=res, reg=reg.get(dn[i], 0), slope=f["m"] / c[i] * 100, width=100 * w / c[i]))
            prev = f


def summ(L):
    if not L: return None
    out = dict(n=len(L), med30=round(st.median(x["r30"] for x in L), 1), win30=round(100 * sum(1 for x in L if x["r30"] > 0) / len(L)),
               med10=round(st.median(x["r10"] for x in L), 1))
    if "res" in L[0]:
        out["other_side"] = round(100 * sum(1 for x in L if x["res"] == "other_side") / len(L))
        out["broke"] = round(100 * sum(1 for x in L if x["res"] == "broke") / len(L))
    if "failed" in L[0]:
        out["failed5"] = round(100 * sum(1 for x in L if x["failed"]) / len(L))
        out["mfe_med"] = round(st.median(x["mfe"] for x in L), 1)
    return out


R = {}
for k in ("near_bottom", "near_top", "breakout", "breakdown"):
    L = ev[k]
    R[k] = {"early": summ([x for x in L if x["day"] < SPLIT]), "late": summ([x for x in L if x["day"] >= SPLIT]),
            "btc_up": summ([x for x in L if x["day"] >= SPLIT and x["reg"] == 1]), "btc_down": summ([x for x in L if x["day"] >= SPLIT and x["reg"] != 1]),
            "rising": summ([x for x in L if x["day"] >= SPLIT and x["slope"] > 0.05]), "falling": summ([x for x in L if x["day"] >= SPLIT and x["slope"] < -0.05])}
    for g, x in R[k].items(): print(f"{k:12s} {g:8s} {x}")
json.dump(R, open(_env.path("chan_stats%s.json" % ("_4h" if H4 else "")), "w"))
