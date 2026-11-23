"""
STF HNS: head-and-shoulders tops and inverse (bottoms) on short timeframes (15 minutes, 1 hour, 4 hours) for gold,
silver, BTC, ETH and SOL (Python 3.9 stdlib only). Runs every 5 minutes on its own timer (bees-stf.timer).

What it does each run
  * fetches the latest CLOSED candles (the candle still forming is never used) for every instrument and timeframe:
      gold    XAU  Binance XAUUSDT perpetual (falls back to PAXGUSDT spot, a gold-backed token, if it is unavailable)
      silver  XAG  Binance XAGUSDT perpetual
      BTC/ETH/SOL  Binance spot USDT pairs
  * finds patterns with the dashboard's own detector (bin/hs.py, the one behind the daily patterns), with swing sizes
    P = 5 and 10 candles and continuation patterns included (a top that forms after a fall, like a bearish flag),
    each tagged reversal or continuation;
  * adds an early stage the daily page does not have: "right shoulder forming": left shoulder, head and the first
    trough are confirmed and price has come back up (tops) / down (inverse) into the left shoulder's zone without
    passing the head. It becomes a full pattern once the right shoulder is confirmed (P candles later);
  * follows every pattern from first sighting to its outcome and keeps them all in a log (nothing is back-filled,
    so the log is an out-of-sample record of exactly what was shown):
      forming -> near the neckline -> neckline broken -> retest -> target reached | failed | expired
  * sends each new stage to Telegram once (when alerts are set up), and writes web_export/stf_hns.json for the page.

Levels (same rules as the daily patterns)
  neckline  straight line through the two troughs (tops) / peaks (inverse), extended
  height    head to the neckline, measured under the head
  target    neckline -/+ height from the break (the measured move)
  stop      before the break: beyond the right shoulder; after it: neckline + 35% of the height against the trade
  failed    right shoulder taken out before the break, or a close back through the neckline by 35% of the height
  expired   no break within 40 candles of the right shoulder, or no target within 60 candles of the break

Usage: python3 bin/stf_hns.py            one run (what the timer does)
       python3 bin/stf_hns.py --probe    data availability and the patterns in the last 1,000 candles (no writes)
       python3 bin/stf_hns.py --backtest the detector's historical record on long history (writes the stats)
"""
import json
import os
import statistics as st
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hs as HS  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "web_export", "stf_hns.json")
STATE = os.path.join(ROOT, "data", "stf_hns_state.json")
BTSTATS = os.path.join(ROOT, "data", "stf_hns_backtest.json")

INSTR = [("XAU", "Gold"), ("XAG", "Silver"), ("BTC", "Bitcoin"), ("ETH", "Ether"), ("SOL", "Solana")]
SOURCES = {"XAU": [("fapi", "XAUUSDT"), ("spot", "PAXGUSDT")], "XAG": [("fapi", "XAGUSDT")],
           "BTC": [("spot", "BTCUSDT")], "ETH": [("spot", "ETHUSDT")], "SOL": [("spot", "SOLUSDT")]}
SOURCE_NAME = {"fapi": "Binance USD-M perpetual", "spot": "Binance spot"}
URL = {"spot": "https://api.binance.com/api/v3/klines", "fapi": "https://fapi.binance.com/fapi/v1/klines"}
TFS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
LIMIT = 1000                 # candles fetched per series each run
P_SIZES = (5, 10)
SPAN = 200                   # longest pattern (left shoulder to right shoulder), candles
# Short-timeframe shape limits (daily page: 0.2 and 0.5). Intraday heads are often flatter: the gold 15-minute top of
# 20 Nov 2026 (the case in tests/test_stf_hns.py) had its head 16% of the height above the shoulders and troughs 54%
# apart. The backtest reports both settings side by side, so the cost of the looser limits is visible.
HEAD_MIN, TROUGH_MAX = 0.12, 0.6
CHART_BARS = 240             # candles per series sent to the page
BT_BARS = {"15m": 12000, "1h": 9000, "4h": 4000}   # backtest history: about 4 months, 1 year, 22 months
STAGE = {"forming": 0, "near_neckline": 1, "pattern": 1, "broken": 2, "retest": 3, "after_break": 3}
LABEL = {"forming": "Right shoulder forming", "pattern": "Pattern complete", "near_neckline": "Near the neckline",
         "broken": "Neckline broken", "retest": "Retesting the neckline", "after_break": "Broken, under way",
         "target": "Target reached", "failed": "Failed", "expired": "Expired"}


