"""
Classic chart patterns on the daily and the weekly chart (Python 3.9 stdlib only):
  * Double tops (bearish) and double bottoms (bullish)
  * Cup and handle (bullish)
Same entry vocabulary as the head-and-shoulders scanner (bin/hs.py): near the line, the break, the retest of the line.
Nothing uses candles after the one it is evaluated on: a swing point is known P candles after it.

Double top (the bottom is the mirror image on the lows):
  * two swing highs P1 < P2 (swing = highest high P candles either side), MIN_SEP..MAX_SEP candles apart, nothing in
    between above the higher one, peaks within PEAK_TOL of the pattern height of each other (and within 6%);
  * the neckline is the lowest low between them (horizontal); height = average peak - neckline, at least MIN_H of the
    price; a real move into the pattern: within one pattern length before P1 price was at least 0.6 heights below the
    neckline (tops) / above it (bottoms);
  * over when price takes out the higher peak before breaking (no double top any more), when it closes back through the
    neckline by more than FAIL heights after the break, at the measured target (neckline -/+ height), or MAX_AGE
    candles after the break; also when there is no break within MAX_WAIT candles of the second peak.

Cup and handle:
  * a left rim L (swing high), a right rim R (a later swing high) within RIM_TOL of the cup depth of L's level and
    within 8%; nothing in between above the higher rim; the cup low B in the middle 20-80% of the cup in time (a U, not
    a lopsided V) and at least U_SHARE of the cup's candles closing in its lower third (a straight V spends a third); depth (rim - B) / rim between the
    timeframe's limits; a run-up into the left rim (price 0.6 depths below the rim within one cup length before L);
  * the handle is everything after R: it must stay in the upper half of the cup (handle low above the cup midpoint),
    last at least H_MIN candles and at most a third of the cup length (else expired);
  * the line is the right rim, breakout = first close above it; target = rim + depth; stop = the handle low.

Status at the latest candle:
  near_neckline / handle  pattern confirmed, no break yet, price within NEAR of the height from the line
  broken                  first close through the line in the last BREAK_FRESH candles, no retest yet
  retest                  after the break price moved AWAY of the height from the line, came back within RETEST of
                          it in the last 2 candles, and the latest close is still on the break side or within RETEST
  after_break             broken earlier, still valid, not at target (context only)
"""
from datetime import date, timedelta

TF = {
    "1d": {"P": (5, 10), "min_sep": 10, "max_sep": 120, "min_h": 0.08, "max_wait": 40, "fresh": 5, "max_age": 60,
           "cup": (35, 330), "depth": (0.12, 0.55), "h_min": 5, "bars": 420},
    "1w": {"P": (3, 5), "min_sep": 4, "max_sep": 60, "min_h": 0.15, "max_wait": 20, "fresh": 2, "max_age": 26,
           "cup": (8, 120), "depth": (0.15, 0.70), "h_min": 2, "bars": 0},
}
PEAK_TOL, RIM_TOL, U_SHARE = 0.25, 0.25, 0.40
NEAR, RETEST, AWAY, FAIL = 0.25, 0.15, 0.3, 0.35


def pivots(x, P, high):
    out = []
    for p in range(P, len(x) - P):
        v = x[p]
        a, b = x[p - P:p], x[p + 1:p + P + 1]
        if high and all(v > z for z in a) and all(v >= z for z in b):
            out.append(p)
        elif not high and all(v < z for z in a) and all(v <= z for z in b):
            out.append(p)
    return out


def find_double(h, l, c, P, cfg):
    """Double tops (sg = 1) and bottoms (sg = -1). Each dict: sg, a (first peak), z (second peak), line, height,
    inval (price level that kills it before the break), known (first candle it can be used on), wait_from."""
    out = []
    for kind, X, Y, sg in (("top", h, l, 1), ("bottom", l, h, -1)):
        pk = pivots(X, P, sg == 1)
        for j in range(1, len(pk)):
            for i0 in range(j - 1, max(-1, j - 4), -1):           # the second peak may pair with one of the 3 before
                a, z = pk[i0], pk[j]
                if not cfg["min_sep"] <= z - a <= cfg["max_sep"]:
                    continue
                pa, pz = sg * X[a], sg * X[z]
                mid = range(a + 1, z)
                if max(sg * X[k] for k in mid) > max(pa, pz):
                    continue                                       # something in between was higher: not a double
                t = min(mid, key=lambda k: sg * Y[k])
                line = Y[t]
                avg = (X[a] + X[z]) / 2
                height = sg * (avg - line)
                if line <= 0 or height <= 0 or height / (avg if sg == 1 else line) < cfg["min_h"]:
                    continue
                if abs(X[a] - X[z]) > PEAK_TOL * height or abs(X[a] - X[z]) > 0.06 * max(X[a], X[z]):
                    continue
                if min(abs(t - a), abs(z - t)) < 2:
                    continue
                pre = range(max(0, a - (z - a)), a + 1)
                if not min(sg * Y[k] for k in pre) < sg * line - 0.6 * height:
                    continue                                       # no move into the pattern
                out.append({"type": "double", "kind": kind, "sg": sg, "P": P, "a": a, "z": z, "t": t, "line": line,
                            "height": height, "inval": X[z] if sg * X[z] >= sg * X[a] else X[a], "known": z + P, "wait_from": z})
                break
    return out


