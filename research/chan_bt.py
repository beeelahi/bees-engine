"""Track record of the channel detector and its statuses (bin/chan.py), daily charts, liquid pairs, no look-ahead.

Channels are re-detected every REFIT candles with only the candles closed at that time (chan.find); every candle
in between is judged against those frozen lines with chan.state, exactly as the website does on the latest candle.
An event = a status worth acting on (all but inside / above_top / below_bottom), one per kind per pair per 10
candles. Results from the next day's open:
  med30 / win30   median 30-day move in the event's direction, share of cases that moved that way
  edge30          median of (move - the average liquid pair's move over the same days): the part not explained by
                  the market; beat30 = share of cases that beat the average pair
  target% / avgR  long events as trades: stop = bottom line - 10% of the width, target = top line - 10% of the
                  width (both moving with the channel), whichever first within 120 days; R capped at -1.5
Split: up to 2021 (early) and 2022 on (late). Usage:
  python3 research/chan_bt.py run WORKER NWORKERS     (writes chan_ev_<worker>.json to RESEARCH_DATA)
  python3 research/chan_bt.py report                  (writes chan_stats.json for bin/)
"""
import glob
import json
import os
import statistics as st
import sys
from collections import defaultdict

import _env
import load
import bot_sim as S
from bot_core import day_num, excluded_tokens, segments
import chan as C

REFIT, MIN_ADV = 10, 2e6
SPLIT = day_num("2022-01-01")
EVENTS = {"breakout": 1, "breakdown": -1, "mid_reclaim": 1, "mid_loss": -1, "mid_retest": 1, "bottom_bounce": 1,
          "near_bottom": 1, "near_top": -1, "top_reject": -1}


def data():
    rows, tok = load.load(_env.path("daily_v2.csv.gz"), _env.path("pairs_v2.csv"))
    d, reg = S.prepare(rows, tok)
    excl = excluded_tokens(set(tok.values()))
    return {s: F for s, F in d.items() if tok.get(s) not in excl}, reg


def run(wk, nw):
    D, reg = data()
    ev = []
    for s in sorted(D)[wk::nw]:
        F = D[s]
        for a, b in segments(F["dn"], F["c"]):
            if b - a < 400:
                continue
            o, h, l, c = (F[k][a:b] for k in ("o", "h", "l", "c"))
            dn, va = F["dn"][a:b], F["vavg20i"][a:b]
            n, last, chans, prev = len(c), {}, [], {}
            for k in range(250, n - 1):
                if (k - 250) % REFIT == 0:
                    chans = C.find(h, l, c, k) if va[k] and va[k] >= MIN_ADV else []
                    prev = {}
                if not chans or not va[k] or va[k] < MIN_ADV:
                    continue
                for ci, f in enumerate(chans):
                    stt = C.state(f, o, h, l, c, k)
                    if not stt:
                        continue
                    kind = stt["status"]
                    fresh = (stt["event_i"] == k) if stt["event_i"] is not None else prev.get(ci) != kind
                    prev[ci] = kind
                    if kind not in EVENTS or not fresh or k - last.get(kind, -99) < 10:
                        continue
                    last[kind] = k
                    ent, sg, w = o[k + 1], EVENTS[kind], f["w"]
                    if ent <= 0:
                        continue
                    r = {z: (c[min(n - 1, k + z)] / ent - 1) * 100 for z in (10, 30, 60)}
                    res, R = None, None
                    lo, hi = C.lines(f, k)
                    if sg == 1 and kind != "breakout" and lo - 0.1 * w > 0 and ent > lo - 0.1 * w:
                        risk, res = ent - (lo - 0.1 * w), "open"
                        for q in range(k + 1, min(n, k + 121)):
                            lq, hq = C.lines(f, q)
                            if l[q] <= lq - 0.1 * w:
                                res, R = "stop", (min(o[q], lq - 0.1 * w) - ent) / risk
                                break
                            if h[q] >= hq - 0.1 * w:
                                res, R = "target", (max(o[q], hq - 0.1 * w) - ent) / risk
                                break
                        if res == "open":
                            R = (c[min(n - 1, k + 120)] - ent) / risk
                    ev.append(dict(day=dn[k], kind=kind, sg=sg, r10=r[10], r30=r[30], r60=r[60], res=res, R=R, full=k + 60 < n,
                                   bars=f["bars"], touches=f["touch_lo"] + f["touch_hi"], width=100 * w / lo if lo > 0 else None,
                                   reg=reg.get(dn[k], 0)))
    with open(_env.path(f"chan_ev_{wk}.json"), "w") as fh:
        json.dump(ev, fh)
    print("worker", wk, "events", len(ev))


