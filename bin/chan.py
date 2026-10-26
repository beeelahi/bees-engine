"""
Parallel channels on the daily and 4-hour charts (Python 3.9 stdlib only).

A "great channel" is a price corridor between two parallel lines that the market has respected:
  * lines fitted on confirmed swing points only (a swing needs P bars after it), over the last W bars;
    slope = average of the least-squares slopes through the swing lows and the swing highs;
  * at least 2 touches of each line (a swing within 10% of the channel width) and at least 5 in all;
  * at least 93% of closes in the window inside the lines (3% tolerance);
  * wide enough to trade, narrow enough to be a corridor: width >= 4 x ATR(20), 8-120% of price on the daily
    (5-45% on 4h).
Several windows are tried per coin; the one with the most touches wins (longer windows win ties).

Status at the latest closed candle:
  near_bottom / near_top   close in the lowest / highest 15% of the channel, still inside
  breakout / breakdown     a close beyond the line by >= 0.5 ATR on >= 1.5x average volume, in the last 3 candles
                           (judged against the channel as it stood before that candle)
  above_top / below_bottom outside the lines (by less than 15% of the width) without the volume or distance
                           to call it a break; further out, the channel is considered over and is not listed
  inside                   anywhere in between
No look-ahead anywhere: everything is computed from candles that had closed at that time.
"""
from bot_core import _atr

TOL_TOUCH, TOL_IN, MIN_INSIDE, MIN_TOUCH_SIDE, MIN_TOUCH_ALL = 0.10, 0.03, 0.93, 2, 5
EDGE = 0.15
BREAK_ATR, BREAK_VOL = 0.5, 1.5
TF = {"1d": {"P": 5, "windows": (60, 90, 120, 180), "min_width_pct": 8.0, "max_width_pct": 120.0, "bars_per_day": 1},
      "4h": {"P": 6, "windows": (90, 180, 270), "min_width_pct": 5.0, "max_width_pct": 45.0, "bars_per_day": 6}}


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


def _slope(pts):
    n = len(pts)
    if n < 2:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else None


def fit(h, l, c, atr, i, W, P, ph, pl, min_width_pct, max_width_pct=1e9):
    """Channel known at the close of bar i, fitted on [i-W, i]. Returns dict or None. Lines: y = m*x + b."""
    if i - W < 0 or atr[i] is None:
        return None
    lows = [(p, l[p]) for p in pl if i - W <= p <= i - P]
    highs = [(p, h[p]) for p in ph if i - W <= p <= i - P]
    if len(lows) < MIN_TOUCH_SIDE or len(highs) < MIN_TOUCH_SIDE:
        return None
    m1, m2 = _slope(lows), _slope(highs)
    if m1 is None or m2 is None:
        return None
    m = (m1 + m2) / 2
    lo_b = min(y - m * x for x, y in lows)
    hi_b = max(y - m * x for x, y in highs)
    w = hi_b - lo_b
    if w <= 0:
        return None
    tl = sum(1 for x, y in lows if y - m * x - lo_b <= TOL_TOUCH * w)
    th = sum(1 for x, y in highs if hi_b - (y - m * x) <= TOL_TOUCH * w)
    if tl < MIN_TOUCH_SIDE or th < MIN_TOUCH_SIDE or tl + th < MIN_TOUCH_ALL:
        return None
    mid_px = m * i + (lo_b + hi_b) / 2
    if mid_px <= 0 or w < 4 * atr[i] or not min_width_pct <= 100 * w / mid_px <= max_width_pct or m * i + lo_b <= 0:
        return None
    inside = sum(1 for k in range(i - W, i + 1) if lo_b - TOL_IN * w <= c[k] - m * k <= hi_b + TOL_IN * w)
    if inside < MIN_INSIDE * (W + 1):
        return None
    return {"m": m, "lo_b": lo_b, "hi_b": hi_b, "w": w, "W": W, "touch_lo": tl, "touch_hi": th, "inside": inside / (W + 1), "i": i}


def best(h, l, c, atr, i, tf, ph, pl):
    cfg = TF[tf]
    out = None
    for W in cfg["windows"]:
        f = fit(h, l, c, atr, i, W, cfg["P"], ph, pl, cfg["min_width_pct"], cfg["max_width_pct"])
        if f and (out is None or (f["touch_lo"] + f["touch_hi"], W) >= (out["touch_lo"] + out["touch_hi"], out["W"])):
            out = f
    return out


def lines(f, k):
    return f["m"] * k + f["lo_b"], f["m"] * k + f["hi_b"]


def state(o, h, l, c, v, tf, i=None):
    """Channel status at bar i (default: last). Returns dict or None when the coin has no great channel."""
    n = len(c)
    i = n - 1 if i is None else i
    atr = _atr(h, l, c, 20)
    P = TF[tf]["P"]
    ph, pl = pivots(h[:i + 1], P, True), pivots(l[:i + 1], P, False)
    # 1) a break in the last 3 candles, judged against the channel as it stood the candle before
    for j in range(i, max(i - 3, 0), -1):
        f = best(h, l, c, atr, j - 1, tf, ph, pl)
        if not f or atr[j] is None:
            continue
        lo_j, hi_j = lines(f, j)
        lo_p, hi_p = lines(f, j - 1)
        va = sum(v[max(0, j - 20):j]) / max(1, min(20, j))
        if c[j] > hi_j + BREAK_ATR * atr[j] and c[j - 1] <= hi_p and va and v[j] >= BREAK_VOL * va:
            return dict(f, status="breakout", event_i=j, level=hi_j)
        if c[j] < lo_j - BREAK_ATR * atr[j] and c[j - 1] >= lo_p and va and v[j] >= BREAK_VOL * va:
            return dict(f, status="breakdown", event_i=j, level=lo_j)
    f = best(h, l, c, atr, i, tf, ph, pl)
    if not f:
        return None
    lo, hi = lines(f, i)
    pos = (c[i] - lo) / f["w"]
    if pos < -EDGE or pos > 1 + EDGE:          # already well outside: the channel is over, not a setup any more
        return None
    st = ("above_top" if pos > 1 else "below_bottom" if pos < 0 else "near_bottom" if pos <= EDGE
          else "near_top" if pos >= 1 - EDGE else "inside")
    return dict(f, status=st, event_i=None, level=None, pos=pos)