def find_cup(h, l, c, P, cfg):
    """Cups with a handle (bullish). Each dict like find_double, sg = -1 (the trade is up), line = the right rim."""
    out = []
    lo_len, hi_len = cfg["cup"]
    dmin, dmax = cfg["depth"]
    pk = pivots(h, P, True)
    for j in range(1, len(pk)):
        R = pk[j]
        best = None
        for i0 in range(j - 1, -1, -1):
            L = pk[i0]
            n = R - L
            if n < lo_len:
                continue
            if n > hi_len:
                break
            top = max(h[L], h[R])
            if max(h[L + 1:R]) > top:
                break                                              # a higher high in between: older rims are not cups
            B = min(range(L + 1, R), key=lambda k: l[k])
            rim = min(h[L], h[R])
            depth = rim - l[B]
            if depth <= 0 or not dmin <= depth / rim <= dmax:
                continue
            if abs(h[L] - h[R]) > RIM_TOL * depth or abs(h[L] - h[R]) > 0.08 * top:
                continue
            if not 0.2 <= (B - L) / n <= 0.8:
                continue
            third = l[B] + depth / 3
            if sum(1 for k in range(L + 1, R) if c[k] < third) < U_SHARE * (n - 1):
                continue                                           # a V or a spike, not a rounded cup
            pre = range(max(0, L - n), L + 1)
            if not min(l[k] for k in pre) < h[L] - 0.6 * depth:
                continue                                           # no run-up into the left rim
            best = (L, B, depth)                                   # keep the widest valid cup for this right rim
        if best:
            L, B, depth = best
            out.append({"type": "cup", "kind": "cup", "sg": -1, "P": P, "a": L, "z": R, "t": B, "line": h[R],
                        "height": depth, "inval": l[B] + depth / 2, "known": R + max(P, cfg["h_min"]), "wait_from": R,
                        "max_handle": max(cfg["h_min"] + 1, (R - L) // 3)})
    return out


def state(p, h, l, c, i, cfg):
    """Status of pattern p at candle i, or None. 'dist' = (close - line) in heights, positive = on the break side."""
    if i < p["known"]:
        return None
    sg, H, line = p["sg"], p["height"], p["line"]
    cup = p["type"] == "cup"
    brk, hlow = None, None
    for k in range(p["z"] + 1, i + 1):
        if brk is None:
            ext = (h if sg == 1 else l)[k]
            if cup:
                hlow = l[k] if hlow is None else min(hlow, l[k])
                if l[k] < p["inval"]:
                    return None                                   # the handle fell into the lower half of the cup
                if c[k] > line and k >= p["known"]:
                    brk = k
                elif c[k] > line:
                    return None                                   # closed above the rim before a handle formed
                elif k - p["z"] > p["max_handle"]:
                    return None                                   # handle too long: expired
            else:
                if sg * ext > sg * p["inval"]:
                    return None                                   # the higher peak was taken out: no double
                if sg * c[k] < sg * line:
                    brk = k
                elif k - p["wait_from"] > cfg["max_wait"]:
                    return None
    dist = sg * (line - c[i]) / H
    target = line - sg * H
    stop = hlow if cup else None
    if brk is None:
        if dist >= -NEAR:
            return {"status": "handle" if cup else "near_neckline", "dist": dist, "target": target, "brk": None, "stop": stop}
        return None
    if i - brk > cfg["max_age"]:
        return None
    touched, away = None, False
    for k in range(brk + 1, i + 1):
        if sg * c[k] > sg * line + FAIL * H:
            return None                                           # closed back through the line: failed
        if (sg == 1 and l[k] <= target) or (sg == -1 and h[k] >= target):
            return None                                           # measured target reached
        if sg * (line - c[k]) / H >= AWAY:
            away = True
        elif away and sg * (line - (h if sg == 1 else l)[k]) / H <= RETEST:
            touched = k
    if sg * (line - c[i]) / H < -FAIL:
        return None
    st = "after_break"
    if i - brk < cfg["fresh"] and touched is None:
        st = "broken"
    elif touched is not None and i - touched <= 1 and dist >= -RETEST:
        st = "retest"
    return {"status": st, "dist": dist, "target": target, "brk": brk, "retest_k": touched, "stop": stop}


def events(p, h, l, c, cfg):
    """The entries a trader could have taken on pattern p, walking forward once (same rules as state()):
    [(status, candle)] with status near_neckline / handle, broken, retest."""
    n, sg, H, line = len(c), p["sg"], p["height"], p["line"]
    cup, out, brk = p["type"] == "cup", [], None
    for k in range(p["z"] + 1, n):
        ext = (h if sg == 1 else l)[k]
        if cup:
            if l[k] < p["inval"] or (c[k] > line and k < p["known"]) or (c[k] <= line and k - p["z"] > p["max_handle"]):
                return out
            if c[k] > line:
                brk = k
                break
        else:
            if sg * ext > sg * p["inval"]:
                return out
            if sg * c[k] < sg * line:
                brk = k
                break
            if k - p["wait_from"] > cfg["max_wait"]:
                return out
        if k >= p["known"] and not out and sg * (line - c[k]) / H >= -NEAR:
            out.append(("handle" if cup else "near_neckline", k))
    if brk is None:
        return out
    e = max(brk, p["known"])
    if e - brk < cfg["fresh"]:
        out.append(("broken", e))
    away = False
    for k in range(brk + 1, min(n, brk + cfg["max_age"] + 1)):
        if sg * c[k] > sg * line + FAIL * H:
            break
        if (sg == 1 and l[k] <= line - H) or (sg == -1 and h[k] >= line + H):
            break
        if sg * (line - c[k]) / H >= AWAY:
            away = True
        elif away and sg * (line - (h if sg == 1 else l)[k]) / H <= RETEST:
            if k >= p["known"] and sg * (line - c[k]) / H >= -RETEST:
                out.append(("retest", k))
            break
    return out


def weekly(dates, o, h, l, c):
    """Daily candles -> completed weekly candles (Monday-Sunday, UTC). Returns (week start dates, o, h, l, c,
    index of each week's last daily candle). A week counts once its Sunday candle is in."""
    W, cur = [], None
    for k, d in enumerate(dates):
        dd = date.fromisoformat(d)
        ws = (dd - timedelta(days=dd.weekday())).isoformat()
        if ws != cur:
            W.append([ws, o[k], h[k], l[k], c[k], k, dd.weekday()])
            cur = ws
        else:
            w = W[-1]
            w[2], w[3], w[4], w[5], w[6] = max(w[2], h[k]), min(w[3], l[k]), c[k], k, dd.weekday()
    W = [w for w in W if w[6] == 6]
    return [w[0] for w in W], [w[1] for w in W], [w[2] for w in W], [w[3] for w in W], [w[4] for w in W], [w[5] for w in W]


def scan(h, l, c, cfg):
    """All patterns of both families, de-duplicated (one per second peak / right rim and type)."""
    seen, out = set(), []
    for P in cfg["P"]:
        for p in find_double(h, l, c, P, cfg) + find_cup(h, l, c, P, cfg):
            key = (p["type"], p["kind"], p["z"])
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


# ---------------------------------------------------------------- website export
MIN_ADV = 2e5
LABEL = {"near_neckline": "Near the neckline", "handle": "Handle: near the breakout", "broken": "Broken out",
         "retest": "Retesting the line", "after_break": "Broken earlier"}
NAME = {"top": "Double top", "bottom": "Double bottom", "cup": "Cup & handle"}


def _r(x):
    return None if x is None else float(f"{x:.7g}")


def rows_for(sym, tf, dates, h, l, c, close_now, rank, stats, cfg):
    i, out, kept = len(c) - 1, [], []
    for p in sorted(scan(h, l, c, cfg), key=lambda q: -q["z"]):
        st = state(p, h, l, c, i, cfg)
        if not st:
            continue
        if any(q["kind"] == p["kind"] and p["a"] <= q["z"] and q["a"] <= p["z"] for q in kept):
            continue                                       # overlaps a later pattern of the same kind: keep the latest
        kept.append(p)
        sg, H, line = p["sg"], p["height"], p["line"]
        stop = st["stop"] if p["type"] == "cup" and st["stop"] is not None else (p["inval"] if st["brk"] is None else line + sg * FAIL * H)
        fam = "cup" if p["type"] == "cup" else "double"
        hist = (stats.get(f"{tf}:{p['kind']}:{st['status']}") or {}).get("late")
        X = h if sg == 1 or p["type"] == "cup" else l
        ev = events(p, h, l, c, cfg)
        out.append({"symbol": sym, "tf": tf, "family": fam, "kind": p["kind"], "name": NAME[p["kind"]], "status": st["status"],
                    "close": _r(close_now), "line": _r(line), "target": _r(st["target"]), "stop": _r(stop),
                    "height_pct": round(100 * H / ((line + H) if sg == 1 else line), 1),
                    "to_line_pct": round(100 * (line / close_now - 1), 1) if close_now else None,
                    "to_target_pct": round(100 * (st["target"] / close_now - 1), 1) if close_now else None,
                    "to_stop_pct": round(100 * (stop / close_now - 1), 1) if close_now and stop else None,
                    "date": dates[i], "break_date": dates[st["brk"]] if st["brk"] is not None else None,
                    "retest_date": dates[st["retest_k"]] if st.get("retest_k") is not None else None,
                    "points": {"a": [dates[p["a"]], _r(X[p["a"]])], "z": [dates[p["z"]], _r(X[p["z"]])],
                               "t": [dates[p["t"]], _r((l if sg == 1 or p["type"] == "cup" else h)[p["t"]])],
                               "line": [[dates[p["a"]], _r(line)], [dates[i], _r(line)]]},
                    "first_date": dates[ev[0][1]] if ev else dates[min(i, p["known"])],
                    "bars": i - p["a"], "liq_rank": rank, "hist": hist})
    return out


def run(con, stats_path=None):
    """Every tracked pair (in the universe, not a stablecoin or excluded token, 20-day volume >= MIN_ADV or added on
    purpose), daily chart (last 420 days) and weekly chart (full history)."""
    import json
    import os
    from binance_sync import STABLE_BASES
    from bot_core import excluded_tokens
    import venues
    p = stats_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "patterns_stats.json")
    stats = {}
    if os.path.exists(p):
        with open(p) as f:
            stats = json.load(f)
    tokens = dict(con.execute("SELECT symbol, token FROM pairs"))
    excl = excluded_tokens(set(tokens.values())) | set(STABLE_BASES)
    liq = []
    for (sym,) in con.execute("SELECT symbol FROM latest WHERE in_universe=1"):
        if tokens.get(sym) in excl:
            continue
        r = con.execute("SELECT AVG(volume_usdt) FROM (SELECT volume_usdt FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 20)", (sym,)).fetchone()
        if r[0] and (r[0] >= MIN_ADV or venues.is_external(sym)):
            liq.append((r[0], sym))
    liq.sort(reverse=True)
    rows, scanned = [], 0
    for rank, (_, sym) in enumerate(liq, 1):
        d = con.execute("SELECT date, open, high, low, close FROM daily WHERE symbol=? ORDER BY date", (sym,)).fetchall()
        if len(d) < 120:
            continue
        scanned += 1
        dates = [x[0] for x in d]
        o, h, l, c = ([x[k] or 0.0 for x in d] for k in range(1, 5))
        n = TF["1d"]["bars"]
        rows += rows_for(sym, "1d", dates[-n:], h[-n:], l[-n:], c[-n:], c[-1], rank, stats, TF["1d"])
        wd, wo, wh, wl, wc, _ = weekly(dates, o, h, l, c)
        if len(wc) >= 30:
            rows += rows_for(sym, "1w", wd, wh, wl, wc, c[-1], rank, stats, TF["1w"])
    order = {"retest": 0, "broken": 1, "handle": 2, "near_neckline": 2, "after_break": 3}
    rows.sort(key=lambda x: (order[x["status"]], x["tf"] != "1w", x["liq_rank"]))
    return {"rows": rows, "labels": LABEL, "names": NAME, "stats": stats, "scanned": scanned,
            "rules": {"min_adv": MIN_ADV, "near": NEAR, "retest": RETEST, "fail": FAIL,
                      "tf": {k: {kk: v[kk] for kk in ("min_h", "cup", "depth", "max_age")} for k, v in TF.items()}}}
