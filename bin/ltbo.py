"""
Long-term channel breakouts with a full confirmation dossier (Python 3.9 stdlib only).

A breakout = a daily close above the top line of a channel that is 250+ days long (bin/chan.py: by 5% of the width,
after 5 closes inside). Each one is recorded once (table ltbo) and followed for 180 days.

The grade uses only what the research showed matters (research/ltbo.py, research/ltbo_exit.py, 2018-2026, no
look-ahead), all known at the breakout close:
  1. RSI(14, daily) between 55 and 75 at the breakout: strong but not already overheated;
  2. the channel was flat or falling (a breakout from a range or a downtrend, not an acceleration of an uptrend);
  3. the close is within 10% of the top line: not already extended.
  Grade A = 3 of 3, B = 2, C = 0-1. Since 2022, grade A breakouts: median +9.6% after 90 days (55% up), 60% gained
  50%+ and 38% doubled at some point within 180 days; grade C: median -27.6%.
Follow-up: the first weekly close after the breakout must hold above the line; when it failed, the median 90-day
result since 2022 was -35.9%. Shown as held / failed / pending.
Context shown but NOT graded, because it did not separate winners from losers in the test: breakout volume, the
200-day average, the 50/200 cross, BTC's trend, strength vs BTC, proven signals, the bull score, futures funding.
"""
import json
import os
from datetime import datetime, timedelta, timezone

MIN_DAYS, KEEP_DAYS, RSI_LO, RSI_HI, MAX_EXT = 250, 180, 55, 75, 10.0
SCHEMA = """CREATE TABLE IF NOT EXISTS ltbo(symbol TEXT, event_date TEXT, t0 TEXT, level REAL, slope REAL, width REAL,
    close REAL, grade TEXT, checks TEXT, first_seen TEXT, PRIMARY KEY(symbol, event_date))"""


def rsi14(c):
    if len(c) < 30:
        return None
    g = l_ = 0.0
    for k in range(1, 15):
        d = c[k] - c[k - 1]
        g += max(d, 0)
        l_ += max(-d, 0)
    g, l_ = g / 14, l_ / 14
    for k in range(15, len(c)):
        d = c[k] - c[k - 1]
        g = (g * 13 + max(d, 0)) / 14
        l_ = (l_ * 13 + max(-d, 0)) / 14
    return 100.0 if l_ == 0 else 100 - 100 / (1 + g / l_)


def grade(checks):
    n = sum(1 for k in ("rsi_ok", "flat_or_falling", "not_extended") if checks.get(k))
    return "A" if n == 3 else "B" if n == 2 else "C"


def record(con, chan_rows, now_utc):
    """Store new long-term breakouts from the channel scan (daily rows with status 'breakout')."""
    con.execute(SCHEMA)
    new = 0
    for x in chan_rows:
        if x.get("tf") != "1d" or x["status"] != "breakout" or (x.get("days") or 0) < MIN_DAYS or not x.get("event_t"):
            continue
        ev = x["event_t"]
        if con.execute("SELECT 1 FROM ltbo WHERE symbol=? AND event_date=?", (x["symbol"], ev)).fetchone():
            continue
        d = con.execute("SELECT date, close, volume_usdt FROM daily WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 300",
                        (x["symbol"], ev)).fetchall()[::-1]
        if len(d) < 40 or d[-1][0] != ev:
            continue
        closes = [r[1] for r in d]
        rsi = rsi14(closes)
        level = x["level"]
        ext = 100 * (closes[-1] / level - 1) if level else None
        vavg = sum(r[2] or 0 for r in d[-21:-1]) / 20
        checks = {"rsi": None if rsi is None else round(rsi, 1), "rsi_ok": rsi is not None and RSI_LO <= rsi <= RSI_HI,
                  "slope_pct_day": x.get("slope_pct_day"), "flat_or_falling": (x.get("slope_pct_day") or 0) <= 0.05,
                  "ext_pct": None if ext is None else round(ext, 1), "not_extended": ext is not None and ext < MAX_EXT,
                  "rvol": round((d[-1][2] or 0) / vavg, 1) if vavg else None, "days": x.get("days"), "touches": x.get("touches"),
                  "width_pct": x.get("width_pct")}
        con.execute("INSERT OR IGNORE INTO ltbo VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (x["symbol"], ev, x["t0"], level, x.get("slope"), (x["hi1"] - x["lo1"]), closes[-1], grade(checks),
                     json.dumps(checks, separators=(",", ":")), now_utc))
        new += 1
    con.commit()
    return new


