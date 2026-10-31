"""
Calls lab (Bilal, 31 Oct 2026): what makes a call worth posting? Reads the server's full daily history (read-only).

Part 1, daily divergences (bin/diverge.py): every bullish / bearish divergence on liquid pairs (20-day average volume
$2M+), judged from the day it is known:
  * excess return vs the average liquid pair over 10 / 30 / 60 days, win rate;
  * does it mark the bottom? share where the divergence low (high) was NOT broken in the next 30 days;
  * breadth: days when many coins show a fresh bullish divergence, and BTC's next 30 days.
Part 2, call rules: candidate entries are the channel entries the site uses (bin/chan.py, refit every REFIT days, no
look-ahead) plus divergence entries. Every candidate is traded under each rule set (stop placement, max stop
distance, minimum reward : risk to target 1, filters): entry at the close, stop or target 1 first (stop first when
both fall in one day), else closed after HOLD days, 0.2% costs. Ranked on 2022 onward with the proven signals chosen
on data up to 2021 only (out of sample). Writes data/calls_lab.json and prints a summary.

    python3 research/calls_lab.py [DB]        (server: ops call-lab, read with call-lab-log)
"""
import itertools
import json
import os
import sqlite3
import statistics as st
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

import _env  # noqa: F401
import chan as C
import diverge as DV
from shortlist_study import proven_sets

REFIT, COST, MIN_ADV, HOLD, SPLIT = 10, 0.002, 2e6, 60, "2022-01-01"


def trade(o, h, l, c, k, side, stop, t1, hold=HOLD):
    """(return after costs, R, days held, how it ended) for an entry at close k."""
    sg, e = (1 if side == "long" else -1), c[k]
    risk = abs(e - stop)
    for q in range(k + 1, min(len(c), k + hold + 1)):
        if (sg == 1 and l[q] <= stop) or (sg == -1 and h[q] >= stop):
            out = min(o[q], stop) if sg == 1 else max(o[q], stop)
            return sg * (out / e - 1) - COST, sg * (out - e) / risk, q - k, "stop"
        if (sg == 1 and h[q] >= t1) or (sg == -1 and l[q] <= t1):
            return sg * (t1 / e - 1) - COST, sg * (t1 - e) / risk, q - k, "target"
    q = min(len(c) - 1, k + hold)
    return sg * (c[q] / e - 1) - COST, sg * (c[q] - e) / risk, q - k, "time"


