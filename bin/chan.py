"""
Parallel channels on the daily and 4-hour charts (Python 3.9 stdlib only).

How a channel is found (linear price, no look-ahead: only candles closed at that time):
  * every major swing point s can start a channel: a swing low starts a bottom-anchored one, a swing high a
    top-anchored one. The anchored line runs from s through a later major swing on the same side;
  * closes since s must respect the anchored line (none through it by more than 5% of the channel width) and at
    most 2 later major swings may poke through it (by at most 15% of the width: wicks);
  * the opposite line is parallel, through the farthest close, widened to swing extremes up to 15% further;
  * a touch is a swing within 10% of the width (and 8% of price) of a line; it counts only if price moved away by
    25% of the width since the previous touch. A channel needs 2+ touches on each line and 5+ in all (or 4 plus
    2+ reactions at the midline), and the anchored line's touches must span 30% of the channel's life.
  * quality = touches on the two lines + half the midline reactions (max 3) + a small bonus for length. Up to
    MAX_PER_COIN distinct channels per coin are kept (a long-term one and shorter ones inside it).

Status at the latest closed candle (first match wins):
  breakout / breakdown     a close beyond a line by 5% of the width after 5 closes inside, in the last 3 candles
  mid_reclaim / mid_loss   a close back above / below the midline after 5 closes on the other side, last 3 candles
  mid_retest               after a reclaim (3-40 candles earlier) price dipped to the midline and closed above it
  bottom_bounce            touched the bottom zone in the last 3 candles and closed green, 15-40% up the channel
  near_bottom / near_top   inside, in the lowest / highest 15% of the channel
  top_reject               tagged the top zone in the last 3 candles and closed red below 85% of the channel
  above_top / below_bottom outside the lines but not a break; inside: anywhere else.
  A channel is dropped once price is more than half a width outside it.
"""
import math

TF = {"1d": {"P_MAJOR": 10, "P_MINOR": 5, "min_bars": 90, "lookback": 2600, "bars_per_day": 1, "step": 86400},
      "4h": {"P_MAJOR": 12, "P_MINOR": 6, "min_bars": 120, "lookback": 1500, "bars_per_day": 6, "step": 14400}}
TOL_TOUCH, TOL_MID, TOL_IN, WICK, TOL_PX, AWAY, MAX_POKES = 0.10, 0.06, 0.05, 0.15, 0.08, 0.25, 2
MIN_WIDTH_PCT, MAX_WIDTH_PCT, MAX_WIDTH_START_PCT = 8.0, 400.0, 700.0
EDGE, BREAK_W, MAX_PER_COIN = 0.15, 0.05, 3


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


def _suffix_hulls(c, starts, i):
    """Upper and lower convex hulls of the closes on [s, i] for every start s (one right-to-left pass), so the
    farthest close from any line is found in O(hull) instead of O(bars)."""
    want = set(starts)
    up, lo, out = [], [], {}
    for k in range(i, min(starts) - 1, -1):
        p = (k, c[k])
        for hull, sign in ((up, 1), (lo, -1)):
            while len(hull) >= 2:
                (x1, y1), (x2, y2) = hull[-2], hull[-1]
                if sign * ((x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1)) <= 0:
                    hull.pop()
                else:
                    break
            hull.append(p)
        if k in want:
            out[k] = (list(up), list(lo))
    return out


def _touches(pts, others, m, b, off, sg, w, frac):
    out = []
    for x, y in pts:
        if abs(sg * (y - (m * x + b)) - off) > min(frac * w, TOL_PX * abs(m * x + b + sg * off)):
            continue
        if out and not any(out[-1] < ox < x and abs(sg * (oy - (m * ox + b)) - off) >= AWAY * w for ox, oy in others):
            continue
        out.append(x)
    return out


