"""Long-term channel breakouts: closes above the top line of a 250+ day channel (bin/chan.py), daily, liquid pairs,
no look-ahead (channels re-detected every 10 days with the data known then). Entry at the next day's open.
Measures: price 30 / 90 / 180 days later, the best gain within 180 days (max high), how often it doubled (+100%) or
gained 50%, the worst drawdown before that peak, and the result vs the average liquid coin. Then splits by the
confirmations a trader would check at the breakout. Usage: python3 research/ltbo.py  (writes ltbo_stats.json)"""
import json
import statistics as st
from collections import defaultdict

import _env
import load
import bot_sim as S
from bot_core import day_num, excluded_tokens, segments
import chan as C

SPLIT, MIN_ADV, REFIT, MIN_BARS = day_num("2022-01-01"), 2e6, 10, 250
rows, tok = load.load(_env.path("daily_v2.csv.gz"), _env.path("pairs_v2.csv"))
data, reg = S.prepare(rows, tok)
excl = excluded_tokens(set(tok.values()))
base = defaultdict(list)
for s, F in data.items():
    if tok.get(s) in excl:
        continue
    o, c, dn, va = F["o"], F["c"], F["dn"], F["vavg20i"]
    for k in range(len(c) - 91):
        if va[k] and va[k] >= MIN_ADV and o[k + 1] > 0:
            base[dn[k]].append((c[k + 90] / o[k + 1] - 1) * 100)
bmed = {d: st.median(v) for d, v in base.items() if len(v) >= 10}
btc = data["BTCUSDT"]
btc_rs = {d: (btc["c"][k] / btc["c"][k - 30] - 1) for k, d in enumerate(btc["dn"]) if k >= 30}

ev = []
for s, F in data.items():
    if tok.get(s) in excl:
        continue
    for a, b in segments(F["dn"], F["c"]):
        if b - a < 400:
            continue
        o, h, l, c, v = (F[k][a:b] for k in ("o", "h", "l", "c", "v"))
        dn, va = F["dn"][a:b], F["vavg20i"][a:b]
        sma200, sma50, rsi = F["sma200"][a:b], F["sma50"][a:b], F["rsi14"][a:b]
        n, chans, last = len(c), [], -99
        for k in range(300, n - 1):
            if (k - 300) % REFIT == 0:
                chans = [f for f in C.find(h, l, c, k) if f["bars"] >= MIN_BARS] if va[k] and va[k] >= MIN_ADV else []
            if not chans or k - last < 30:
                continue
            for f in chans:
                stt = C.state(f, o, h, l, c, k)
                if not stt or stt["status"] != "breakout" or stt["event_i"] != k:
                    continue
                last = k
                ent = o[k + 1]
                if ent <= 0:
                    break
                end = min(n - 1, k + 180)
                hi180 = max(h[k + 1:end + 1])
                peak_i = max(range(k + 1, end + 1), key=lambda q: h[q])
                dd = min(l[k + 1:peak_i + 1]) / ent - 1
                lo, hi = C.lines(f, k)
                vavg = sum(v[k - 20:k]) / 20
                # weekly confirmation: the next weekly close (Sunday) still above the top line
                wk = next((q for q in range(k, min(n, k + 7)) if (dn[q] + 1) % 7 == 0), None)
                ev.append({"sym": s, "day": dn[k], "full": k + 180 < n,
                           "r30": (c[min(n - 1, k + 30)] / ent - 1) * 100, "r90": (c[min(n - 1, k + 90)] / ent - 1) * 100,
                           "r180": (c[end] / ent - 1) * 100, "max180": (hi180 / ent - 1) * 100, "dd": dd * 100,
                           "rel90": (c[min(n - 1, k + 90)] / ent - 1) * 100 - bmed[dn[k]] if dn[k] in bmed else None,
                           "bars": f["bars"], "touches": f["touch_lo"] + f["touch_hi"], "slope": f["m"] / ((lo + hi) / 2) * 100,
                           "width": 100 * f["w"] / lo if lo > 0 else None, "rvol": v[k] / vavg if vavg else None,
                           "above200": sma200[k] is not None and c[k] > sma200[k], "golden": sma50[k] is not None and sma200[k] is not None and sma50[k] > sma200[k],
                           "rsi": rsi[k], "btc_up": reg.get(dn[k], 0) == 1,
                           "rs_btc": (c[k] / c[k - 30] - 1) - btc_rs.get(dn[k], 0) if k >= 30 else None,
                           "ext": (c[k] / hi - 1) * 100,
                           "weekly_hold": None if wk is None else c[wk] > C.lines(f, wk)[1]})
                break


def summ(L):
    L = [x for x in L if x["full"]]
    if len(L) < 12:
        return None
    return {"n": len(L), "med90": round(st.median(x["r90"] for x in L), 1), "win90": round(100 * sum(x["r90"] > 0 for x in L) / len(L)),
            "med180": round(st.median(x["r180"] for x in L), 1), "max180": round(st.median(x["max180"] for x in L), 1),
            "x2": round(100 * sum(x["max180"] >= 100 for x in L) / len(L)), "p50": round(100 * sum(x["max180"] >= 50 for x in L) / len(L)),
            "dd": round(st.median(x["dd"] for x in L), 1),
            "rel90": round(st.median(x["rel90"] for x in L if x["rel90"] is not None), 1)}


TESTS = {"all": lambda x: True, "rvol>=2": lambda x: (x["rvol"] or 0) >= 2, "rvol<2": lambda x: (x["rvol"] or 0) < 2,
         "above200": lambda x: x["above200"], "below200": lambda x: not x["above200"],
         "golden": lambda x: x["golden"], "btc_up": lambda x: x["btc_up"], "btc_down": lambda x: not x["btc_up"],
         "rs_btc>0": lambda x: (x["rs_btc"] or 0) > 0, "rsi55-75": lambda x: x["rsi"] is not None and 55 <= x["rsi"] <= 75,
         "rsi>75": lambda x: x["rsi"] is not None and x["rsi"] > 75, "weekly_hold": lambda x: x["weekly_hold"] is True,
         "weekly_fail": lambda x: x["weekly_hold"] is False, "falling_channel": lambda x: x["slope"] < -0.05,
         "rising_channel": lambda x: x["slope"] > 0.05, "365d+": lambda x: x["bars"] >= 365, "6+touches": lambda x: x["touches"] >= 6,
         "ext<10%": lambda x: x["ext"] < 10}
out = {}
for per, fn in (("early", lambda x: x["day"] < SPLIT), ("late", lambda x: x["day"] >= SPLIT)):
    out[per] = {}
    for name, t in TESTS.items():
        out[per][name] = summ([x for x in ev if fn(x) and t(x)])
        print(f"{per:5s} {name:16s} {out[per][name]}")
# the combination a trader would demand
combo = lambda x: (x["rvol"] or 0) >= 2 and x["above200"] and x["btc_up"] and (x["rs_btc"] or 0) > 0   # noqa: E731
for per, fn in (("early", lambda x: x["day"] < SPLIT), ("late", lambda x: x["day"] >= SPLIT)):
    out[per]["confirmed"] = summ([x for x in ev if fn(x) and combo(x)])
    print(f"{per:5s} {'confirmed':16s} {out[per]['confirmed']}")
with open(_env.path("ltbo_stats.json"), "w") as f:
    json.dump(out, f, separators=(",", ":"))
with open(_env.path("ltbo_events.json"), "w") as f:
    json.dump(ev, f)
