"""
Head-and-shoulders patterns on the daily chart, tops and inverse (bottoms) (Python 3.9 stdlib only).

Pattern (top; the inverse is the mirror image on the lows):
  * three swing highs (left shoulder, head, right shoulder; a swing = the highest high P candles either side, so it is
    known P candles later) with the head above both shoulders by 20%+ of the pattern height, shoulders within 35% of
    the height of each other, the head roughly centred (time ratio 0.4-2.5), all within MAX_SPAN candles;
  * two troughs (the deepest point between each shoulder and the head) define the neckline, extended as a straight line;
    they may differ by at most half the height. Height = head - neckline under the head;
  * a prior move into the pattern: within 30 candles before the left shoulder, price was at least half a height below
    the neckline (tops) / above it (inverse).
Two swing sizes are scanned (P = 5 and 10 candles) so both compact and large patterns are found.

Status at a candle (first match; nothing uses candles after it):
  near_neckline  right shoulder confirmed, no close through the neckline yet, price within NEAR of the height from it
  broken         first close through the neckline in the last BREAK_FRESH candles, not yet back at the neckline
  retest         after the break price moved away (AWAY of the height), then came back to the neckline (within
                 RETEST of the height) in the last 3 candles, and the latest close
                 is still on the break side or within RETEST of it: the classic second entry
  after_break    broken earlier, still on the break side, not at target  (shown as context only)
A pattern is over when the right shoulder is taken out before the break, price closes back through the neckline by
more than FAIL of the height after the break, the measured target (neckline -/+ height) is reached, or it is older
than MAX_AGE candles after the break.
"""
P_SIZES = (5, 10)
MAX_SPAN, NEAR, BREAK_FRESH, RETEST, FAIL, MAX_AGE, MAX_WAIT, AWAY = 160, 0.25, 5, 0.15, 0.35, 60, 40, 0.3


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


def find(h, l, c, P):
    """All patterns in the series (each usable only from its 'known' candle on)."""
    out = []
    for kind, X, Y, sg in (("top", h, l, 1), ("inverse", l, h, -1)):
        # sg = +1: peaks are highs, troughs are lows; inverse: peaks are lows (sg flips comparisons)
        peaks = pivots(X, P, sg == 1)
        pick = (lambda t: Y[t]) if sg == 1 else (lambda t: -Y[t])
        for j in range(2, len(peaks)):
            lsh, hd, rsh = peaks[j - 2], peaks[j - 1], peaks[j]
            if rsh - lsh > MAX_SPAN or hd - lsh < 3 or rsh - hd < 3:
                continue
            # the troughs: the deepest point between each shoulder and the head (as a trader draws the neckline)
            t1, t2 = min(range(lsh + 1, hd), key=pick), min(range(hd + 1, rsh), key=pick)
            Hd, Ls, Rs = sg * X[hd], sg * X[lsh], sg * X[rsh]
            if not (Hd > Ls and Hd > Rs):
                continue
            m = (Y[t2] - Y[t1]) / (t2 - t1)
            neck_hd = Y[t1] + m * (hd - t1)
            height = Hd - sg * neck_hd
            if height <= 0 or abs(Ls - Rs) > 0.35 * height or Hd - max(Ls, Rs) < 0.2 * height:
                continue
            if not (0.4 <= (hd - lsh) / max(1, rsh - hd) <= 2.5) or abs(Y[t1] - Y[t2]) > 0.5 * height:
                continue
            pre = max(0, lsh - 30)
            prior = min(sg * Y[k] for k in range(pre, lsh + 1))
            if not prior < sg * neck_hd - 0.5 * height:
                continue
            if neck_hd <= 0 or (sg == -1 and Y[t1] <= 0):
                continue
            out.append({"kind": kind, "sg": sg, "P": P, "lsh": lsh, "hd": hd, "rsh": rsh, "t1": t1, "t2": t2, "m": m,
                        "b": Y[t1] - m * t1, "height": height, "known": rsh + P})
    return out


def neck(p, k):
    return p["m"] * k + p["b"]


def state(p, o, h, l, c, i):
    """Status of pattern p at candle i, or None when it is not (or no longer) relevant. Direction of the trade:
    top -> down, inverse -> up. 'dist' = (close - neckline) in heights, positive = on the break side."""
    if i < p["known"]:
        return None
    sg, H = p["sg"], p["height"]
    shoulder = sg * (h if sg == 1 else l)[p["rsh"]]
    brk = None
    for k in range(p["known"] - p["P"] + 1, i + 1):
        if brk is None:
            if sg * (h if sg == 1 else l)[k] > shoulder:
                return None                                   # right shoulder taken out before the break: failed
            if sg * c[k] < sg * neck(p, k):
                brk = k
            elif k - p["rsh"] > MAX_WAIT:
                return None                                   # no break within MAX_WAIT candles of the right shoulder
    nk = neck(p, i)
    dist = sg * (nk - c[i]) / H                               # > 0: beyond the neckline on the break side
    target = nk - sg * H
    if brk is None:
        if dist >= -NEAR:
            return {"status": "near_neckline", "dist": dist, "neck": nk, "target": target, "brk": None}
        return {"status": "pattern", "dist": dist, "neck": nk, "target": target, "brk": None}
    if i - brk > MAX_AGE:
        return None
    for k in range(brk + 1, i + 1):
        if sg * c[k] > sg * neck(p, k) + FAIL * H:
            return None                                       # closed back through the neckline: failed
        if (sg == 1 and l[k] <= neck(p, k) - H) or (sg == -1 and h[k] >= neck(p, k) + H):
            return None                                       # measured target reached: done
    # a retest: after the break price first moved AWAY from the neckline (by AWAY of the height), then came back to
    # within RETEST of it
    touched, away = None, False
    for k in range(brk + 1, i + 1):
        if sg * (neck(p, k) - c[k]) / H >= AWAY:
            away = True
        elif away and (sg * (neck(p, k) - (h if sg == 1 else l)[k]) / H) <= RETEST:
            touched = k
    st = "after_break"
    if i - brk < BREAK_FRESH and touched is None:
        st = "broken"
    elif touched is not None and i - touched <= 3 and dist >= -RETEST:
        st = "retest"
    return {"status": st, "dist": dist, "neck": nk, "target": target, "brk": brk, "retest_k": touched}


