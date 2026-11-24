"""STF HNS lab: which head-and-shoulders shapes actually pay, on XAU, XAG, BTC, ETH and SOL, 15m / 1h / 4h / 1d
(5m was tested on 24 Nov 2026 and lost on every variant).

Each variant changes one or two of the watcher's shape rules (bin/stf_hns.py constants) and is tested on the same
history, the same way: every pattern the rules accept, the 'Neckline broken' alert as the watcher would send it (the
first candle the complete pattern is known AND has closed through its neckline, within BREAK_FRESH candles), entry at
the next open, the measured target, the stop beyond the right shoulder (and, second, the tight neckline stop).

Guarding against fitting the rules to chance:
  * every series is split by time: the first two thirds ("earlier") and the last third ("later"); a variant only
    counts as better if it beats the live rules in BOTH halves, on enough trades;
  * the standard error of the average R is printed next to it.
Bilal's reference patterns are checked for each variant (caught or not): gold 15m 20 Nov 2026 (head 10:30 UTC),
BTC 1h 21-24 Nov (head 23 Nov 14:00). Results are also broken down by market.

The direct scan is checked against the watcher's own candle-by-candle replay (stf_hns.replay_breaks) on the 4-hour
series, so the shortcut is known to give the same alerts.

Writes data/stf_lab.json and prints the report. Read-only on everything else. Usage: python3 research/stf_lab.py
"""
import json
import os
import statistics as st
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin"))
import hs as HS  # noqa: E402
import stf_hns as S  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CACHE = os.path.join(ROOT, "data", "stf_cache")
OUT = os.path.join(ROOT, "data", "stf_lab.json")
BARS = {"15m": 70_000, "1h": 26_000, "4h": 6_600, "1d": 3_000}     # about 2, 3, 3 and 8 years (5m was tested and lost)
TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
SPLIT = 2 / 3

LIVE = {k: getattr(S, k) for k in ("SHOULDER_MIN", "SYM_MAX", "TIME_RATIO", "NECK_AGAINST", "NECK_WITH", "PRIOR_MIN",
                                   "ATR_MIN", "WIDTH_MIN", "SIDE_MIN", "HEAD_CLOSE", "HEAD_MIN", "TROUGH_MAX", "SPAN")}
