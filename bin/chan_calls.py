"""
Channel calls (Python 3.9 stdlib only): coins whose daily channel gives an entry AND whose other indicators agree.

A call needs all of:
  * a daily channel (bin/chan.py) in an entry status:
      long:  near_bottom, bottom_bounce, mid_retest
      short / caution: mid_loss, top_reject
  * at least one PROVEN signal in the same direction on the pair: fired on the daily chart in the last 3 days or on
    the latest closed weekly candle (proven = 100+ cases, 30-day edge vs the average pair with the same sign in the
    full history and in both halves, up to 2021 and 2022 on: the same test the automatic shortlist uses);
  * the daily bull score (0-10 confluence of trend and momentum) on the right side: 5+ for longs, 5 or less for
    shorts.
One call per coin (its best channel). Score = channel quality + 2 per agreeing proven signal (+1 if volume
confirmed) + the status's measured 30-day result vs the average coin + the bull score's distance from neutral.
Grade A = the long statuses with the best measured record (near the bottom, bounce off the bottom, midline retest);
B = the shorts (midline lost, rejected at the top). The full record is in calls_stats.json (bin/calls_study.py).
Plan: the stop sits 10% of the channel width beyond the line the entry depends on (the bottom line for bottom
entries, the midline for midline reclaims / retests, the top line for breakouts; for shorts the line that was lost
or rejected). Targets: the midline, then the far line (a breakout: half a width above the top). A call is kept only
when the stop is within MAX_RISK of the price and the first target pays at least MIN_RR times the risk.
"""
from datetime import datetime, timedelta

# Statuses kept after bin/calls_study.py (2022 on, out of sample, average R per call): mid_retest +0.22, near_bottom
# +0.18, mid_loss +0.10. Dropped: mid_reclaim +0.04 and breakout +0.03 (no real edge), breakdown -0.23.
LONG = {"near_bottom", "bottom_bounce", "mid_retest"}
SHORT = {"mid_loss", "top_reject"}
GRADE_A = {"near_bottom", "bottom_bounce", "mid_retest"}   # the statuses with the best measured record
REPOST_DAYS = 7
MAX_RISK, MIN_RR = 0.20, 1.5


def _r(x):
    return None if x is None else float(f"{x:.7g}")


