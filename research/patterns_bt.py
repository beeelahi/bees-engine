"""Track record of the classic patterns in bin/patterns.py (double tops / bottoms, cup and handle) on the daily and
the weekly chart, by entry (near the line / handle, the break, the retest). No look-ahead: a pattern is used only from
the candle its second peak / right rim is confirmed; a weekly pattern only after the weekly close (entry at the next
daily open, Monday). Liquid pairs only (20-day average volume >= $2M at the entry). Direction: top -> down, bottom
and cup -> up. Horizon: 30 days for daily patterns, 90 days for weekly ones.
  med / win    median move in the pattern's direction over the horizon, share that moved that way
  rel / beat   the same move minus the average liquid coin's (direction-adjusted), share that beat the market
  target/avg_r as a trade: stop = the failure level (line + 35% of the height against the trade; before the break:
               the higher peak, or the handle low for cups), target = the measured move; first hit within 2x the
               horizon, else marked there; R capped at -1.5
Splits: early = before 2022, late = 2022 on (what the website shows), and late by BTC regime (above / below its 200-day).
Usage: python3 research/patterns_bt.py   (writes patterns_stats.json to RESEARCH_DATA; copy it to bin/)"""
import json
import statistics as st
from collections import defaultdict

import _env
import load
import bot_sim as S
from bot_core import day_num, excluded_tokens, segments
import patterns as PT

SPLIT, MIN_ADV = day_num("2022-01-01"), 2e6
MON = day_num("2024-01-01")                                  # a Monday
rows, tok = load.load(_env.path("daily_v2.csv.gz"), _env.path("pairs_v2.csv"))
data, reg = S.prepare(rows, tok)
excl = excluded_tokens(set(tok.values()))

base = {30: defaultdict(list), 90: defaultdict(list)}
for s, F in data.items():
    if tok.get(s) in excl:
        continue
    o, c, dn, va = F["o"], F["c"], F["dn"], F["vavg20i"]
    for k in range(len(c) - 1):
        if va[k] and va[k] >= MIN_ADV and o[k + 1] > 0:
            for hz in (30, 90):
                if k + hz < len(c):
                    base[hz][dn[k]].append((c[k + hz] / o[k + 1] - 1) * 100)
bmed = {hz: {d: st.median(v) for d, v in b.items() if v} for hz, b in base.items()}


def trade(sg_trade, ent, stop, tgt, h, l, o, c, k, n, span):
    """sg_trade +1 = long. Returns (result, R)."""
    risk = (ent - stop) * sg_trade
    if risk <= 0 or (tgt - ent) * sg_trade <= 0:
        return None, None
    for q in range(k + 1, min(n, k + span + 1)):
        if (l[q] <= stop) if sg_trade == 1 else (h[q] >= stop):
            px = o[q] if (o[q] - stop) * sg_trade < 0 else stop
            return "stop", max(-1.5, (px - ent) * sg_trade / risk)
        if (h[q] >= tgt) if sg_trade == 1 else (l[q] <= tgt):
            return "target", (tgt - ent) * sg_trade / risk
    return "open", max(-1.5, sg_trade * (c[min(n - 1, k + span)] - ent) / risk)


ev = []
for s, F in data.items():
    if tok.get(s) in excl:
        continue
    for a, b in segments(F["dn"], F["c"]):
        if b - a < 120:
            continue
        o, h, l, c = (F[key][a:b] for key in ("o", "h", "l", "c"))
        dn, va = F["dn"][a:b], F["vavg20i"][a:b]
        n = len(c)
        # weekly candles from this segment (complete Monday-Sunday weeks)
        W, cur = [], None
        for k in range(n):
            wd = (dn[k] - MON) % 7
            ws = dn[k] - wd
            if ws != cur:
                W.append([o[k], h[k], l[k], c[k], k, wd])
                cur = ws
            else:
                w = W[-1]
                w[1], w[2], w[3], w[4], w[5] = max(w[1], h[k]), min(w[2], l[k]), c[k], k, wd
        W = [w for w in W if w[5] == 6]
        series = [("1d", h, l, c, list(range(n)))]
        if len(W) >= 30:
            series.append(("1w", [w[1] for w in W], [w[2] for w in W], [w[3] for w in W], [w[4] for w in W]))
        for tf, X, Y, Z, last_day in series:
            cfg = PT.TF[tf]
            hz = 30 if tf == "1d" else 90
            for p in PT.scan(X, Y, Z, cfg):
                sg, H, line = p["sg"], p["height"], p["line"]
                d = -sg                                            # trade direction
                for entry, kw in PT.events(p, X, Y, Z, cfg):
                    k = last_day[kw]                               # daily candle of the signal close
                    if k + 1 >= n or not va[k] or va[k] < MIN_ADV or o[k + 1] <= 0:
                        continue
                    ent = o[k + 1]
                    end = k + hz
                    full = end < n
                    mv = d * (c[min(n - 1, end)] / ent - 1) * 100
                    bm = bmed[hz].get(dn[k])
                    if p["type"] == "cup":
                        stop = line - PT.FAIL * H if entry == "retest" else min(Y[p["z"] + 1:kw + 1] or [Y[p["z"]]])   # handle low
                    else:
                        stop = p["inval"] if entry == "near_neckline" else line + sg * PT.FAIL * H
                    tgt = line - sg * H
                    res, R = trade(d, ent, stop, tgt, h, l, o, c, k, n, 2 * hz)
                    ev.append({"key": f"{tf}:{p['kind']}:{entry}", "day": dn[k], "move": mv, "rel": None if bm is None else mv - d * bm,
                               "full": full, "res": res, "R": R, "reg": reg.get(dn[k], 0), "sym": s})


def summ(L):
    L = [x for x in L if x["full"] and x["rel"] is not None]
    if len(L) < 12:
        return None if not L else {"n": len(L)}
    out = {"n": len(L), "coins": len({x["sym"] for x in L}), "med": round(st.median(x["move"] for x in L), 1),
           "win": round(100 * sum(x["move"] > 0 for x in L) / len(L)),
           "rel": round(st.median(x["rel"] for x in L), 1), "beat": round(100 * sum(x["rel"] > 0 for x in L) / len(L))}
    T = [x for x in L if x["R"] is not None]
    if len(T) >= 12:
        out["target"] = round(100 * sum(x["res"] == "target" for x in T) / len(T))
        out["avg_r"] = round(st.mean(x["R"] for x in T), 2)
    return out


R = {}
for key in sorted({x["key"] for x in ev}):
    L = [x for x in ev if x["key"] == key]
    R[key] = {"h": 30 if key.startswith("1d") else 90, "early": summ([x for x in L if x["day"] < SPLIT]),
              "late": summ([x for x in L if x["day"] >= SPLIT]),
              "late_btc_up": summ([x for x in L if x["day"] >= SPLIT and x["reg"] == 1]),
              "late_btc_down": summ([x for x in L if x["day"] >= SPLIT and x["reg"] != 1])}
    print(key, "early", R[key]["early"], "\n   late", R[key]["late"], "\n   up", R[key]["late_btc_up"], "\n   down", R[key]["late_btc_down"])
with open(_env.path("patterns_stats.json"), "w") as f:
    json.dump(R, f, separators=(",", ":"))