# ------------------------------------------------------------------ data
def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "BeesDashboard/stf"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def klines(src, sym, tf, limit=LIMIT, end_ms=None, now_ms=None):
    """Closed candles only: [(open_ms, o, h, l, c)], oldest first."""
    q = f"{URL[src]}?symbol={sym}&interval={tf}&limit={min(limit, 1500 if src == 'fapi' else 1000)}"
    if end_ms:
        q += f"&endTime={end_ms}"
    rows = _get(q)
    now_ms = now_ms or int(time.time() * 1000)
    return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows if int(r[6]) < now_ms]


def long_history(src, sym, tf, n):
    """Up to n closed candles, paging back in time."""
    out, end = [], None
    while len(out) < n:
        part = klines(src, sym, tf, 1000, end)
        if not part:
            break
        out = part + [x for x in out if x[0] > part[-1][0]]
        end = part[0][0] - 1
        if len(part) < 900:
            break
        time.sleep(0.2)
    return out[-n:]


def source_for(code, log=print):
    for src, sym in SOURCES[code]:
        try:
            if klines(src, sym, "1h", 3):
                return src, sym
        except Exception as e:  # noqa: BLE001 - try the next source
            log(f"stf: {code} {src} {sym} unavailable ({type(e).__name__})")
    return None, None


# ------------------------------------------------------------------ detection
def _series(rows):
    t = [r[0] for r in rows]
    o, h, l, c = ([r[k] for r in rows] for k in range(1, 5))
    return t, o, h, l, c


def forming(h, l, c, P):
    """Patterns whose right shoulder is not confirmed yet (a confirmed one needs P more candles; hs.find takes over
    then). The last two swing points are the left shoulder and the head; the right shoulder candidate is the highest
    point (tops; lowest for inverse) since the head that nothing later has exceeded, with the second trough the
    deepest point between the head and it. Same shape rules as the complete patterns. Each comes with its state:
    'forming' (price in the right-shoulder zone, no break yet) or 'broken' / 'after_break' when a close has already
    gone through the neckline, so a break is reported on its own candle, not P candles later. Returns [(p, state)]."""
    out, n, i = [], len(c), len(c) - 1
    for kind, X, Y, sg in (("top", h, l, 1), ("inverse", l, h, -1)):
        peaks = HS.pivots(X, P, sg == 1)
        pick = (lambda t: Y[t]) if sg == 1 else (lambda t: -Y[t])
        if len(peaks) < 2:
            continue
        lsh, hd = peaks[-2], peaks[-1]
        if hd - lsh < 3 or n - 1 - lsh > SPAN:
            continue
        Hd, Ls = sg * X[hd], sg * X[lsh]
        if not Hd > Ls or max(sg * X[k] for k in range(lsh, n)) > Hd:   # the head is the extreme of the whole pattern
            continue
        t1 = min(range(lsh + 1, hd), key=pick)
        cands, top_ = [], None                              # right-shoulder candidates: never exceeded afterwards
        for k in range(n - 1, hd + 1, -1):
            if top_ is None or sg * X[k] > top_:
                top_ = sg * X[k]
                cands.append(k)
        for rs_k in reversed(cands):                         # earliest (highest) first
            if not 0.4 * (hd - lsh) <= rs_k - hd <= 2.5 * (hd - lsh) or rs_k - hd < 2:
                continue
            t2 = min(range(hd + 1, rs_k), key=pick)
            m = (Y[t2] - Y[t1]) / (t2 - t1)
            nk_hd = Y[t1] + m * (hd - t1)
            height = Hd - sg * nk_hd
            Rs = sg * X[rs_k]
            if height <= 0 or Hd - max(Ls, Rs) < HEAD_MIN * height or abs(Ls - Rs) > 0.35 * height:
                continue
            if abs(Y[t1] - Y[t2]) > TROUGH_MAX * height or Rs <= sg * (Y[t2]):
                continue
            neck = lambda k: m * k + Y[t1] - m * t1  # noqa: E731
            if any(sg * c[k] < sg * neck(k) for k in range(t2, rs_k + 1)):
                continue                                     # already through the neckline before the shoulder
            pre = max(0, lsh - 30)
            prior = min(sg * Y[k] for k in range(pre, lsh + 1))
            context = "reversal" if prior < sg * nk_hd - 0.5 * height else "continuation"
            p = {"kind": kind, "sg": sg, "P": P, "lsh": lsh, "hd": hd, "rsh": None, "rs_k": rs_k, "t1": t1, "t2": t2,
                 "m": m, "b": Y[t1] - m * t1, "height": height, "context": context, "early": True}
            brk = next((k for k in range(rs_k + 1, n) if sg * c[k] < sg * neck(k)), None)
            nk = neck(i)
            st = {"dist": sg * (nk - c[i]) / height, "neck": nk, "target": nk - sg * height, "brk": brk}
            if brk is None:
                if i - rs_k > HS.MAX_WAIT:
                    break
                st["status"] = "forming"
            else:
                over = any(sg * c[k] > sg * neck(k) + HS.FAIL * height or
                           (sg == 1 and l[k] <= neck(k) - height) or (sg == -1 and h[k] >= neck(k) + height)
                           for k in range(brk + 1, n))
                if over:
                    break                                    # failed or done before the shoulder was confirmed
                st["status"] = "broken" if i - brk < HS.BREAK_FRESH else "after_break"
            out.append((p, st))
            break
    return out