def build(con, chan_rows, signal_stats, last_date):
    import auto_picks
    bull, bear = auto_picks.proven(signal_stats)
    frm = (datetime.strptime(last_date, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    wk = con.execute("SELECT MAX(period_start) FROM signals WHERE tf='1w' AND provisional=0").fetchone()[0] or ""
    sig = {}
    for tf, ps, sym, name, d, conf in con.execute(
            "SELECT tf, period_start, symbol, signal, direction, confirmed FROM signals WHERE provisional=0 AND warm=1 AND "
            "((tf='1d' AND period_start BETWEEN ? AND ?) OR (tf='1w' AND period_start=?))", (frm, last_date, wk)):
        if (tf, name, d) in (bull if d == "bullish" else bear if d == "bearish" else ()):
            sig.setdefault((sym, d), {})[tf + ":" + name] = bool(conf)
    latest = {s: (bs, rsi, rs30) for s, bs, rsi, rs30 in con.execute("SELECT symbol, d_bull_score, d_rsi14, d_rs30 FROM latest")}
    best = {}
    for x in chan_rows:
        if x.get("tf") != "1d" or x["status"] not in LONG | SHORT:
            continue
        side = "long" if x["status"] in LONG else "short"
        agree = sig.get((x["symbol"], "bullish" if side == "long" else "bearish"))
        bs, rsi, rs30 = latest.get(x["symbol"], (None, None, None))
        if not agree or bs is None or (side == "long" and bs < 5) or (side == "short" and bs > 5):
            continue
        h = x.get("hist") or {}
        rel = h.get("rel30")
        sg = 1 if side == "long" else -1
        score = (x["quality"] + 2 * len(agree) + (1 if any(agree.values()) else 0)
                 + (sg * rel if rel is not None else 0) + 0.3 * sg * (bs - 5))
        lo, hi, close = x["lo1"], x["hi1"], x["close"]
        w, mid = hi - lo, (lo + hi) / 2
        if side == "long":
            line = {"near_bottom": lo, "bottom_bounce": lo, "mid_reclaim": mid, "mid_retest": mid, "breakout": hi}[x["status"]]
            stop = line - 0.1 * w
            t1 = mid if close < mid - 0.05 * w else hi if close < hi - 0.05 * w else hi + w / 2
            t2 = hi if t1 == mid else None
        else:
            line = {"breakdown": lo, "mid_loss": mid, "top_reject": hi}[x["status"]]
            stop = line + 0.1 * w
            t1 = mid if close > mid + 0.05 * w else lo
            t2 = lo if t1 != lo else None
        risk = abs(close - stop)
        rr = abs(t1 - close) / risk if risk > 0 and (t1 - close) * sg > 0 else None
        if risk <= 0 or risk / close > MAX_RISK or rr is None or rr < MIN_RR:
            continue                                # a great channel is not a great entry when the stop is too far
        grade = "A" if x["status"] in GRADE_A else "B"
        row = {"symbol": x["symbol"], "side": side, "grade": grade, "score": round(score, 1), "status": x["status"],
               "date": last_date, "close": close, "stop": _r(stop), "t1": _r(t1), "t2": _r(t2), "rr": None if rr is None else round(rr, 1),
               "risk_pct": round(100 * risk / close, 1) if close else None,
               "signals": sorted(agree), "confirmed": any(agree.values()), "bull_score": bs, "rsi": None if rsi is None else round(rsi),
               "rs30": None if rs30 is None else round(rs30, 1), "channel": {k: x.get(k) for k in (
                   "t0", "days", "touches", "mid_touches", "quality", "width_pct", "slope_pct_day", "lo1", "hi1", "pos", "n")},
               "hist": h or None, "liq_rank": x.get("liq_rank")}
        if x["symbol"] not in best or row["score"] > best[x["symbol"]]["score"]:
            best[x["symbol"]] = row
    rows = sorted(best.values(), key=lambda r: (r["grade"], -r["score"]))
    import json
    import os
    sp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls_stats.json")
    stats = json.load(open(sp)) if os.path.exists(sp) else {}
    return {"rows": rows, "stats": stats, "rules": {"long": sorted(LONG), "short": sorted(SHORT), "daily_days": 3, "repost_days": REPOST_DAYS,
                                     "max_risk_pct": 100 * MAX_RISK, "min_rr": MIN_RR}}


def log(con, calls, now_utc):
    """Post each NEW call to the highlights feed (highlight_log, kind 'chan_call'). A coin is posted again only when
    its side flips or REPOST_DAYS have passed since its last post."""
    import json
    # calls posted before the stop / reward rule existed (first run, 2026-10-26) that would not pass it are withdrawn
    for sym, ps, det in con.execute("SELECT symbol, period_start, detail FROM highlight_log WHERE kind='chan_call' AND first_seen_utc < '2026-10-27'").fetchall():
        try:
            d = json.loads(det or "{}")
        except ValueError:
            d = {}
        risk = abs(d.get("close", 0) - d.get("stop", 0)) / d["close"] if d.get("close") else 1
        if risk > MAX_RISK or (d.get("rr") or 0) < MIN_RR:
            con.execute("DELETE FROM highlight_log WHERE kind='chan_call' AND symbol=? AND period_start=?", (sym, ps))
    since = (datetime.strptime(now_utc[:10], "%Y-%m-%d") - timedelta(days=REPOST_DAYS)).strftime("%Y-%m-%d")
    recent = {(s, d) for s, d in con.execute(
        "SELECT symbol, direction FROM highlight_log WHERE kind='chan_call' AND period_start > ?", (since,))}
    new = []
    for x in calls["rows"]:
        d = "bullish" if x["side"] == "long" else "bearish"
        if (x["symbol"], d) in recent:
            continue
        det = {k: x[k] for k in ("grade", "status", "close", "stop", "t1", "t2", "rr", "signals", "bull_score")}
        det["days"] = x["channel"]["days"]
        det["touches"] = x["channel"]["touches"]
        new.append((x["symbol"], "1d", x["date"], "chan_call", d, json.dumps(det, separators=(",", ":")), now_utc))
    con.executemany("INSERT OR IGNORE INTO highlight_log VALUES(?,?,?,?,?,?,?)", new)
    con.commit()
    return len(new)