VARIANTS = [
    ("live", "The live rules", {}),
    ("spike_head", "Spike head: right shoulder >= 25% of height, time ratio 0.3-3", {"SHOULDER_MIN": 0.25, "TIME_RATIO": (0.3, 3.0)}),
    ("shoulder_35", "Shoulders >= 35% of height (live 50%)", {"SHOULDER_MIN": 0.35}),
    ("shoulder_65", "Shoulders >= 65% of height", {"SHOULDER_MIN": 0.65}),
    ("time_3", "Time ratio 0.33-3 (live 0.5-2)", {"TIME_RATIO": (0.33, 3.0)}),
    ("time_15", "Time ratio 0.67-1.5 (more even)", {"TIME_RATIO": (0.67, 1.5)}),
    ("sym_15", "Shoulders within 15% of each other (live 25%)", {"SYM_MAX": 0.15}),
    ("sym_35", "Shoulders within 35% of each other", {"SYM_MAX": 0.35}),
    ("head_10", "Head >= 10% above the shoulders (live 20%)", {"HEAD_MIN": 0.10}),
    ("head_30", "Head >= 30% above the shoulders", {"HEAD_MIN": 0.30}),
    ("size_15", "Height >= 1.5x typical candle (live 2x)", {"ATR_MIN": 1.5}),
    ("size_3", "Height >= 3x typical candle", {"ATR_MIN": 3.0}),
    ("size_5", "Height >= 5x typical candle", {"ATR_MIN": 5.0}),
    ("width_12", "Width >= 12 candles (live 20)", {"WIDTH_MIN": 12, "SIDE_MIN": 4}),
    ("width_30", "Width >= 30 candles", {"WIDTH_MIN": 30, "SIDE_MIN": 10}),
    ("prior_any", "No prior-move rule (continuation patterns too)", {"PRIOR_MIN": -1e9}),
    ("prior_1", "Prior move >= 1 height (live 0.5)", {"PRIOR_MIN": 1.0}),
    ("neck_with", "Neckline only with the trade or flat (0 degrees against)", {"NECK_AGAINST": 0.0}),
    ("neck_15", "Neckline up to ~11 degrees against (0.2)", {"NECK_AGAINST": 0.2}),
    ("no_head_close", "Head may be a wick (no closes rule)", {"HEAD_CLOSE": False}),
    ("span_300", "Up to 300 candles wide (live 200)", {"SPAN": 300}),
]
REFS = [("XAU", "15m", "2026-11-20 10:30", "Gold 15m 20 Nov (your trade)"),
        ("BTC", "1h", "2026-11-23 14:00", "BTC 1h 21-24 Nov (spike head)"),
        ]


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def history(code, tf, log=print):
    """Long closed-candle history, cached on disk (the newest candles are refetched each run)."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{code}_{tf}.json")
    src, sym = S.source_for(code, log)
    if not src:
        return None, None
    old = []
    try:
        with open(path) as f:
            d = json.load(f)
        if d.get("sym") == sym:
            old = [tuple(r) for r in d["rows"]]
    except (OSError, ValueError, KeyError):
        pass
    if len(old) >= BARS[tf] * 0.9:
        new = S.long_history(src, sym, tf, 3000)
        rows = old + [r for r in new if r[0] > old[-1][0]]
    else:
        rows = S.long_history(src, sym, tf, BARS[tf])
    rows = rows[-BARS[tf]:]
    with open(path + ".tmp", "w") as f:
        json.dump({"sym": sym, "rows": rows}, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    return rows, f"{sym} ({S.SOURCE_NAME[src]})"


def direct_breaks(rows):
    """Every break alert the watcher would send, from one scan of the whole series (no look-ahead: a pattern is only
    used from its 'known' candle on, and its break is read candle by candle from there)."""
    t, o, h, l, c = S._series(rows)
    out, done = [], set()
    for P in S.P_SIZES:
        for p in S.find(h, l, c, P):
            if (p["kind"], p["hd"]) in done:
                continue
            ext = h if p["sg"] == 1 else l
            for i in range(p["known"], min(len(c), p["rsh"] + HS.MAX_WAIT + 2)):
                s = HS.state(p, o, h, l, c, i)
                if s is None:
                    break
                if s["brk"] is not None:
                    if s["status"] == "broken":
                        done.add((p["kind"], p["hd"]))
                        out.append({"n": i, "kind": p["kind"], "context": p["context"], "sg": p["sg"], "target": s["target"],
                                    "stop_shoulder": ext[p["rsh"]], "stop_neck": s["neck"] + p["sg"] * HS.FAIL * p["height"],
                                    "bar_ms": t[i], "head_ms": t[p["hd"]], "P": P})
                    break
    return out


def stats(rs):
    if not rs:
        return {"n": 0}
    return {"n": len(rs), "avg_r": round(st.mean(rs), 3), "se": round(st.stdev(rs) / len(rs) ** 0.5, 3) if len(rs) > 1 else None,
            "win_pct": round(100 * sum(r > 0 for r in rs) / len(rs), 1), "sum_r": round(sum(rs), 1)}


def evaluate(hist, log=print):
    res = {}
    for key, desc, over in VARIANTS:
        for k, v in LIVE.items():
            setattr(S, k, v)
        for k, v in over.items():
            setattr(S, k, v)
        t0 = time.time()
        per_tf, refs, res_mk = {}, {}, {}
        for (code, tf), rows in hist.items():
            br = direct_breaks(rows)
            cut = rows[0][0] + SPLIT * (rows[-1][0] - rows[0][0])
            mk = res_mk.setdefault(f"{code} {tf}", [])
            d = per_tf.setdefault(tf, {"shoulder": [], "neck": [], "early": [], "late": [], "target": [], "target_65": []})
            for b in br:
                x = S.trade(rows, b, "stop_shoulder")
                y = S.trade(rows, b, "stop_neck")
                if x:
                    mk.append(x["r"])
                    d["shoulder"].append(x["r"])
                    d["early" if b["bar_ms"] < cut else "late"].append(x["r"])
                    d["target"].append(x["exit"] == "target")
                if y:
                    d["neck"].append(y["r"])
            for code_r, tf_r, head, _ in REFS:
                if (code_r, tf_r) == (code, tf):
                    hit = [b for b in br if utc(b["head_ms"]) == head]
                    refs[f"{code_r} {tf_r} {head}"] = bool(hit)
        res[key] = {"desc": desc, "over": {k: list(v) if isinstance(v, tuple) else v for k, v in over.items()}, "refs": refs, "tf": {}}
        for tf, d in per_tf.items():
            res[key]["tf"][tf] = {"shoulder": stats(d["shoulder"]), "neck": stats(d["neck"]), "earlier": stats(d["early"]),
                                  "later": stats(d["late"]),
                                  "target_pct": round(100 * sum(d["target"]) / len(d["target"]), 1) if d["target"] else None}
        allr = [r for d in per_tf.values() for r in d["shoulder"]]
        res[key]["all"] = stats(allr)
        res[key]["market"] = {k: stats(v) for k, v in res_mk.items()}
        log(f"lab {key}: {res[key]['all']} ({time.time() - t0:.0f}s)")
    for k, v in LIVE.items():
        setattr(S, k, v)
    return res


def exits_study(hist):
    """On the live rules: entry at the break vs a limit order back at the neckline; full vs 65% target."""
    out = {}
    for (code, tf), rows in hist.items():
        d = out.setdefault(tf, {"break_full": [], "break_65": [], "retest_full": [], "retest_65": []})
        for b in direct_breaks(rows):
            sg, n = b["sg"], b["n"]
            # the neckline at the break: stop_neck = neck + sg*FAIL*H and target = neck - sg*H
            H = abs(b["stop_neck"] - b["target"]) / (1 + HS.FAIL)
            neck = b["target"] + sg * H
            t65 = neck - sg * 0.65 * H
            for name, tgt in (("full", b["target"]), ("65", t65)):
                x = S.trade(rows, dict(b, target=tgt), "stop_shoulder")
                if x:
                    d[f"break_{name}"].append(x["r"])
                # limit at the neckline within 10 candles after the break, then the same rules from that candle
                for k in range(n + 1, min(len(rows) - 1, n + 11)):
                    if (sg == 1 and rows[k][2] >= neck) or (sg == -1 and rows[k][3] <= neck):
                        e, stop = neck, b["stop_shoulder"]
                        risk = sg * (stop - e)
                        if risk <= 0:
                            break
                        r = None
                        if (sg == 1 and rows[k][2] >= stop) or (sg == -1 and rows[k][3] <= stop):
                            r = -1.0
                        for j in range(k + 1, min(len(rows), k + HS.MAX_AGE)):
                            if r is not None:
                                break
                            if (sg == 1 and rows[j][2] >= stop) or (sg == -1 and rows[j][3] <= stop):
                                r = max(-1.5, sg * (e - (max(rows[j][1], stop) if sg == 1 else min(rows[j][1], stop))) / risk)
                            elif (sg == 1 and rows[j][3] <= tgt) or (sg == -1 and rows[j][2] >= tgt):
                                r = sg * (e - tgt) / risk
                        if r is None:
                            r = max(-1.5, sg * (e - rows[min(len(rows) - 1, k + HS.MAX_AGE)][4]) / risk)
                        d[f"retest_{name}"].append(round(r, 3))
                        break
    return {tf: {k: stats(v) for k, v in d.items()} for tf, d in out.items()}


def replay_check(hist, log=print):
    """The direct scan against the watcher's own replay on the 4-hour series (same alerts expected)."""
    out = {}
    for (code, tf), rows in hist.items():
        if tf != "4h":
            continue
        a = {(b["kind"], b["bar_ms"]) for b in direct_breaks(rows) if b["n"] >= S.REPLAY_W}   # the replay starts there
        r = {(b["kind"], b["bar_ms"]) for b in S.replay_breaks(rows, code, tf)}
        out[code] = {"direct": len(a), "replay": len(r), "both": len(a & r)}
        log(f"lab check {code} 4h: direct {len(a)} replay {len(r)} same {len(a & r)}")
    return out