def exit_reason(p, h, l, c, i):
    """Why a pattern is no longer active at candle i (mirrors hs.state's exits): target | failed | expired."""
    sg, H = p["sg"], p["height"]
    ext = (h if sg == 1 else l)
    shoulder = sg * ext[p["rsh"]]
    brk = None
    for k in range(p["known"] - p["P"] + 1, i + 1):
        if brk is None:
            if sg * ext[k] > shoulder:
                return "failed", k
            if sg * c[k] < sg * HS.neck(p, k):
                brk = k
            elif k - p["rsh"] > HS.MAX_WAIT:
                return "expired", k
            continue
        if k - brk > HS.MAX_AGE:
            return "expired", k
        if sg * c[k] > sg * HS.neck(p, k) + HS.FAIL * H:
            return "failed", k
        if (sg == 1 and l[k] <= HS.neck(p, k) - H) or (sg == -1 and h[k] >= HS.neck(p, k) + H):
            return "target", k
    return None, None


def scan(t, o, h, l, c):
    """Active patterns at the last candle: complete ones (hs.state) and right shoulders forming."""
    i, out, heads = len(c) - 1, [], set()
    for P in P_SIZES:
        for p in HS.find(h, l, c, P, max_span=SPAN, continuation=True, head_min=HEAD_MIN, trough_max=TROUGH_MAX):
            if p["hd"] in heads:
                continue
            s = HS.state(p, o, h, l, c, i)
            if s:
                heads.add(p["hd"])
                out.append((p, s))
    for P in P_SIZES:
        for p, st_ in forming(h, l, c, P):
            if p["hd"] in heads:
                continue
            heads.add(p["hd"])
            out.append((p, st_))
    return out


def _r(x):
    return None if x is None else float(f"{x:.7g}")