def summ(T):
    if not T:
        return None
    yrs = defaultdict(list)
    for x in T:
        yrs[x[2][:4]].append(x[1])
    r, R = [x[0] for x in T], [x[1] for x in T]
    pos, neg = sum(x for x in r if x > 0), -sum(x for x in r if x < 0)
    return {"n": len(T), "win": round(100 * sum(x > 0 for x in r) / len(r), 1), "avg_pct": round(100 * st.mean(r), 2),
            "avg_r": round(st.mean(R), 3), "pf": round(pos / neg, 2) if neg else None,
            "months": len({x[2][:7] for x in T}),
            "years": {y: [len(v), round(st.mean(v), 2), round(100 * sum(r > 0 for r in v) / len(v))] for y, v in sorted(yrs.items())},
            "worst_year": round(min(st.mean(v) for v in yrs.values() if len(v) >= 8), 2) if any(len(v) >= 8 for v in yrs.values()) else None}


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_env.HERE, "..", "data", "binance.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    t0 = time.time()
    print("loading ...", flush=True)
    bs = {(s, d): b for s, d, b in con.execute("SELECT symbol, period_start, bull_score FROM indicators WHERE tf='1d' AND complete=1")}
    sets = {lab: proven_sets(con, early) for lab, early in (("full", False), ("early", True))}
    want = set().union(*[a | b for a, b, _ in sets.values()])
    sig = defaultdict(list)
    for s, tf, ps, name, d in con.execute("SELECT symbol, tf, period_start, signal, direction FROM signals WHERE warm=1 AND provisional=0 AND tf IN ('1d','1w')"):
        if (tf, name) in want:
            known = ps if tf == "1d" else (date.fromisoformat(ps) + timedelta(days=6)).isoformat()
            sig[s].append((known, tf, name, d))
    syms = [s for (s,) in con.execute("SELECT symbol FROM pairs")]
    btc = {d: cl for d, cl in con.execute("SELECT date, close FROM daily WHERE symbol='BTCUSDT' ORDER BY date")}
    bd = sorted(btc)
    bema, e = {}, None
    for d in bd:
        e = btc[d] if e is None else 2 / 51 * btc[d] + (1 - 2 / 51) * e
        bema[d] = btc[d] > e                                          # BTC above its 50-day EMA
    base = {"n": 0, "up30": 0, "down30": 0, "held30": 0}
    day_ret = defaultdict(dict)        # date -> {sym: daily return} for liquid pairs (the market average)
    divs, cands = [], []
    for n_sym, s in enumerate(syms):
        d = con.execute("SELECT date, open, high, low, close, volume_usdt FROM daily WHERE symbol=? ORDER BY date", (s,)).fetchall()
        if len(d) < 400:
            continue
        dates = [x[0] for x in d]
        o, h, l, c, v = ([x[k] or 0.0 for x in d] for k in range(1, 6))
        if min(c) <= 0:
            continue
        adv = [0.0] * len(c)
        run = 0.0
        for k in range(len(c)):
            run += v[k] - (v[k - 20] if k >= 20 else 0)
            adv[k] = run / min(k + 1, 20)
        for k in range(1, len(c)):
            if adv[k - 1] >= MIN_ADV:
                day_ret[dates[k]][s] = c[k] / c[k - 1] - 1
            if adv[k] >= MIN_ADV and k + 30 < len(c) and dates[k] >= SPLIT:      # baseline: any liquid coin-day, 2022 on
                base["n"] += 1
                base["up30"] += c[k + 30] > c[k]
                base["down30"] += c[k + 30] < c[k]
                base["held30"] += min(l[k + 1:k + 31]) >= l[k]
        first = len(cands)
        r = DV.rsi(c)
        hist = DV.macd(c)[2]
        ev = DV.events(o, h, l, c, r, hist, start=250)
        recent_div = {}
        for x in ev:
            k = x["i"]
            if adv[k] < MIN_ADV or k + 1 >= len(c):
                continue
            sg = 1 if x["side"] == "bull" else -1
            fw = {hz: max(-0.95, min(3.0, c[k + hz] / c[k] - 1)) if k + hz < len(c) else None for hz in (10, 30, 60)}   # capped: one listing pump cannot dominate
            held = (min(l[k + 1:k + 31]) >= x["px_j"]) if sg == 1 and k + 30 < len(c) else \
                   (max(h[k + 1:k + 31]) <= x["px_j"]) if sg == -1 and k + 30 < len(c) else None
            divs.append({"s": s, "d": dates[k], "side": x["side"], "kind": x["kind"], "candle": x["candle"], "rsi_p": x["rsi_p"],
                         "fw": fw, "held": held, "btc_up": bema.get(dates[k])})
            recent_div.setdefault(k, []).append(x)
            if sg == 1:                                               # divergence as an entry: stop under its low, target the swing high
                stop, t1 = x["px_j"] * 0.99, x["swing"]
                cands.append({"src": "div_" + x["kind"], "s": s, "d": dates[k], "side": "long", "status": "divergence",
                              "e": c[k], "stop_line": stop, "stop_tight": stop, "stop_swing": min(l[k - 4:k + 1]) * 0.995, "t1": t1,
                              "bs": bs.get((s, dates[k])), "div": x["kind"], "candle": x["candle"], "touch": True,
                              "btc_up": bema.get(dates[k]), "k": k, "sym_i": n_sym})
        chans, last = [], -99
        for k in range(300, len(c) - 1):
            if (k - 300) % REFIT == 0:
                chans = C.find(h, l, c, k) if adv[k] >= MIN_ADV else []
            if not chans or adv[k] < MIN_ADV or k - last < 5:
                continue
            for f in chans:
                stt = C.state(f, o, h, l, c, k)
                if not stt or stt["status"] not in ("near_bottom", "bottom_bounce", "mid_retest", "mid_loss", "top_reject"):
                    continue
                side = "short" if stt["status"] in ("mid_loss", "top_reject") else "long"
                lo, hi = C.lines(f, k)
                w, mid = hi - lo, (lo + hi) / 2
                if side == "long":
                    line = lo if stt["status"] != "mid_retest" else mid
                    t1 = mid if c[k] < mid - 0.05 * w else hi if c[k] < hi - 0.05 * w else hi + w / 2
                    stops = (line - 0.1 * w, line - 0.03 * w, min(l[k - 4:k + 1]) * 0.995)
                    lo3 = [C.lines(f, k - z)[0] for z in range(3)]
                    touch = any(l[k - z] <= lo3[z] * 1.015 for z in range(3)) if stt["status"] != "mid_retest" else \
                        any(l[k - z] <= ((lambda a, b: (a + b) / 2)(*C.lines(f, k - z))) * 1.015 for z in range(3))
                else:
                    line = mid if stt["status"] == "mid_loss" else hi
                    t1 = mid if c[k] > mid + 0.05 * w else lo
                    stops = (line + 0.1 * w, line + 0.03 * w, max(h[k - 4:k + 1]) * 1.005)
                    touch = True
                near = [x for z in range(0, 11) for x in recent_div.get(k - z, []) if (x["side"] == "bull") == (side == "long")]
                cands.append({"src": "chan", "s": s, "d": dates[k], "side": side, "status": stt["status"], "e": c[k],
                              "stop_line": stops[0], "stop_tight": stops[1], "stop_swing": stops[2], "t1": t1,
                              "bs": bs.get((s, dates[k])), "div": (near[0]["kind"] if near else None), "candle": c[k] > o[k] if side == "long" else c[k] < o[k],
                              "touch": touch, "btc_up": bema.get(dates[k]), "k": k, "sym_i": n_sym, "quality": f.get("quality") if isinstance(f, dict) else None})
                last = k
                break
        # outcomes for this symbol's candidates under every stop mode (the grid below only filters)
        for x in cands[first:]:
            x["agree"] = {}
            for lab, (bull, bear, _) in sets.items():
                want_d = "bullish" if x["side"] == "long" else "bearish"
                pset = bull if x["side"] == "long" else bear
                frm = dates[max(0, x["k"] - 2)]
                wk_from = (date.fromisoformat(x["d"]) - timedelta(days=7)).isoformat()
                x["agree"][lab] = sum(1 for kn, tf, nm, dd in sig.get(s, []) if dd == want_d and (tf, nm) in pset and kn <= x["d"] and
                                      ((tf == "1d" and kn >= frm) or (tf == "1w" and kn > wk_from)))
            x["res"] = {}
            for mode in ("line", "tight", "swing"):
                stop = x["stop_" + mode]
                sg = 1 if x["side"] == "long" else -1
                risk = (x["e"] - stop) * sg
                if risk <= 0 or (x["t1"] - x["e"]) * sg <= 0:
                    continue
                room = (x["t1"] - x["e"]) * sg / risk          # reward : risk to the channel's next line (or the swing)
                for tgt, tp in (("line", x["t1"]), ("2r", x["e"] + sg * 2 * risk), ("3r", x["e"] + sg * 3 * risk)):
                    x["res"][(mode, tgt)] = trade(o, h, l, c, x["k"], x["side"], stop, tp) + (risk / x["e"], room)
        if n_sym % 50 == 0:
            print(f"  {n_sym}/{len(syms)} pairs, {len(divs)} divergences, {len(cands)} candidates, {time.time() - t0:.0f}s", flush=True)
    mkt = {d: st.median(m.values()) for d, m in day_ret.items() if m}      # median: a freak candle cannot move "the market"
    md = sorted(mkt)
    cum, acc = {}, 1.0
    for d in md:
        acc *= 1 + mkt[d]
        cum[d] = acc
    out = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "split": SPLIT, "hold": HOLD, "cost": COST}

    # ---------------- part 1: divergences
    if base["n"]:
        out["baseline"] = {k: round(100 * base[k] / base["n"], 1) for k in ("up30", "down30", "held30")}
        print(f"\n# BASELINE (any liquid coin-day, 2022 on, n {base['n']}): up after 30 days {out['baseline']['up30']}%, down {out['baseline']['down30']}%, "
              f"that day's low not broken in 30 days {out['baseline']['held30']}%", flush=True)
    print("\n# PART 1: daily divergences (liquid pairs), excess vs the average liquid pair; 'held' = the divergence low (high) not broken in 30 days")
    idx = {d: k for k, d in enumerate(md)}

    def mret(d, hz):
        k = idx.get(d)
        return cum[md[min(len(md) - 1, k + hz)]] / cum[d] - 1 if k is not None and k + hz < len(md) else None
    P1 = {}
    for side, kind, extra in itertools.product(("bull", "bear"), ("rsi", "triple", "any"), ("all", "candle", "btc_up", "btc_down")):
        for per, f in (("pre", lambda x: x["d"] < SPLIT), ("oos", lambda x: x["d"] >= SPLIT)):
            T = [x for x in divs if x["side"] == side and f(x) and (kind == "any" or x["kind"] == kind) and
                 (extra == "all" or (extra == "candle" and x["candle"]) or (extra == "btc_up" and x["btc_up"]) or (extra == "btc_down" and x["btc_up"] is False))]
            row = {"n": len(T)}
            for hz in (10, 30, 60):
                ex = [x["fw"][hz] - mret(x["d"], hz) for x in T if x["fw"][hz] is not None and mret(x["d"], hz) is not None]
                raw = [x["fw"][hz] for x in T if x["fw"][hz] is not None]
                if ex:
                    sg = 1 if side == "bull" else -1
                    row[f"ex{hz}"] = round(100 * st.mean(ex), 2)
                    row[f"win{hz}"] = round(100 * sum(sg * z > 0 for z in raw) / len(raw), 1)
            hl = [x["held"] for x in T if x["held"] is not None]
            row["held30"] = round(100 * sum(hl) / len(hl), 1) if hl else None
            P1[f"{side}|{kind}|{extra}|{per}"] = row
            if row["n"] >= 30:
                print(f"{side:4s} {kind:6s} {extra:8s} {per:3s} n {row['n']:6d}  ex10 {row.get('ex10', 0):+6.2f}%  ex30 {row.get('ex30', 0):+6.2f}%  ex60 {row.get('ex60', 0):+6.2f}%  "
                      f"win30 {row.get('win30', 0):5.1f}%  held30 {row['held30']}%", flush=True)
    out["divergence"] = P1
    # breadth: share of liquid pairs with a fresh (5-day) bullish triple divergence; BTC's next 30 days
    fresh = defaultdict(set)
    for x in divs:
        if x["side"] == "bull" and x["kind"] == "triple":
            k = idx.get(x["d"])
            for z in range(5):
                if k is not None and k + z < len(md):
                    fresh[md[k + z]].add(x["s"])
    bres = []
    for k, d in enumerate(bd):
        if d in day_ret and len(day_ret[d]) >= 50 and k + 30 < len(bd):
            bres.append((len(fresh.get(d, ())) / len(day_ret[d]), btc[bd[k + 30]] / btc[d] - 1, d))
    if bres:
        qs = sorted(b[0] for b in bres)
        P1b = {}
        for lab, lo_q, hi_q in (("all days", 0, 1.01), ("top 10% breadth", 0.9, 1.01), ("top 5% breadth", 0.95, 1.01), ("no fresh divergence", -1, 0)):
            lo_v = qs[int(lo_q * (len(qs) - 1))] if lo_q >= 0 else -1
            T = [b for b in bres if (b[0] >= lo_v if lo_q > 0 else True) and (b[0] == 0 if lo_q < 0 else True)]
            if T:
                P1b[lab] = {"days": len(T), "btc30": round(100 * st.mean(b[1] for b in T), 2), "btc_win30": round(100 * sum(b[1] > 0 for b in T) / len(T), 1),
                            "min_share": round(min(b[0] for b in T), 3)}
                print(f"breadth {lab:22s} days {len(T):5d}  BTC next 30d {P1b[lab]['btc30']:+6.2f}%  up {P1b[lab]['btc_win30']}%", flush=True)
        out["breadth"] = P1b

    # ---------------- part 2: call rules
    print("\n# PART 2: call rules, 2022 on, proven signals chosen on data up to 2021 (out of sample)")
    grid = []
    for src, mode, tgt, mr, rr, filt in itertools.product(("chan", "div_triple", "div_rsi", "chan+div"), ("line", "tight", "swing"), ("line", "2r", "3r"),
                                                     (0.06, 0.08, 0.10, 0.20), (1.5, 2.0, 2.5, 3.0),
                                                     ("base", "proven", "proven+bs", "touch", "proven+bs+touch", "proven+bs+touch+btc",
                                                      "div", "proven+div", "candle+btc")):
        for side in ("long", "short"):
            if side == "short" and (src != "chan" or "div" in filt or "touch" in filt):
                continue
            T = []
            for x in cands:
                if x["side"] != side or x["d"] < SPLIT or (mode, tgt) not in x["res"]:
                    continue
                if src == "chan" and x["src"] != "chan":
                    continue
                if src.startswith("div_") and x["src"] != src:
                    continue
                if src == "chan+div" and not (x["src"] == "chan" and x["div"]):
                    continue
                ret, R, days, how, risk, xrr = x["res"][(mode, tgt)]
                if risk > mr or xrr < rr:
                    continue
                ag = x["agree"].get("early", 0)
                bsv = x["bs"]
                f = filt
                if "proven" in f and ag < 1:
                    continue
                if "bs" in f.split("+") and (bsv is None or (side == "long" and bsv < 5) or (side == "short" and bsv > 5)):
                    continue
                if "touch" in f and not x["touch"]:
                    continue
                if "btc" in f and not ((x["btc_up"] is True) if side == "long" else (x["btc_up"] is False)):
                    continue
                if "div" in f.split("+") and not x["div"]:
                    continue
                if "candle" in f and not x["candle"]:
                    continue
                T.append((ret, R, x["d"], how))
            s_ = summ(T)
            if s_ and s_["n"] >= 40:
                grid.append(dict(s_, src=src, side=side, stop=mode, target=tgt, max_risk=mr, min_rr=rr, filt=filt))
    grid.sort(key=lambda g: -(g["avg_r"] * min(1.0, g["n"] / 150)))
    out["rules"] = grid
    cur = [g for g in grid if g["src"] == "chan" and g["stop"] == "line" and g["target"] == "line" and g["max_risk"] == 0.20 and g["min_rr"] == 1.5 and g["filt"] == "proven+bs"]
    for g in cur:
        print(f"CURRENT RULES {g['side']}: n {g['n']} win {g['win']}% avgR {g['avg_r']:+.3f} PF {g['pf']} avg {g['avg_pct']:+.2f}%", flush=True)
    print("\nbest rule sets (avg R, weighted toward 150+ calls), room to the next line 2R+:")
    shown = 0
    for g in grid:
        if g["min_rr"] < 2.0:
            continue
        print(f"  {g['side']:5s} {g['src']:10s} stop {g['stop']:5s} tp {g['target']:4s} max {100 * g['max_risk']:.0f}%  rr>={g['min_rr']}  {g['filt']:22s}  "
              f"n {g['n']:5d} ({g['months']} months)  win {g['win']:5.1f}%  avgR {g['avg_r']:+.3f}  PF {g['pf']}  avg {g['avg_pct']:+.2f}%", flush=True)
        shown += 1
        if shown >= 40:
            break
    print("\nmost comfortable (highest win rate) with avg R >= +0.25 and 100+ calls, room 2R+:")
    comf = sorted([g for g in grid if g["n"] >= 100 and g["avg_r"] >= 0.25 and g["min_rr"] >= 2.0], key=lambda g: (-g["win"], -g["avg_r"]))
    for g in comf[:30]:
        print(f"  {g['side']:5s} {g['src']:10s} stop {g['stop']:5s} tp {g['target']:4s} max {100 * g['max_risk']:.0f}%  rr>={g['min_rr']}  {g['filt']:22s}  "
              f"n {g['n']:5d} ({g['months']} months)  win {g['win']:5.1f}%  avgR {g['avg_r']:+.3f}  PF {g['pf']}  avg {g['avg_pct']:+.2f}%", flush=True)
    print("\nfixed 2R take-profit, best 15 (100+ calls):")
    for g in [g for g in grid if g["target"] == "2r" and g["n"] >= 100][:15]:
        print(f"  {g['side']:5s} {g['src']:10s} stop {g['stop']:5s} max {100 * g['max_risk']:.0f}%  rr>={g['min_rr']}  {g['filt']:22s}  "
              f"n {g['n']:5d}  win {g['win']:5.1f}%  avgR {g['avg_r']:+.3f}  PF {g['pf']}  avg {g['avg_pct']:+.2f}%", flush=True)
    print("\nshorts, best 12 (100+ calls):")
    for g in [g for g in grid if g["side"] == "short" and g["n"] >= 100][:12]:
        print(f"  short {g['src']:10s} stop {g['stop']:5s} tp {g['target']:4s} max {100 * g['max_risk']:.0f}%  rr>={g['min_rr']}  {g['filt']:22s}  "
              f"n {g['n']:5d}  win {g['win']:5.1f}%  avgR {g['avg_r']:+.3f}  PF {g['pf']}  avg {g['avg_pct']:+.2f}%", flush=True)
    print("\nshortlist with year-by-year avg R (n, avgR, win%), 2R+ room, 60+ calls, every year with 8+ calls positive:")
    steady = [g for g in grid if g["n"] >= 60 and g["min_rr"] >= 2.0 and g["worst_year"] is not None and g["worst_year"] > 0]
    steady.sort(key=lambda g: (-g["worst_year"], -g["avg_r"]))
    for g in steady[:40]:
        print(f"  {g['side']:5s} {g['src']:10s} stop {g['stop']:5s} tp {g['target']:4s} max {100 * g['max_risk']:.0f}%  rr>={g['min_rr']}  {g['filt']:20s} "
              f"n {g['n']:4d} win {g['win']:4.1f}% avgR {g['avg_r']:+.2f} PF {g['pf']} worst year {g['worst_year']:+.2f} | "
              + " ".join(f"{y}:{v[0]}/{v[1]:+.2f}/{v[2]}%" for y, v in g["years"].items()), flush=True)
    by_year = defaultdict(list)                      # the leading long rule, year by year (is it steady?)
    lead = next((g for g in grid if g["side"] == "long" and g["min_rr"] >= 2.0), None)
    if lead:
        for x in cands:
            key = (lead["stop"], lead["target"])
            if x["side"] != "long" or x["d"] < SPLIT or key not in x["res"]:
                continue
            if lead["src"] == "chan+div" and not (x["src"] == "chan" and x["div"]):
                continue
            if lead["src"] == "chan" and x["src"] != "chan":
                continue
            ret, R, _, _, risk, room = x["res"][key]
            if risk > lead["max_risk"] or room < lead["min_rr"] or ("touch" in lead["filt"] and not x["touch"]):
                continue
            if "candle" in lead["filt"] and not x["candle"]:
                continue
            if "btc" in lead["filt"] and x["btc_up"] is not True:
                continue
            by_year[x["d"][:4]].append(R)
        print("\nleading long rule by year: " + ", ".join(f"{y} n {len(v)} avgR {st.mean(v):+.2f} win {100 * sum(r > 0 for r in v) / len(v):.0f}%" for y, v in sorted(by_year.items())), flush=True)
    path = os.path.join(os.path.dirname(os.path.abspath(db)), "calls_lab.json")
    with open(path + ".tmp", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    print(f"# written {path} in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