def scan(h, l, c, i, tf="1d"):
    """Every qualifying channel ending at bar i."""
    cfg = TF[tf]
    PM, Pm = cfg["P_MAJOR"], cfg["P_MINOR"]
    lb = max(0, i - cfg["lookback"])
    majL = [p for p in pivots(l[:i + 1], PM, False) if p >= lb]
    majH = [p for p in pivots(h[:i + 1], PM, True) if p >= lb]
    minL = [(p, l[p]) for p in pivots(l[:i + 1], Pm, False) if p >= lb]
    minH = [(p, h[p]) for p in pivots(h[:i + 1], Pm, True) if p >= lb]
    starts = [s for s in majL + majH if i - s >= cfg["min_bars"]]
    if not starts:
        return []
    hulls = _suffix_hulls(c, starts, i)
    out = []
    for anchor, same, X, Y, opp_major in (("lo", majL, l, h, majH), ("hi", majH, h, l, majL)):
        sg = 1 if anchor == "lo" else -1
        base_pts, opp_pts = (minL, minH) if anchor == "lo" else (minH, minL)
        for s in same:
            if i - s < cfg["min_bars"]:
                continue
            up, lw = hulls[s]
            n = i - s + 1
            later = [p for p in same if s + PM <= p <= i - Pm]
            bp = [q for q in base_pts if s <= q[0] <= i]
            op = [q for q in opp_pts if s <= q[0] <= i]
            both = sorted(bp + op)
            om = [p for p in opp_major if s <= p <= i]
            for j in later:
                m = (X[j] - X[s]) / (j - s)
                b = X[s] - m * s
                hi_res = max(y - m * x - b for x, y in up)
                lo_res = min(y - m * x - b for x, y in lw)
                w0, worst = (hi_res, lo_res) if sg == 1 else (-lo_res, -hi_res)
                if w0 <= 0:
                    continue
                w = max([w0] + [r for r in (sg * (Y[p] - (m * p + b)) for p in om) if r <= (1 + WICK) * w0])
                if worst < -TOL_IN * w:
                    continue
                pokes = [sg * (X[p] - (m * p + b)) for p in later]
                if sum(1 for r in pokes if r < -0.02 * w) > MAX_POKES or min(pokes) < -WICK * w:
                    continue
                lo_b = b if anchor == "lo" else b - w
                lo_now, lo_start = m * i + lo_b, m * s + lo_b
                if lo_now <= 0 or lo_start <= 0:
                    continue
                if not (MIN_WIDTH_PCT <= 100 * w / lo_now <= MAX_WIDTH_PCT and 100 * w / lo_start <= MAX_WIDTH_START_PCT):
                    continue
                tb = _touches(bp, op, m, b, 0, sg, w, TOL_TOUCH)
                if len(tb) < 2 or tb[-1] - tb[0] < 0.3 * n:
                    continue
                to = _touches(op, bp, m, b, w, sg, w, TOL_TOUCH)
                if len(to) < 2:
                    continue
                tm = _touches(both, both, m, b, w / 2, sg, w, TOL_MID)
                if len(tb) + len(to) < 5 and not (len(tb) + len(to) == 4 and len(tm) >= 2):
                    continue
                tlo, thi = (tb, to) if anchor == "lo" else (to, tb)
                out.append({"m": m, "lo_b": lo_b, "hi_b": lo_b + w, "w": w, "first": s, "bars": n, "anchor": anchor,
                            "touch_lo": len(tlo), "touch_hi": len(thi), "touch_mid": len(tm), "touch_at": sorted(tb + to),
                            "mid_at": tm, "quality": len(tb) + len(to) + 0.5 * min(len(tm), 3) + 0.75 * math.log2(n / 120), "i": i})
    return out


def lines(f, k):
    return f["m"] * k + f["lo_b"], f["m"] * k + f["hi_b"]


def distinct(cands, i, k=MAX_PER_COIN):
    """The best channels that are not near-copies of a better one (both lines within 15% of the width, now and at
    the later start)."""
    keep = []
    for f in sorted(cands, key=lambda f: (-f["quality"], -f["bars"])):
        dup = False
        for g in keep:
            w = max(f["w"], g["w"])
            t = max(f["first"], g["first"])
            if all(abs(lines(f, z)[q] - lines(g, z)[q]) < 0.15 * w for z in (i, t) for q in (0, 1)):
                dup = True
                break
        if not dup:
            keep.append(f)
            if len(keep) >= k:
                break
    return keep


def find(h, l, c, i, tf="1d", k=MAX_PER_COIN):
    return distinct(scan(h, l, c, i, tf), i, k)


def _mid(f, k):
    lo, hi = lines(f, k)
    return (lo + hi) / 2