def row(code, tf, t, o, h, l, c, p, s):
    i, sg, H = len(c) - 1, p["sg"], p["height"]
    ext = (h if sg == 1 else l)
    rs = p["rsh"] if p["rsh"] is not None else p["rs_k"]
    if s["brk"] is None:
        stop = ext[rs]
    else:
        stop = s["neck"] + sg * HS.FAIL * H
    nk = s["neck"]
    return {"key": f"{code}:{tf}:{p['kind']}:{t[p['hd']]}", "inst": code, "tf": tf, "kind": p["kind"], "context": p["context"],
            "status": s["status"], "label": LABEL[s["status"]], "P": p["P"], "rs_confirmed": p["rsh"] is not None, "close": _r(c[i]), "bar_ms": t[i],
            "neck": _r(nk), "target": _r(s["target"]), "stop": _r(stop), "height_pct": round(100 * H / nk, 2) if nk else None,
            "dist_h": round(s["dist"], 2), "to_neck_pct": round(100 * (nk / c[i] - 1), 2), "to_target_pct": round(100 * (s["target"] / c[i] - 1), 2),
            "to_stop_pct": round(100 * (stop / c[i] - 1), 2), "rr": round(abs(s["target"] - c[i]) / abs(stop - c[i]), 2) if stop != c[i] else None,
            "break_ms": t[s["brk"]] if s.get("brk") is not None else None,
            "retest_ms": t[s["retest_k"]] if s.get("retest_k") is not None else None,
            "geo": {"t1_ms": t[p["t1"]], "y1": HS.neck(p, p["t1"]), "slope_ms": p["m"] / (t[1] - t[0]) if len(t) > 1 else 0.0,
                    "height": H, "sg": sg, "rs_px": ext[rs], "rs_ms": t[rs], "brk_ms": t[s["brk"]] if s.get("brk") is not None else None},
            "pts": {"ls": [t[p["lsh"]], _r(ext[p["lsh"]])], "head": [t[p["hd"]], _r(ext[p["hd"]])], "rs": [t[rs], _r(ext[rs])],
                    "t1": [t[p["t1"]], _r(HS.neck(p, p["t1"]))], "t2": [t[p["t2"]], _r(HS.neck(p, p["t2"]))],
                    "neck_now": [t[i], _r(nk)]}}


# ------------------------------------------------------------------ the forward log
def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"since": None, "patterns": {}, "events": []}


def save_json(path, d):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, path)


def settle(e, t, h, l, c):
    """Why a logged pattern left the active list, from its stored levels and the candles after it was last active:
    target | failed | expired, or 'dropped' when it never broke and simply stopped qualifying (shape changed)."""
    g, last = e["last"].get("geo"), e["last"]
    if not g:
        return "dropped"
    sg, H = g["sg"], g["height"]
    neck = lambda ms: g["y1"] + g["slope_ms"] * (ms - g["t1_ms"])  # noqa: E731
    brk_ms = g.get("brk_ms")
    step = t[1] - t[0] if len(t) > 1 else 1
    for k in range(len(t)):
        if t[k] <= last["bar_ms"]:
            continue
        if brk_ms is None:
            if sg * (h if sg == 1 else l)[k] > sg * g["rs_px"]:
                return "failed"                              # right shoulder taken out before the break
            if sg * c[k] < sg * neck(t[k]):
                brk_ms = t[k]
            elif (t[k] - g["rs_ms"]) / step > HS.MAX_WAIT:
                return "expired"
            continue
        if sg * c[k] > sg * neck(t[k]) + HS.FAIL * H:
            return "failed"
        if (sg == 1 and l[k] <= neck(t[k]) - H) or (sg == -1 and h[k] >= neck(t[k]) + H):
            return "target"
        if (t[k] - brk_ms) / step > HS.MAX_AGE:
            return "expired"
    return "dropped" if brk_ms is None else "expired"


def update_log(S, code, tf, t, o, h, l, c, active, now_s):
    """New patterns and stage changes become events; patterns that left the active list get their outcome. A new
    pattern on a head whose earlier pattern is already settled is a new record (key suffix #2, #3, ...)."""
    events, keys = [], set()
    alias = S.setdefault("alias", {})
    for p, s in active:
        r = row(code, tf, t, o, h, l, c, p, s)
        base = r["key"]
        key = alias.get(base, base)
        e = S["patterns"].get(key)
        if e is not None and not e.get("open"):
            n = 2
            while f"{base}#{n}" in S["patterns"]:
                n += 1
            key = alias[base] = f"{base}#{n}"
            e = None
        r["key"] = key
        keys.add(key)
        if e is None:
            e = S["patterns"][key] = {"inst": code, "tf": tf, "kind": r["kind"], "context": r["context"], "first_ms": r["bar_ms"],
                                      "first_px": r["close"], "first_status": r["status"], "stages": [], "open": True}
        e.update(last=r, open=True)
        st_now = r["status"]
        seen = [x[0] for x in e["stages"]]
        if st_now not in seen and st_now not in ("pattern", "after_break"):
            if not seen or STAGE.get(st_now, 0) >= STAGE.get(seen[-1], 0):   # never step back to an earlier stage
                e["stages"].append([st_now, r["bar_ms"], r["close"]])
                events.append({"key": key, "inst": code, "tf": tf, "kind": r["kind"], "context": r["context"], "status": st_now,
                               "bar_ms": r["bar_ms"], "price": r["close"], "neck": r["neck"], "target": r["target"], "stop": r["stop"],
                               "rs_confirmed": r["rs_confirmed"], "logged_utc": now_s})
    for k, e in S["patterns"].items():
        if not e.get("open") or e["inst"] != code or e["tf"] != tf or k in keys:
            continue
        e["open"] = False
        out = settle(e, t, h, l, c)
        last = e.get("last") or {}
        e["outcome"], e["end_ms"], e["end_px"] = out, t[-1], c[-1]
        if out != "dropped":
            events.append({"key": k, "inst": code, "tf": tf, "kind": e["kind"], "context": e["context"], "status": out,
                           "bar_ms": t[-1], "price": _r(c[-1]), "neck": last.get("neck"), "target": last.get("target"),
                           "stop": last.get("stop"), "logged_utc": now_s})
    return events