def summ(L):
    L = [x for x in L if x["full"]]
    if len(L) < 20:
        return None
    sg = L[0]["sg"]
    out = {"n": len(L), "med30": round(st.median(sg * x["r30"] for x in L), 1),
           "win30": round(100 * sum(1 for x in L if sg * x["r30"] > 0) / len(L)),
           "edge30": round(st.median(x["e30"] for x in L), 1), "beat30": round(100 * sum(1 for x in L if x["e30"] > 0) / len(L)),
           "med60": round(st.median(sg * x["r60"] for x in L), 1), "edge60": round(st.median(x["e60"] for x in L), 1)}
    T = [x for x in L if x["R"] is not None]
    if len(T) >= 20:
        out["target"] = round(100 * sum(1 for x in T if x["res"] == "target") / len(T))
        out["avg_r"] = round(st.mean(max(-1.5, x["R"]) for x in T), 2)
    return out


def report():
    D, reg = data()
    base = defaultdict(lambda: defaultdict(list))
    for F in D.values():
        o, c, dn, va = F["o"], F["c"], F["dn"], F["vavg20i"]
        for k in range(len(c) - 1):
            if va[k] and va[k] >= MIN_ADV and o[k + 1] > 0:
                for z in (30, 60):
                    if k + z < len(c):
                        base[dn[k]][z].append((c[k + z] / o[k + 1] - 1) * 100)
    med = {d: {z: st.median(v) for z, v in m.items()} for d, m in base.items()}
    ev = []
    for p in glob.glob(_env.path("chan_ev_*.json")):
        with open(p) as fh:
            ev += json.load(fh)
    ev = [e for e in ev if 30 in med.get(e["day"], {}) and 60 in med.get(e["day"], {})]
    for e in ev:
        e["e30"] = e["sg"] * (e["r30"] - med[e["day"]][30])
        e["e60"] = e["sg"] * (e["r60"] - med[e["day"]][60])
    groups = {"early": lambda x: x["day"] < SPLIT, "late": lambda x: x["day"] >= SPLIT,
              "late_long": lambda x: x["day"] >= SPLIT and x["bars"] >= 365,
              "late_btc_up": lambda x: x["day"] >= SPLIT and x["reg"] == 1,
              "late_btc_down": lambda x: x["day"] >= SPLIT and x["reg"] != 1,
              "late_narrow": lambda x: x["day"] >= SPLIT and (x["width"] or 999) < 60}
    out = {}
    for kind in EVENTS:
        out[kind] = {g: summ([e for e in ev if e["kind"] == kind and fn(e)]) for g, fn in groups.items()}
        for g, v in out[kind].items():
            print(f"{kind:14s} {g:13s} {v}")
    days = sorted(e["day"] for e in ev)
    from datetime import date
    off = SPLIT - date(2022, 1, 1).toordinal()
    day_str = lambda n: date.fromordinal(n - off).isoformat()   # noqa: E731
    out["_meta"] = {"from": day_str(days[0]) if days else None, "to": day_str(days[-1]) if days else None,
                    "events": len(ev), "refit": REFIT, "min_adv": MIN_ADV}
    with open(_env.path("chan_stats.json"), "w") as fh:
        json.dump(out, fh, separators=(",", ":"))


if __name__ == "__main__":
    if sys.argv[1] == "run":
        run(int(sys.argv[2]), int(sys.argv[3]))
    else:
        report()