def state(f, o, h, l, c, i):
    """Status of channel f at bar i (see the module notes). None once the channel is over."""
    w = f["w"]
    lo, hi = lines(f, i)
    pos = (c[i] - lo) / w
    if pos < -0.5 or pos > 1.5:
        return None
    ev = None
    for j in range(i, max(i - 3, f["first"] + 6), -1):          # events in the last 3 candles, newest first
        loj, hij = lines(f, j)
        inside5 = all(lines(f, j - z)[0] <= c[j - z] <= lines(f, j - z)[1] for z in range(1, 6))
        if c[j] > hij + BREAK_W * w and inside5:
            ev = ("breakout", j, hij)
            break
        if c[j] < loj - BREAK_W * w and inside5:
            ev = ("breakdown", j, loj)
            break
        below = all(c[j - z] < _mid(f, j - z) for z in range(1, 6))
        above = all(c[j - z] > _mid(f, j - z) for z in range(1, 6))
        if c[j] > _mid(f, j) and below and c[i] > _mid(f, i):
            ev = ("mid_reclaim", j, _mid(f, j))
            break
        if c[j] < _mid(f, j) and above and c[i] < _mid(f, i):
            ev = ("mid_loss", j, _mid(f, j))
            break
    if ev:
        return {"status": ev[0], "event_i": ev[1], "level": ev[2], "pos": pos}
    mid = _mid(f, i)
    # a midline retest that held: reclaimed 3-40 candles ago, closes above the midline since, dipped to it now
    for j in range(i - 3, max(i - 41, f["first"] + 6), -1):
        if c[j] > _mid(f, j) and all(c[j - z] < _mid(f, j - z) for z in range(1, 6)):
            if all(c[z] > _mid(f, z) for z in range(j, i + 1)) and min(l[i - 2:i + 1]) <= mid + 0.05 * w and c[i] > o[i]:
                return {"status": "mid_retest", "event_i": i, "level": mid, "pos": pos}
            break
    touched_bot = min(l[i - z] - lines(f, i - z)[0] for z in range(3)) <= 0.10 * w
    touched_top = max(h[i - z] - lines(f, i - z)[1] for z in range(3)) >= -0.10 * w
    if pos > 1:
        st = "above_top"
    elif pos < 0:
        st = "below_bottom"
    elif touched_bot and 0.15 <= pos <= 0.40 and c[i] > o[i]:
        st = "bottom_bounce"
    elif pos <= EDGE:
        st = "near_bottom"
    elif touched_top and pos <= 0.85 and c[i] < o[i]:
        st = "top_reject"
    elif pos >= 1 - EDGE:
        st = "near_top"
    else:
        st = "inside"
    return {"status": st, "event_i": None, "level": None, "pos": pos}


# ---------------------------------------------------------------- website export
TOP_DAILY, TOP_4H, MIN_ADV = 300, 200, 2e6
LABEL = {"breakout": "Breakout (above the top)", "breakdown": "Breakdown (below the bottom)", "mid_reclaim": "Midline reclaimed",
         "mid_loss": "Midline lost", "mid_retest": "Midline retest held", "bottom_bounce": "Bounce off the bottom",
         "near_bottom": "Near the bottom", "near_top": "Near the top", "top_reject": "Rejected at the top",
         "above_top": "Above the top (no break yet)", "below_bottom": "Below the bottom (no break yet)", "inside": "Inside the channel"}
BULLISH = {"breakout", "mid_reclaim", "mid_retest", "bottom_bounce", "near_bottom"}


def _r(x):
    return None if x is None else float(f"{x:.7g}")


def rows_for(sym, tf, times, o, h, l, c, v, rank):
    i = len(c) - 1
    out = []
    for k, f in enumerate(find(h, l, c, i, tf)):
        s = state(f, o, h, l, c, i)
        if not s:
            continue
        lo0, hi0 = lines(f, f["first"])
        lo1, hi1 = lines(f, i)
        mid = (lo1 + hi1) / 2
        bpd = TF[tf]["bars_per_day"]
        row = {"symbol": sym, "tf": tf, "n": k, "status": s["status"], "pos": _r(s["pos"]), "close": _r(c[i]),
               "t0": times[f["first"]], "lo0": _r(lo0), "hi0": _r(hi0), "t1": times[i], "lo1": _r(lo1), "hi1": _r(hi1),
               "slope": _r(f["m"]), "bars": f["bars"], "days": round(f["bars"] / bpd, 1),
               "width_pct": round(100 * f["w"] / lo1, 1), "slope_pct_day": round(100 * f["m"] * bpd / mid, 2),
               "touches": [f["touch_lo"], f["touch_hi"]], "mid_touches": f["touch_mid"], "quality": round(f["quality"], 1),
               "touch_t": [times[x] for x in f["touch_at"]], "mid_t": [times[x] for x in f["mid_at"]],
               "to_bottom_pct": round(100 * (lo1 / c[i] - 1), 1), "to_mid_pct": round(100 * (mid / c[i] - 1), 1),
               "to_top_pct": round(100 * (hi1 / c[i] - 1), 1), "liq_rank": rank}
        if s["event_i"] is not None:
            j = s["event_i"]
            row.update(event_t=times[j], level=_r(s["level"]), ago=i - j)
            if j + 1 <= i and o[j + 1] > 0:
                sg = 1 if s["status"] in BULLISH else -1
                row["so_far_pct"] = round(100 * sg * (c[i] / o[j + 1] - 1), 1)
        out.append(row)
    return out


