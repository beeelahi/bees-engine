"""Long-term channel breakouts (ltbo_events.json from ltbo.py): the score of measured confirmations, entries after the
weekly close confirms (no look-ahead), and exit plans. Writes ltbo_exits.json."""
import json
import statistics as st

import _env
import load
import bot_sim as S
from bot_core import day_num

SPLIT = day_num("2022-01-01")
rows, tok = load.load(_env.path("daily_v2.csv.gz"), _env.path("pairs_v2.csv"))
data, _ = S.prepare(rows, tok)
ev = json.load(open(_env.path("ltbo_events.json")))


def score(e):
    return int(e["weekly_hold"] is True) + int(e["rsi"] is not None and 55 <= e["rsi"] <= 75) + int(e["slope"] <= 0.05) + int(e["ext"] < 10)


def plans(F, i0):
    """Entry at the open after candle i0; returns {plan: return %}."""
    o, h, l, c = F["o"], F["h"], F["l"], F["c"]
    n = len(c)
    ent = o[i0 + 1]
    out = {"hold90": (c[min(n - 1, i0 + 90)] / ent - 1) * 100}
    for name, trail, half_at in (("trail25", 0.25, None), ("half50_trail25", 0.25, 0.5), ("trail35", 0.35, None)):
        peak, sold, part, res = ent, 0.0, 0.0, None
        for q in range(i0 + 1, min(n, i0 + 366)):
            if half_at and sold == 0 and h[q] >= ent * (1 + half_at):
                sold, part = 0.5, 0.5 * half_at
            peak = max(peak, h[q])
            if c[q] < peak * (1 - trail):
                res = part + (1 - sold) * (c[q] / ent - 1)
                break
        if res is None:
            res = part + (1 - sold) * (c[min(n - 1, i0 + 365)] / ent - 1)
        out[name] = res * 100
    return out


R = {}
for e in ev:
    F = data[e["sym"]]
    k = F["dn"].index(e["day"])
    if k + 400 >= len(F["c"]):
        continue
    wk = next((q for q in range(k, k + 7) if (F["dn"][q] + 1) % 7 == 0), None)
    if wk is None:
        continue
    per = "early" if e["day"] < SPLIT else "late"
    sc = score(e)
    grp = "4" if sc == 4 else "3" if sc == 3 else "0-2"
    for entry, i0 in (("breakout", k), ("weekly", wk)):
        if entry == "weekly" and e["weekly_hold"] is not True:
            continue
        p = plans(F, i0)
        for name, v in p.items():
            R.setdefault(per, {}).setdefault(grp, {}).setdefault(entry, {}).setdefault(name, []).append(v)
out = {}
for per, G in R.items():
    for grp, E in sorted(G.items()):
        for entry, P in E.items():
            for name, L in P.items():
                if len(L) < 8:
                    continue
                d = {"n": len(L), "avg": round(st.mean(L), 1), "med": round(st.median(L), 1), "win": round(100 * sum(x > 0 for x in L) / len(L))}
                out.setdefault(per, {}).setdefault(grp, {}).setdefault(entry, {})[name] = d
                print(f"{per:5s} score {grp:4s} {entry:9s} {name:15s} {d}")
with open(_env.path("ltbo_exits.json"), "w") as f:
    json.dump(out, f, separators=(",", ":"))