def record(S):
    """Forward record of settled patterns that broke their neckline: how many reached the target vs failed."""
    out = {}
    for tf in TFS:
        done = [e for e in S["patterns"].values() if e["tf"] == tf and not e.get("open") and any(x[0] == "broken" for x in e["stages"])]
        hit = sum(1 for e in done if e.get("outcome") == "target")
        fail = sum(1 for e in done if e.get("outcome") == "failed")
        out[tf] = {"settled": len(done), "target": hit, "failed": fail, "expired": len(done) - hit - fail,
                   "hit_pct": round(100 * hit / len(done), 1) if done else None,
                   "open": sum(1 for e in S["patterns"].values() if e["tf"] == tf and e.get("open"))}
    return out


# ------------------------------------------------------------------ backtest (history, no look-ahead)
def backtest_series(t, o, h, l, c, head_min=None, trough_max=None):
    """Every pattern that broke its neckline: entry at the next candle's open after the break close, stop at the
    failure level (neckline + 35% of the height against the trade), target = the measured move, first hit within
    MAX_AGE candles, else closed at that candle. R capped at -1.5 (gaps)."""
    trades, used = [], set()
    for P in P_SIZES:
        for p in HS.find(h, l, c, P, max_span=SPAN, continuation=True, head_min=HEAD_MIN if head_min is None else head_min,
                           trough_max=TROUGH_MAX if trough_max is None else trough_max):
            if p["hd"] in used:
                continue
            sg, H = p["sg"], p["height"]
            ext = (h if sg == 1 else l)
            brk = None
            for k in range(p["known"] - p["P"] + 1, len(c)):
                if k < p["known"]:
                    if sg * ext[k] > sg * ext[p["rsh"]]:
                        break
                    if sg * c[k] < sg * HS.neck(p, k):
                        break                                 # broke before the pattern was even known: not tradable
                    continue
                if sg * ext[k] > sg * ext[p["rsh"]] or k - p["rsh"] > HS.MAX_WAIT:
                    break
                if sg * c[k] < sg * HS.neck(p, k):
                    brk = k
                    break
            if brk is None or brk + 1 >= len(c):
                continue
            used.add(p["hd"])
            e = o[brk + 1]
            nk = HS.neck(p, brk)
            stop, target = nk + sg * HS.FAIL * H, nk - sg * H
            risk = sg * (stop - e)
            if risk <= 0 or sg * (e - target) <= 0:
                continue
            res, end = None, min(len(c) - 1, brk + HS.MAX_AGE)
            for k in range(brk + 1, end + 1):
                if (sg == 1 and h[k] >= stop) or (sg == -1 and l[k] <= stop):
                    fill = max(o[k], stop) if sg == 1 else min(o[k], stop)      # a gap through the stop fills at the open
                    res = ("stop", max(-1.5, sg * (e - fill) / risk))
                    break
                if (sg == 1 and l[k] <= target) or (sg == -1 and h[k] >= target):
                    res = ("target", sg * (e - target) / risk)
                    break
            if res is None:
                res = ("time", max(-1.5, sg * (e - c[end]) / risk))
            trades.append({"kind": p["kind"], "context": p["context"], "r": round(res[1], 3), "exit": res[0], "brk_ms": t[brk]})
    return trades