def _context(con, sym, btc_up, sig, deriv):
    r = con.execute("SELECT d_bull_score, d_sma200, d_rs30, d_rsi14, w_rsi14, d_dist_ath FROM latest WHERE symbol=?", (sym,)).fetchone()
    out = {}
    if r:
        out = {"bull_score": r[0], "above_200d": None if r[1] is None else r[1] > 0, "dist_200d_pct": r[1], "rs30": r[2],
               "rsi_now": r[3], "weekly_rsi": r[4], "from_ath_pct": r[5]}
    out["btc_above_200d"] = btc_up
    out["proven_signals"] = sorted(sig.get(sym, []))
    if deriv.get(sym):
        out["funding_apr"] = deriv[sym].get("funding_apr")
        out["oi_chg_7d"] = deriv[sym].get("oi_chg_7d")
    return out


def export(con, last_date, stats_path=None, signal_stats=None):
    con.execute(SCHEMA)
    p = stats_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ltbo_stats.json")
    stats = {}
    if os.path.exists(p):
        with open(p) as f:
            stats = json.load(f)
    since = (datetime.strptime(last_date, "%Y-%m-%d") - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    # context sources
    b = con.execute("SELECT d_sma200 FROM latest WHERE symbol='BTCUSDT'").fetchone()
    btc_up = None if not b or b[0] is None else b[0] > 0
    sig = {}
    if signal_stats:
        import auto_picks
        bull, _ = auto_picks.proven(signal_stats)
        frm = (datetime.strptime(last_date, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
        for tf, ps, s, name, d in con.execute("SELECT tf, period_start, symbol, signal, direction FROM signals WHERE provisional=0 AND warm=1 "
                                               "AND direction='bullish' AND period_start BETWEEN ? AND ?", (frm, last_date)):
            if (tf, name, d) in bull:
                sig.setdefault(s, set()).add(tf + ":" + name)
    deriv = {}
    try:
        import derivatives
        deriv = derivatives.snapshot(con, [s for (s,) in con.execute("SELECT DISTINCT symbol FROM ltbo WHERE event_date >= ?", (since,))])
    except Exception:
        deriv = {}
    rows = []
    for sym, ev, t0, level, slope, width, close0, gr, checks, seen in con.execute(
            "SELECT * FROM ltbo WHERE event_date >= ? ORDER BY event_date DESC", (since,)).fetchall():
        d = con.execute("SELECT date, high, low, close FROM daily WHERE symbol=? AND date>? ORDER BY date", (sym, ev)).fetchall()
        ck = json.loads(checks or "{}")
        evd = datetime.strptime(ev, "%Y-%m-%d")
        line = lambda day: level + (slope or 0) * (datetime.strptime(day, "%Y-%m-%d") - evd).days   # noqa: E731
        weekly = "pending"
        wk = next((r for r in d if datetime.strptime(r[0], "%Y-%m-%d").weekday() == 6), None) if evd.weekday() != 6 else (ev, None, None, close0)
        if wk:
            weekly = "held" if wk[3] > line(wk[0]) else "failed"
        now = d[-1][3] if d else close0
        line_now = line(d[-1][0]) if d else level
        mx = max([r[1] for r in d], default=close0)
        mn = min([r[2] for r in d], default=close0)
        g = grade(ck)
        hist = (stats.get("late") or {}).get(g)
        rows.append({"symbol": sym, "event_date": ev, "t0": t0, "grade": g, "checks": ck, "weekly": weekly,
                     "close_at_break": close0, "level_at_break": level, "line_now": round(line_now, 10), "price": now,
                     "since_pct": round(100 * (now / close0 - 1), 1), "max_pct": round(100 * (mx / close0 - 1), 1),
                     "min_pct": round(100 * (mn / close0 - 1), 1), "status": "above the line" if now > line_now else "back inside",
                     "days_since": len(d), "target_width": round(line_now + width, 10) if width else None,
                     "context": _context(con, sym, btc_up, sig, deriv), "hist": hist})
    order = {"A": 0, "B": 1, "C": 2}
    rows.sort(key=lambda r: (r["days_since"] > 10, order[r["grade"]], r["weekly"] == "failed", -r["since_pct"]))
    return {"rows": rows, "stats": stats, "rules": {"min_days": MIN_DAYS, "keep_days": KEEP_DAYS, "rsi": [RSI_LO, RSI_HI], "max_ext_pct": MAX_EXT}}


def log(con, data, now_utc):
    """Post each new breakout to the highlights feed once (kind 'ltbo')."""
    have = {(s, ps) for s, ps in con.execute("SELECT symbol, period_start FROM highlight_log WHERE kind='ltbo'")}
    new = []
    for x in data["rows"]:
        if (x["symbol"], x["event_date"]) in have or x["days_since"] > 3:
            continue
        det = {"grade": x["grade"], "checks": x["checks"], "level": x["level_at_break"], "close": x["close_at_break"],
               "days": x["checks"].get("days"), "hist": x["hist"]}
        new.append((x["symbol"], "1d", x["event_date"], "ltbo", "bullish", json.dumps(det, separators=(",", ":")), now_utc))
    con.executemany("INSERT OR IGNORE INTO highlight_log VALUES(?,?,?,?,?,?,?)", new)
    con.commit()
    return len(new)


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