# ---------------------------------------------------------------- website export
TOP_DAILY, TOP_4H, MIN_ADV = 300, 200, 2e6
LABEL = {"near_bottom": "Near channel bottom", "near_top": "Near channel top", "breakout": "Channel breakout",
         "breakdown": "Channel breakdown", "above_top": "Above the top (unconfirmed)", "below_bottom": "Below the bottom (unconfirmed)",
         "inside": "Inside the channel"}


def _r(x):
    return None if x is None else float(f"{x:.7g}")


def _row(sym, tf, times, o, h, l, c, v, rank):
    s = state(o, h, l, c, v, tf)
    if not s:
        return None
    i = len(c) - 1
    t0 = i - s["W"]
    lo0, hi0 = lines(s, t0)
    lo1, hi1 = lines(s, i)
    mid = (lo1 + hi1) / 2
    bpd = TF[tf]["bars_per_day"]
    row = {"symbol": sym, "tf": tf, "status": s["status"], "pos": _r((c[i] - lo1) / s["w"]), "close": _r(c[i]),
           "t0": times[t0], "lo0": _r(lo0), "hi0": _r(hi0), "t1": times[i], "lo1": _r(lo1), "hi1": _r(hi1),
           "width_pct": round(100 * s["w"] / mid, 1), "slope_pct_day": round(100 * s["m"] * bpd / mid, 2),
           "touches": [s["touch_lo"], s["touch_hi"]], "bars": s["W"], "days": round(s["W"] / bpd, 1),
           "to_bottom_pct": round(100 * (lo1 / c[i] - 1), 1), "to_top_pct": round(100 * (hi1 / c[i] - 1), 1), "liq_rank": rank}
    if s["event_i"] is not None:
        j = s["event_i"]
        row.update(event_t=times[j], level=_r(s["level"]), ago=i - j,
                   so_far_pct=(round(100 * ((c[i] / o[j + 1] - 1) if s["status"] == "breakout" else (1 - c[i] / o[j + 1])), 1) if j + 1 <= i else None))
    return row


def run(con, now_ms=None, stats_path=None):
    import json
    import os
    import time
    from binance_sync import STABLE_BASES
    from bot_core import excluded_tokens
    now_ms = now_ms or int(time.time() * 1000)
    tokens = dict(con.execute("SELECT symbol, token FROM pairs"))
    excl = excluded_tokens(set(tokens.values())) | set(STABLE_BASES)
    liq = []
    for (sym,) in con.execute("SELECT symbol FROM latest WHERE in_universe=1"):
        if tokens.get(sym) in excl:
            continue
        r = con.execute("SELECT AVG(volume_usdt), COUNT(*) FROM (SELECT volume_usdt FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 20)", (sym,)).fetchone()
        n = con.execute("SELECT COUNT(*) FROM daily WHERE symbol=?", (sym,)).fetchone()[0]
        if r[0] and r[0] >= MIN_ADV and n >= 250:
            liq.append((r[0], sym))
    liq.sort(reverse=True)
    rank = {s: k + 1 for k, (_, s) in enumerate(liq)}
    rows = []
    for _, sym in liq[:TOP_DAILY]:
        d = con.execute("SELECT date, open, high, low, close, volume_usdt FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 400", (sym,)).fetchall()[::-1]
        if len(d) < 250:
            continue
        x = _row(sym, "1d", [r[0] for r in d], *([r[k] or 0.0 for r in d] for k in range(1, 6)), rank[sym])
        if x:
            rows.append(x)
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='candles_4h'").fetchone():
        import h4_export
        have = {s for (s,) in con.execute("SELECT symbol FROM h4_state WHERE backfilled=1")}
        for _, sym in [z for z in liq if z[1] in have][:TOP_4H]:
            d = h4_export._series(con, sym, now_ms)[-400:]
            if len(d) < 300:
                continue
            x = _row(sym, "4h", [r[0] // 1000 for r in d], *([r[k] or 0.0 for r in d] for k in range(1, 6)), rank[sym])
            if x:
                rows.append(x)
    order = {"breakout": 0, "breakdown": 0, "near_bottom": 1, "near_top": 1, "above_top": 2, "below_bottom": 2, "inside": 3}
    rows.sort(key=lambda x: (order[x["status"]], x.get("ago", 9), x["liq_rank"]))
    stats = {}
    p = stats_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "chan_stats.json")
    if os.path.exists(p):
        with open(p) as f:
            stats = json.load(f)
    return {"rows": rows, "labels": LABEL, "stats": stats,
            "rules": {"edge": EDGE, "break_atr": BREAK_ATR, "break_vol": BREAK_VOL, "min_touch": MIN_TOUCH_ALL, "min_inside": MIN_INSIDE,
                      "top_daily": TOP_DAILY, "top_4h": TOP_4H, "windows": {k: list(v["windows"]) for k, v in TF.items()},
                      "min_width_pct": {k: v["min_width_pct"] for k, v in TF.items()}}}