def summarize(tr):
    if not tr:
        return {"n": 0}
    rs = [x["r"] for x in tr]
    return {"n": len(tr), "target_pct": round(100 * sum(x["exit"] == "target" for x in tr) / len(tr), 1),
            "stop_pct": round(100 * sum(x["exit"] == "stop" for x in tr) / len(tr), 1),
            "avg_r": round(st.mean(rs), 2), "median_r": round(st.median(rs), 2),
            "win_pct": round(100 * sum(r > 0 for r in rs) / len(rs), 1)}


def backtest(log=print):
    res, per, strict = {}, {}, {}
    for code, _ in INSTR:
        src, sym = source_for(code, log)
        if not src:
            continue
        for tf in TFS:
            rows = long_history(src, sym, tf, BT_BARS[tf])
            if len(rows) < 300:
                continue
            sr = _series(rows)
            tr = backtest_series(*sr)
            per.setdefault(tf, []).extend(tr)
            strict.setdefault(tf, []).extend(backtest_series(*sr, head_min=0.2, trough_max=0.5))
            res[f"{code}:{tf}"] = dict(summarize(tr), first_ms=rows[0][0], last_ms=rows[-1][0], bars=len(rows))
            log(f"stf backtest {code} {tf}: {len(rows)} candles, {len(tr)} trades")
    out = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "series": res, "tf": {}}
    for tf, tr in per.items():
        out["tf"][tf] = {"all": summarize(tr), "top": summarize([x for x in tr if x["kind"] == "top"]),
                         "inverse": summarize([x for x in tr if x["kind"] == "inverse"]),
                         "reversal": summarize([x for x in tr if x["context"] == "reversal"]),
                         "continuation": summarize([x for x in tr if x["context"] == "continuation"]),
                         "strict": summarize(strict.get(tf, []))}
    out["limits"] = {"stf": {"head_min": HEAD_MIN, "trough_max": TROUGH_MAX}, "strict": {"head_min": 0.2, "trough_max": 0.5}}
    save_json(BTSTATS, out)
    return out


# ------------------------------------------------------------------ alerts
TF_NAME = {"15m": "15-min", "1h": "1-hour", "4h": "4-hour"}


def fmt(x):
    if x is None:
        return "—"
    return f"{x:,.2f}" if x >= 100 else f"{x:,.4f}" if x >= 1 else f"{x:.6g}"


def alert_text(ev):
    name = dict(INSTR)[ev["inst"]]
    kind = "Head & shoulders TOP (bearish)" if ev["kind"] == "top" else "INVERSE head & shoulders (bullish)"
    st_ = LABEL.get(ev["status"], ev["status"])
    line = f"{name} {TF_NAME[ev['tf']]}: {kind}, {ev['context']}\n{st_} at {fmt(ev['price'])}"
    if ev["status"] in ("forming", "near_neckline", "broken", "retest"):
        line += f"\nNeckline {fmt(ev['neck'])} · target {fmt(ev['target'])} · stop {fmt(ev['stop'])}"
    return line


def send_alerts(events, log=print):
    if not events:
        return 0
    try:
        import alerts
        cred = alerts.read_cred()
    except Exception as e:  # noqa: BLE001
        log(f"stf: alerts unavailable ({type(e).__name__})")
        return 0
    if not cred:
        return 0
    n = 0
    for ev in events[:10]:
        try:
            alerts.send(*cred, "Bee's STF HNS\n" + alert_text(ev) + "\nhttps://beesells.xyz/#stf")
            n += 1
        except Exception as e:  # noqa: BLE001
            log(f"stf: telegram send failed ({type(e).__name__})")
            break
    if len(events) > 10:
        try:
            alerts.send(*cred, f"Bee's STF HNS: {len(events) - 10} more updates on https://beesells.xyz/#stf")
        except Exception:  # noqa: BLE001
            pass
    return n