# ---------------------------------------------------------------- website export
TOP, MIN_ADV, BARS = 300, 2e6, 420
LABEL = {"near_neckline": "Near the neckline", "broken": "Neckline broken", "retest": "Retesting the neckline",
         "after_break": "Broken earlier"}


def _r(x):
    return None if x is None else float(f"{x:.7g}")


def rows_for(sym, dates, o, h, l, c, rank, stats):
    i, out, seen = len(c) - 1, [], set()
    for P in P_SIZES:
        for p in find(h, l, c, P):
            if p["hd"] in seen:
                continue
            st = state(p, o, h, l, c, i)
            if not st or st["status"] == "pattern":
                continue
            seen.add(p["hd"])
            sg, H = p["sg"], p["height"]
            stop = (h if sg == 1 else l)[p["rsh"]] if st["brk"] is None else st["neck"] + sg * FAIL * H
            hist = (stats.get(f"{p['kind']}:{st['status']}") or {}).get("late")
            ext = max(10, (i - p["lsh"]) // 4)
            out.append({"symbol": sym, "kind": p["kind"], "status": st["status"], "close": _r(c[i]), "neck": _r(st["neck"]),
                        "target": _r(st["target"]), "stop": _r(stop), "height_pct": round(100 * H / st["neck"], 1) if st["neck"] else None,
                        "dist": round(st["dist"], 2), "to_neck_pct": round(100 * (st["neck"] / c[i] - 1), 1),
                        "to_target_pct": round(100 * (st["target"] / c[i] - 1), 1), "date": dates[i],
                        "break_date": dates[st["brk"]] if st["brk"] is not None else None,
                        "retest_date": dates[st["retest_k"]] if st.get("retest_k") is not None else None,
                        "points": {"ls": [dates[p["lsh"]], _r((h if sg == 1 else l)[p["lsh"]])], "head": [dates[p["hd"]], _r((h if sg == 1 else l)[p["hd"]])],
                                   "rs": [dates[p["rsh"]], _r((h if sg == 1 else l)[p["rsh"]])],
                                   "neck": [[dates[p["t1"]], _r(neck(p, p["t1"]))], [dates[i], _r(st["neck"])]],
                                   "neck_slope": _r(p["m"]), "ext": ext},
                        "days": i - p["lsh"], "liq_rank": rank, "hist": hist})
    return out


def run(con, stats_path=None):
    import json
    import os
    from binance_sync import STABLE_BASES
    from bot_core import excluded_tokens
    p = stats_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "hs_stats.json")
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
        if r[0] and r[0] >= MIN_ADV:
            liq.append((r[0], sym))
    liq.sort(reverse=True)
    rows = []
    for rank, (_, sym) in enumerate(liq[:TOP], 1):
        d = con.execute("SELECT date, open, high, low, close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT ?", (sym, BARS)).fetchall()[::-1]
        if len(d) < 120:
            continue
        rows += rows_for(sym, [x[0] for x in d], *([x[k] or 0.0 for x in d] for k in range(1, 5)), rank, stats)
    order = {"retest": 0, "broken": 1, "near_neckline": 2, "after_break": 3}
    rows.sort(key=lambda x: (order[x["status"]], x["liq_rank"]))
    return {"rows": rows, "labels": LABEL, "stats": stats,
            "rules": {"top": TOP, "p_sizes": list(P_SIZES), "near": NEAR, "retest": RETEST, "fail": FAIL, "max_age": MAX_AGE}}


def log(con, data, now_utc):
    """Post each new near-neckline / break / retest to the highlights feed once (kind 'hs_event')."""
    import json
    have = set()
    for sym, det in con.execute("SELECT symbol, detail FROM highlight_log WHERE kind='hs_event'"):
        try:
            d = json.loads(det or "{}")
        except ValueError:
            continue
        have.add((sym, d.get("head"), d.get("status")))
    new = []
    for x in data["rows"]:
        if x["status"] == "after_break" or (x["symbol"], x["points"]["head"][0], x["status"]) in have:
            continue                                        # one post per pattern and status
        det = {k: x[k] for k in ("kind", "status", "close", "neck", "target", "stop", "to_target_pct")}
        det["head"] = x["points"]["head"][0]
        new.append((x["symbol"], "1d", x["date"], "hs_event", "bearish" if x["kind"] == "top" else "bullish",
                    json.dumps(det, separators=(",", ":")), now_utc))
    con.executemany("INSERT OR IGNORE INTO highlight_log VALUES(?,?,?,?,?,?,?)", new)
    con.commit()
    return len(new)
