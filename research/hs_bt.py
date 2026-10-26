"""Head-and-shoulders (bin/hs.py) track record by entry: near the neckline, the break, the neckline retest.
Daily, liquid pairs (20-day average volume >= $2M at the entry), tops and inverse, no look-ahead (a pattern is used
only from the candle its right shoulder is confirmed). Entry at the next day's open, in the pattern's direction
(top = down, inverse = up). Results:
  med30 / win30   median 30-day move in the pattern's direction, share that moved that way
  rel30 / beat30  the same move minus the average liquid coin's (direction-adjusted), share that beat the market
  target / avg_r  as a trade: stop = the failure level (neckline + 35% of the height against the trade; for
                  near-neckline entries, beyond the right shoulder), target = the measured move; first hit within 60
                  days, else marked at day 60; R capped at -1.5.
Usage: python3 research/hs_bt.py   (writes hs_stats.json to RESEARCH_DATA; copy it to bin/)"""
import json
import statistics as st
from collections import defaultdict

import _env
import load
import bot_sim as S
from bot_core import day_num, excluded_tokens, segments
import hs as HS

SPLIT, MIN_ADV = day_num("2022-01-01"), 2e6
rows, tok = load.load(_env.path("daily_v2.csv.gz"), _env.path("pairs_v2.csv"))
data, reg = S.prepare(rows, tok)
excl = excluded_tokens(set(tok.values()))
base = defaultdict(list)
for s, F in data.items():
    if tok.get(s) in excl:
        continue
    o, c, dn, va = F["o"], F["c"], F["dn"], F["vavg20i"]
    for k in range(len(c) - 31):
        if va[k] and va[k] >= MIN_ADV and o[k + 1] > 0:
            base[dn[k]].append((c[k + 30] / o[k + 1] - 1) * 100)
bmed = {d: st.median(v) for d, v in base.items() if v}

ev = []
for s, F in data.items():
    if tok.get(s) in excl:
        continue
    for a, b in segments(F["dn"], F["c"]):
        if b - a < 120:
            continue
        o, h, l, c = (F[k][a:b] for k in ("o", "h", "l", "c"))
        dn, va = F["dn"][a:b], F["vavg20i"][a:b]
        n = len(c)
        seen = set()
        for P in HS.P_SIZES:
            for p in HS.find(h, l, c, P):
                if p["hd"] in seen:
                    continue
                seen.add(p["hd"])
                sg, H, kn = p["sg"], p["height"], p["known"]
                d = -sg                                     # trade direction: top -> -1 (down), inverse -> +1
                shoulder = (h if sg == 1 else l)[p["rsh"]]
                brk, events = None, []
                for k in range(p["rsh"] + 1, min(n - 1, p["rsh"] + HS.MAX_WAIT + 1)):
                    if sg * (h if sg == 1 else l)[k] > sg * shoulder:
                        break
                    if sg * c[k] < sg * HS.neck(p, k):
                        brk = k
                        break
                    if k >= kn and HS.neck(p, k) and sg * (HS.neck(p, k) - c[k]) / H >= -HS.NEAR and not events:
                        events.append(("near_neckline", k, shoulder))
                if brk is not None:
                    e = max(brk, kn)
                    if e - brk < HS.BREAK_FRESH:
                        events.append(("broken", e, None))
                    away = False
                    for k in range(brk + 1, min(n - 1, brk + HS.MAX_AGE)):
                        if sg * c[k] > sg * HS.neck(p, k) + HS.FAIL * H:
                            break
                        if (sg == 1 and l[k] <= HS.neck(p, k) - H) or (sg == -1 and h[k] >= HS.neck(p, k) + H):
                            break
                        if sg * (HS.neck(p, k) - c[k]) / H >= HS.AWAY:
                            away = True
                            continue
                        if k >= kn and away and sg * (HS.neck(p, k) - (h if sg == 1 else l)[k]) / H <= HS.RETEST and sg * (HS.neck(p, k) - c[k]) / H >= -HS.RETEST:
                            events.append(("retest", k, None))
                            break
                for kind, k, sh in events:
                    if k + 1 >= n or not va[k] or va[k] < MIN_ADV or o[k + 1] <= 0:
                        continue
                    ent = o[k + 1]
                    end = min(n - 1, k + 30)
                    r30 = (c[end] / ent - 1) * 100
                    nk = HS.neck(p, k)
                    stop = sh if sh is not None else nk + sg * HS.FAIL * H
                    tgt = nk - sg * H
                    risk = (stop - ent) * sg
                    res, R = None, None
                    if risk > 0 and (tgt - ent) * d > 0:
                        res = "open"
                        for q in range(k + 1, min(n, k + 61)):
                            if (h[q] >= stop) if sg == 1 else (l[q] <= stop):
                                res, R = "stop", -abs((o[q] if (o[q] - stop) * sg > 0 else stop) - ent) / risk
                                break
                            if (l[q] <= tgt) if sg == 1 else (h[q] >= tgt):
                                res, R = "target", abs(tgt - ent) / risk
                                break
                        if res == "open":
                            R = d * (c[min(n - 1, k + 60)] - ent) / risk
                    ev.append({"kind": p["kind"], "entry": kind, "day": dn[k], "move30": d * r30,
                               "rel30": d * (r30 - bmed[dn[k]]) if dn[k] in bmed else None, "res": res, "R": R, "full": k + 30 < n,
                               "reg": reg.get(dn[k], 0)})


def summ(L):
    L = [x for x in L if x["full"] and x["rel30"] is not None]
    if len(L) < 15:
        return None
    out = {"n": len(L), "med30": round(st.median(x["move30"] for x in L), 1), "win30": round(100 * sum(x["move30"] > 0 for x in L) / len(L)),
           "rel30": round(st.median(x["rel30"] for x in L), 1), "beat30": round(100 * sum(x["rel30"] > 0 for x in L) / len(L))}
    T = [x for x in L if x["R"] is not None]
    if len(T) >= 15:
        out["target"] = round(100 * sum(x["res"] == "target" for x in T) / len(T))
        out["avg_r"] = round(st.mean(max(-1.5, x["R"]) for x in T), 2)
    return out


R = {}
for kind in ("top", "inverse"):
    for entry in ("near_neckline", "broken", "retest"):
        L = [x for x in ev if x["kind"] == kind and x["entry"] == entry]
        R[f"{kind}:{entry}"] = {"early": summ([x for x in L if x["day"] < SPLIT]), "late": summ([x for x in L if x["day"] >= SPLIT]),
                                "late_btc_up": summ([x for x in L if x["day"] >= SPLIT and x["reg"] == 1]),
                                "late_btc_down": summ([x for x in L if x["day"] >= SPLIT and x["reg"] != 1])}
        print(kind, entry, R[f"{kind}:{entry}"])
with open(_env.path("hs_stats.json"), "w") as f:
    json.dump(R, f, separators=(",", ":"))