def history(stats, x):
    """Track record (since 2022, daily charts) of x's status for the most specific matching group: long-term
    channels (365+ days), narrow ones (< 60% wide), else all. rel30 = median 30-day result of the coin minus the
    average liquid coin (positive = the coin did better); beat30 = share of cases where it did better."""
    if x["tf"] != "1d" or x["status"] not in stats:
        return None
    S = stats[x["status"]]
    g = "late_long" if x["days"] >= 365 and S.get("late_long") else "late_narrow" if x["width_pct"] < 60 and S.get("late_narrow") else "late"
    v = S.get(g)
    if not v:
        return None
    sg = 1 if x["status"] in BULLISH else -1
    return {"group": g, "n": v["n"], "rel30": round(sg * v["edge30"], 1), "beat30": v["beat30"] if sg == 1 else 100 - v["beat30"],
            "move30": round(sg * v["med30"], 1)}


ORDER = {"breakout": 0, "breakdown": 0, "mid_reclaim": 1, "mid_loss": 1, "mid_retest": 1, "bottom_bounce": 2, "near_bottom": 2,
         "top_reject": 3, "near_top": 3, "above_top": 4, "below_bottom": 4, "inside": 5}


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
        r = con.execute("SELECT AVG(volume_usdt) FROM (SELECT volume_usdt FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 20)", (sym,)).fetchone()
        n = con.execute("SELECT COUNT(*) FROM daily WHERE symbol=?", (sym,)).fetchone()[0]
        if r[0] and r[0] >= MIN_ADV and n >= 200:
            liq.append((r[0], sym))
    liq.sort(reverse=True)
    rank = {s: k + 1 for k, (_, s) in enumerate(liq)}
    rows = []
    for _, sym in liq[:TOP_DAILY]:
        d = con.execute("SELECT date, open, high, low, close, volume_usdt FROM daily WHERE symbol=? ORDER BY date DESC LIMIT ?",
                        (sym, TF["1d"]["lookback"] + 50)).fetchall()[::-1]
        if len(d) < 200:
            continue
        rows += rows_for(sym, "1d", [r[0] for r in d], *([r[k] or 0.0 for r in d] for k in range(1, 6)), rank[sym])
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='candles_4h'").fetchone():
        import h4_export
        have = {s for (s,) in con.execute("SELECT symbol FROM h4_state WHERE backfilled=1")}
        for _, sym in [z for z in liq if z[1] in have][:TOP_4H]:
            d = h4_export._series(con, sym, now_ms)[-(TF["4h"]["lookback"] + 50):]
            if len(d) < 300:
                continue
            rows += rows_for(sym, "4h", [r[0] // 1000 for r in d], *([r[k] or 0.0 for r in d] for k in range(1, 6)), rank[sym])
    stats = {}
    p = stats_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "chan_stats.json")
    if os.path.exists(p):
        with open(p) as f:
            stats = json.load(f)
    for x in rows:                              # the measured history of this status for this kind of channel
        h = history(stats, x)
        if h:
            x["hist"] = h
    rows.sort(key=lambda x: (ORDER[x["status"]] if x["status"] in ("breakout", "breakdown") else 1 if "hist" in x else 5,
                             -(x.get("hist") or {}).get("rel30", -99), -x["quality"], x["liq_rank"]))
    return {"rows": rows, "labels": LABEL, "stats": stats, "bullish": sorted(BULLISH),
            "rules": {"edge": EDGE, "break_w": BREAK_W, "tol_touch": TOL_TOUCH, "max_per_coin": MAX_PER_COIN,
                      "top_daily": TOP_DAILY, "top_4h": TOP_4H, "lookback_days": {k: round(v["lookback"] / v["bars_per_day"]) for k, v in TF.items()},
                      "step": {k: v["step"] for k, v in TF.items()}}}
