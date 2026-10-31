"""
Channel calls (Python 3.9 stdlib only): coins whose daily channel gives an entry AND whose other indicators agree.

Rules v2 (Bilal, 31 Oct 2026: fewer, more confident calls, 2R+ reward, smaller stops), chosen with research/calls_lab.py
on the full history, out of sample 2022-2026, because they were positive in EVERY year, not only on average:
  long   status near_bottom / bottom_bounce (price really touched the bottom line: a low within 1.5% of it in the last 3
         candles) or mid_retest (touched the midline); at least one PROVEN bullish signal (daily, last 3 days, or the
         latest weekly candle); daily bull score 5+. Stop 10% of the channel width under the line. The stop must be within
         MAX_RISK (8%) and the channel's next line at least 3R away. Take profit at 3R.
         Record: 33% winners, +0.29R a call, profit factor 1.42 (old rules: +0.10R, 1.05).
  short  status mid_loss / top_reject with a PROVEN bearish signal. Stop just above the 5-day high (+0.5%), within 8%,
         the next line at least 2.5R away. Take profit at 3R.
         Record: 34% winners, +0.30R a call, profit factor 1.50 (old rules: +0.14R).
Grade A = a daily divergence in the same direction confirmed in the last 10 candles (bin/diverge.py); B = without.
Score ranks calls within a grade (channel quality + agreeing proven signals + bull score). The measured record of the
live rules is read from data/calls_lab.json and published with the calls.
"""
import json
import os
from datetime import datetime, timedelta

LONG = {"near_bottom", "bottom_bounce", "mid_retest"}
SHORT = {"mid_loss", "top_reject"}
REPOST_DAYS = 7
MAX_RISK, TP_R = 0.08, 3.0
MIN_ROOM = {"long": 3.0, "short": 2.5}
MIN_RR = 2.0                 # never post a call whose take-profit pays less than 2x the risk (Bilal, 31 Oct 2026)
LAB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "calls_lab.json")
LAB_ROWS = {"long": {"src": "chan", "stop": "line", "target": "3r", "max_risk": 0.08, "min_rr": 3.0, "filt": "proven+bs+touch"},
            "short": {"src": "chan", "stop": "swing", "target": "3r", "max_risk": 0.08, "min_rr": 2.5, "filt": "proven"}}


def _r(x):
    return None if x is None else float(f"{x:.7g}")


def lab_stats(path=LAB):
    """The live rules' record from the calls lab (None when it has not run)."""
    try:
        with open(path) as f:
            lab = json.load(f)
    except (OSError, ValueError):
        return None
    out = {"source": "research/calls_lab.py, full history, 2022 on, proven signals chosen on data up to 2021 (out of sample)",
           "generated_utc": lab.get("generated_utc")}
    for side, want in LAB_ROWS.items():
        row = next((g for g in lab.get("rules", []) if g.get("side") == side and all(g.get(k) == v for k, v in want.items())), None)
        if row:
            out[side] = {k: row.get(k) for k in ("n", "win", "avg_r", "pf", "avg_pct", "months", "years", "worst_year")}
    for k in ("baseline", "breadth"):
        if lab.get(k):
            out[k] = lab[k]
    d = lab.get("divergence") or {}
    out["divergence"] = {k: d.get(k) for k in ("bull|triple|all|oos", "bull|triple|btc_up|oos", "bull|triple|btc_down|oos", "bear|triple|all|oos") if d.get(k)}
    return out