# ------------------------------------------------------------------ one run
def run(log=print, now=None):
    now = now or datetime.now(timezone.utc)
    now_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    S = load_state()
    first_run = S["since"] is None
    S["since"] = S["since"] or now_s
    out = {"generated_utc": now_s, "instruments": [], "tfs": list(TFS), "series": {}, "active": [], "labels": LABEL,
           "rules": {"p_sizes": list(P_SIZES), "span": SPAN, "near": HS.NEAR, "fail": HS.FAIL, "max_wait": HS.MAX_WAIT,
                     "max_age": HS.MAX_AGE, "retest": HS.RETEST}}
    new_events, problems = [], []
    for code, name in INSTR:
        src, sym = source_for(code, log)
        out["instruments"].append({"code": code, "name": name, "source": SOURCE_NAME.get(src), "symbol": sym, "ok": bool(src)})
        if not src:
            problems.append(f"{code}: no data source")
            continue
        for tf in TFS:
            try:
                rows = klines(src, sym, tf)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{code} {tf}: {type(e).__name__}")
                continue
            if len(rows) < 300:
                problems.append(f"{code} {tf}: only {len(rows)} candles")
                continue
            t, o, h, l, c = _series(rows)
            active = scan(t, o, h, l, c)
            evs = update_log(S, code, tf, t, o, h, l, c, active, now_s)
            new_events += evs
            out["active"] += [row(code, tf, t, o, h, l, c, p, s_) for p, s_ in active]
            out["series"].setdefault(code, {})[tf] = {"last_ms": t[-1], "close": c[-1],
                                                       "bars": [[t[k], _r(o[k]), _r(h[k]), _r(l[k]), _r(c[k])] for k in range(max(0, len(c) - CHART_BARS), len(c))]}
    S["events"] = (S["events"] + new_events)[-2000:]
    # keep settled patterns for the record; trim the oldest settled beyond 3,000
    settled = sorted((k for k, e in S["patterns"].items() if not e.get("open")), key=lambda k: S["patterns"][k].get("end_ms") or 0)
    for k in settled[:-3000]:
        del S["patterns"][k]
    save_json(STATE, S)
    order = {"retest": 0, "broken": 1, "near_neckline": 2, "forming": 3, "pattern": 4, "after_break": 5}
    tfo = {"15m": 0, "1h": 1, "4h": 2}
    out["active"].sort(key=lambda r: (order.get(r["status"], 9), tfo[r["tf"]], r["inst"]))
    out["events"] = S["events"][-300:][::-1]
    out["record"] = {"since": S["since"], "by_tf": record(S)}
    try:
        with open(BTSTATS) as f:
            out["backtest"] = json.load(f)
    except (OSError, ValueError):
        out["backtest"] = None
    out["problems"] = problems
    save_json(OUT, out)
    # the first run only learns what is already there: no alert flood for patterns that were already under way
    sent = 0 if first_run else send_alerts(new_events, log)
    log(f"stf: {len(out['active'])} active, {len(new_events)} new events, {sent} alerts sent" + (f"; problems: {problems}" if problems else ""))
    return out


def probe(log=print):
    for code, name in INSTR:
        src, sym = source_for(code, log)
        log(f"{code} {name}: {src} {sym}")
        if not src:
            continue
        for tf in TFS:
            rows = klines(src, sym, tf)
            t, o, h, l, c = _series(rows)
            f = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%m-%d %H:%M")  # noqa: E731
            log(f"  {tf}: {len(rows)} candles {f(t[0])} .. {f(t[-1])} close {c[-1]}")
            for P in P_SIZES:
                for p in HS.find(h, l, c, P, max_span=SPAN, continuation=True, head_min=HEAD_MIN, trough_max=TROUGH_MAX):
                    log(f"    P{P} {p['kind']:7s} {p['context']:12s} LS {f(t[p['lsh']])} {fmt((h if p['sg'] == 1 else l)[p['lsh']])}"
                        f" H {f(t[p['hd']])} {fmt((h if p['sg'] == 1 else l)[p['hd']])} RS {f(t[p['rsh']])} {fmt((h if p['sg'] == 1 else l)[p['rsh']])}"
                        f" neck {fmt(HS.neck(p, p['t1']))}->{fmt(HS.neck(p, p['t2']))} exit {exit_reason(p, h, l, c, len(c) - 1)[0]}")
            for p, s in scan(t, o, h, l, c):
                log(f"    ACTIVE {p['kind']} {s['status']} neck {fmt(s['neck'])} target {fmt(s['target'])}")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    elif "--backtest" in sys.argv:
        backtest()
    else:
        run()
        try:                                       # the historical record: refreshed once a day, after the run
            stale = time.time() - os.path.getmtime(BTSTATS) > 86400
        except OSError:
            stale = True
        if stale:
            backtest()