def main(log=print):
    t0 = time.time()
    hist, src = {}, {}
    for code, _ in S.INSTR:
        for tf in BARS:
            try:
                rows, name = history(code, tf, log)
            except Exception as e:  # noqa: BLE001 - one market failing must not stop the study
                log(f"lab {code} {tf}: data failed ({type(e).__name__}: {e})")
                continue
            if rows and len(rows) > (300 if tf == "1d" else 700):
                hist[(code, tf)] = rows
                src[f"{code} {tf}"] = {"source": name, "bars": len(rows), "from": utc(rows[0][0]), "to": utc(rows[-1][0])}
                log(f"lab data {code} {tf}: {len(rows)} candles {utc(rows[0][0])} .. {utc(rows[-1][0])}")
    check = replay_check(hist, log)
    res = evaluate(hist, log)
    ex = exits_study(hist)
    out = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "data": src, "check": check,
           "variants": res, "exits": ex, "split": SPLIT, "minutes": round((time.time() - t0) / 60, 1)}
    S.save_json(OUT, out)
    report(out, log)
    return out


def report(out, log=print):
    log(f"=== STF HNS lab {out['generated_utc']} ({out['minutes']} min). R = result in units of risk, stop beyond the right shoulder.")
    live = out["variants"]["live"]
    for tf in BARS:
        log(f"--- {tf}: variant | trades | target% | avg R (+-se) | earlier | later | neck-stop avg R")
        for key, v in out["variants"].items():
            T = v["tf"].get(tf)
            if not T or not T["shoulder"].get("n"):
                continue
            a, e, la = T["shoulder"], T["earlier"], T["later"]
            L = live["tf"].get(tf, {})
            better = (key != "live" and e.get("n", 0) >= 15 and la.get("n", 0) >= 10 and L
                      and e.get("avg_r", -9) > L["earlier"].get("avg_r", 9) and la.get("avg_r", -9) > L["later"].get("avg_r", 9))
            log(f"{key:14s} {a['n']:5d} {T['target_pct']:6.1f} {a['avg_r']:+.2f} (+-{a['se'] or 0:.2f}) "
                f"{e.get('avg_r', 0):+.2f}/{e.get('n', 0)} {la.get('avg_r', 0):+.2f}/{la.get('n', 0)} {T['neck'].get('avg_r', 0):+.2f}"
                f"{'  <- better in both halves' if better else ''}")
    log("--- by market (avg R / trades): live | spike_head | shoulder_35")
    for mk in out["variants"]["live"]["market"]:
        log(f"{mk:9s} " + " | ".join(f"{out['variants'][v]['market'].get(mk, {}).get('avg_r', 0):+.2f}/{out['variants'][v]['market'].get(mk, {}).get('n', 0)}"
                                     for v in ("live", "spike_head", "shoulder_35")))
    log("--- reference patterns caught (gold 15m / BTC 1h spike):")
    for key, v in out["variants"].items():
        log(f"{key:14s} " + "  ".join(("YES" if x else "no ") for x in v["refs"].values()))
    log("--- entries and targets on the live rules (avg R / trades): break+full, break+65%, neckline-limit+full, neckline-limit+65%")
    for tf, d in out["exits"].items():
        log(f"{tf:4s} " + "  ".join(f"{k} {x.get('avg_r', 0):+.2f}/{x.get('n', 0)}" for k, x in d.items()))
    log("--- direct scan vs the watcher's replay (4h): " + json.dumps(out["check"]))


if __name__ == "__main__":
    if "--report" in sys.argv:
        with open(OUT) as f:
            report(json.load(f))
    else:
        main()