def recent_divergences(con, symbols, days=10):
    """{symbol: newest divergence confirmed in the last `days` daily candles} from the daily table (empty when the
    table is missing)."""
    import diverge as DV
    out = {}
    try:
        for s in symbols:
            rows = con.execute("SELECT date, open, high, low, close FROM (SELECT * FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 200) ORDER BY date", (s,)).fetchall()
            if len(rows) < 80:
                continue
            dates = [r[0] for r in rows]
            o, h, l, c = ([r[k] for r in rows] for k in range(1, 5))
            ev = DV.events(o, h, l, c, start=len(c) - days)
            if ev:
                e = ev[-1]
                out[s] = {"side": e["side"], "kind": e["kind"], "date": dates[e["i"]], "swing_date": dates[e["j"]],
                          "rsi": [e["rsi_p"], e["rsi_j"]], "px": [_r(e["px_p"]), _r(e["px_j"])]}
    except Exception:
        return out
    return out


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
        if not agree or (side == "long" and (bs is None or bs < 5)):
            continue
        t3 = x.get("touch3") or {}
        if side == "long" and not t3.get("mid" if x["status"] == "mid_retest" else "lo"):
            continue                                # the price never actually reached the line: no "bottom" call
        lo, hi, close = x["lo1"], x["hi1"], x["close"]
        w, mid = hi - lo, (lo + hi) / 2
        sg = 1 if side == "long" else -1
        if side == "long":
            line = mid if x["status"] == "mid_retest" else lo
            stop = line - 0.1 * w
            nxt = mid if close < mid - 0.05 * w else hi if close < hi - 0.05 * w else hi + w / 2
        else:
            if x.get("high5") is None:
                continue
            stop = x["high5"] * 1.005
            nxt = mid if close > mid + 0.05 * w else lo
        risk = sg * (close - stop)
        if risk <= 0 or risk / close > MAX_RISK or sg * (nxt - close) <= 0:
            continue
        room = sg * (nxt - close) / risk
        if room < MIN_ROOM[side] or TP_R < MIN_RR:
            continue                                # not enough room before the next line, or the stop is too far
        t1 = close + sg * TP_R * risk
        h_ = x.get("hist") or {}
        score = x["quality"] + 2 * len(agree) + (1 if any(agree.values()) else 0) + (0.3 * sg * (bs - 5) if bs is not None else 0)
        row = {"symbol": x["symbol"], "side": side, "grade": "B", "score": round(score, 1), "status": x["status"],
               "date": last_date, "close": close, "stop": _r(stop), "t1": _r(t1), "t2": None, "line_target": _r(nxt), "rr": TP_R,
               "room_r": round(room, 1), "risk_pct": round(100 * risk / close, 1), "rules": "v2",
               "signals": sorted(agree), "confirmed": any(agree.values()), "bull_score": bs, "rsi": None if rsi is None else round(rsi),
               "rs30": None if rs30 is None else round(rs30, 1), "channel": {k: x.get(k) for k in (
                   "t0", "days", "touches", "mid_touches", "quality", "width_pct", "slope_pct_day", "lo1", "hi1", "pos", "n")},
               "hist": h_ or None, "liq_rank": x.get("liq_rank")}
        if x["symbol"] not in best or row["score"] > best[x["symbol"]]["score"]:
            best[x["symbol"]] = row
    div = recent_divergences(con, list(best))
    for s, row in best.items():
        d = div.get(s)
        if d and (d["side"] == "bull") == (row["side"] == "long"):
            row["divergence"], row["grade"] = d, "A"
    rows = sorted(best.values(), key=lambda r: (r["grade"], -r["score"]))
    return {"rows": rows, "stats": lab_stats(), "divergences": div,
            "rules": {"version": 2, "long": sorted(LONG), "short": sorted(SHORT), "daily_days": 3, "repost_days": REPOST_DAYS,
                      "max_risk_pct": 100 * MAX_RISK, "take_profit_r": TP_R, "min_room_r": MIN_ROOM, "min_rr": MIN_RR,
                      "touch_pct": 1.5}}


def log(con, calls, now_utc):
    """Post each NEW call to the highlights feed (highlight_log, kind 'chan_call'). A coin is posted again only when
    its side flips or REPOST_DAYS have passed since its last post."""
    # calls posted before the stop / reward rule existed (first run, 2026-10-26) that would not pass it are withdrawn
    for sym, ps, det in con.execute("SELECT symbol, period_start, detail FROM highlight_log WHERE kind='chan_call' AND first_seen_utc < '2026-10-27'").fetchall():
        try:
            d = json.loads(det or "{}")
        except ValueError:
            d = {}
        risk = abs(d.get("close", 0) - d.get("stop", 0)) / d["close"] if d.get("close") else 1
        if risk > 0.20 or (d.get("rr") or 0) < 1.5:        # the rules of that day (v1); later calls are never withdrawn
            con.execute("DELETE FROM highlight_log WHERE kind='chan_call' AND symbol=? AND period_start=?", (sym, ps))
    since = (datetime.strptime(now_utc[:10], "%Y-%m-%d") - timedelta(days=REPOST_DAYS)).strftime("%Y-%m-%d")
    recent = {(s, d) for s, d in con.execute(
        "SELECT symbol, direction FROM highlight_log WHERE kind='chan_call' AND period_start > ?", (since,))}
    new = []
    for x in calls["rows"]:
        d = "bullish" if x["side"] == "long" else "bearish"
        if (x["symbol"], d) in recent:
            continue
        det = {k: x.get(k) for k in ("grade", "status", "close", "stop", "t1", "t2", "rr", "signals", "bull_score", "risk_pct", "rules", "line_target")}
        if x.get("divergence"):
            det["divergence"] = x["divergence"]["kind"]
        det["days"] = x["channel"]["days"]
        det["touches"] = x["channel"]["touches"]
        new.append((x["symbol"], "1d", x["date"], "chan_call", d, json.dumps(det, separators=(",", ":")), now_utc))
    con.executemany("INSERT OR IGNORE INTO highlight_log VALUES(?,?,?,?,?,?,?)", new)
    con.commit()
    return len(new)
