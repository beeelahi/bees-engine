"""
Live view of the paper bots between closes (Python 3.9 stdlib only): web_export/bot_live.json, every engine run.

Why: the bots decide at a candle's close and enter at the very next open (the same moment), but the paper record
books a trade only when the candle after the signal has closed (a day later for the daily bots, 4 hours later for
Fade 4h). So the website showed "order for the next open" while the trade had in fact been running for hours.
This module shows each new entry and each open position as it really stands, from MEXC/Binance 1-hour candles of
the current period (plus the latest 15-minute price):
  * daily bots (bot.json): entry at today's 00:00 UTC open (+ the same slippage as the record), the stop, the
    one-third profit-take and the break-even move, checked hour by hour; positions flagged to exit are shown sold
    at today's open;
  * Fade 4h (paper4h.json): the short entered at the open of the running 4-hour candle, its stop checked hourly.
It is a VIEW: it never changes the paper record. At the close the record books the same trades with the same rules
(the daily record uses daily highs and lows, so on a day that touches both the stop and the profit-take the record
may resolve the day conservatively; the live view uses the hourly order of events).
"""
import json
import os
import time
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
H, DAY, H4 = 3_600_000, 86_400_000, 4 * 3_600_000


def iso_ms(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_ms(d):
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def hourly(con, sym, start_ms, now_ms):
    """Completed 1-hour candles [(open_ms, o, h, l, c)] from start_ms."""
    return con.execute("SELECT open_ms, open, high, low, close FROM candles_1h WHERE symbol=? AND open_ms>=? AND close_ms<? "
                       "ORDER BY open_ms", (sym, start_ms, now_ms)).fetchall()


def walk(bars, entry, stop, tp, tp_frac, be, side="long"):
    """Hour by hour: stop first (conservative), then the profit-take; returns (events, stop now, open share, exit)."""
    ev, share, out = [], 1.0, None
    lg = side == "long"
    for k, (t, o, h, l, c) in enumerate(bars):
        if k > 0 and ((lg and o <= stop) or (not lg and o >= stop)):
            out = ("stop_gap", o, t)
            break
        if (lg and l <= stop) or (not lg and h >= stop):
            out = ("stop", stop, t)
            break
        if tp and ((lg and h >= tp) or (not lg and l <= tp)):
            ev.append({"utc": iso_ms(t), "what": "take_profit", "price": tp, "share": tp_frac})
            share -= tp_frac
            tp = None
            if be:
                stop = max(stop, entry) if lg else min(stop, entry)
    return ev, stop, share, out


def daily_bots(con, bot, live_px, now_ms):
    B = bot.get("data_through")
    if not B:
        return {}
    start = day_ms(B) + DAY                                    # the open right after the last daily close
    out = {}
    for b in bot["bots"]:
        P, cfg, prm = b["paper"], b["strategy"]["config"], b["strategy"]["params"]
        tp_r, tp_frac, be = prm.get("tp_r") or 0, prm.get("tp_frac", 0.5), prm.get("be_after_tp", True)
        eq = P["equity"]
        rows_e, rows_p = [], []
        gross = sum((p.get("qty_open") or 0) * (p.get("last") or 0) for p in P["open"])
        slots = cfg.get("max_pos", 10) - sum(1 for p in P["open"] if not p.get("exit_next_open"))
        for p in P["open"]:
            bars = hourly(con, p["symbol"], start, now_ms)
            lg, slip = p["side"] == "long", p.get("slip") or 0.0
            row = {"symbol": p["symbol"], "side": p["side"], "entry": p["entry"], "stop": p["stop"], "events": []}
            if not bars:
                row["status"] = "waiting for today's first hourly candle"
                rows_p.append(row)
                continue
            if p.get("exit_next_open"):
                px = bars[0][1] * ((1 - slip) if lg else (1 + slip))
                row.update(status="sold at today's open", exit=px, exit_utc=iso_ms(bars[0][0]),
                           pnl_pct=100 * ((px / p["entry"] - 1) if lg else (1 - px / p["entry"])))
                rows_p.append(row)
                continue
            ev, stop, share, x = walk(bars, p["entry"], p["stop"], p.get("tp_price"), tp_frac, be, p["side"])
            row["events"] = ev
            if x:
                why, px, t = x
                px = px * ((1 - slip) if lg else (1 + slip))
                row.update(status="stopped" if why == "stop" else "stopped at the open (gap)", exit=px, exit_utc=iso_ms(t))
            else:
                last = live_px.get(p["symbol"]) or bars[-1][4]
                row.update(status="holding", last=last, stop_now=stop, open_share=round(share, 4))
                px = last
            row["pnl_pct"] = 100 * ((px / p["entry"] - 1) if lg else (1 - px / p["entry"]))
            r0 = abs(p["entry"] - (p.get("stop0") or p["stop"]))
            row["r"] = ((px - p["entry"]) if lg else (p["entry"] - px)) / r0 if r0 else None
            rows_p.append(row)
        for o in P["orders"]:
            if o.get("add"):
                continue
            row = {"symbol": o["symbol"], "side": o["side"], "signal_date": o["signal_date"], "events": []}
            if slots <= 0:
                row["status"] = "no free slot: not entered"
                rows_e.append(row)
                continue
            bars = hourly(con, o["symbol"], start, now_ms)
            if not bars:
                row["status"] = "waiting for today's first hourly candle"
                rows_e.append(row)
                continue
            lg = o["side"] == "long"
            adv = o.get("adv")                             # None: a bot.json from before this field existed
            slip = _slip(adv) if adv is not None else 0.0015
            entry = bars[0][1] * ((1 + slip) if lg else (1 - slip))
            dist = o["stop_dist"]
            stop = entry - dist if lg else entry + dist
            qty = eq * cfg.get("risk", 0.01) / dist if dist > 0 else 0
            cap = min(eq * cfg.get("max_pos_frac", 0.2), adv * cfg.get("max_adv_frac", 0.005) if adv is not None else float("inf"),
                      eq * cfg.get("max_gross", 1.0) - gross)
            if qty * entry > cap:
                qty = cap / entry
            if qty * entry < eq * 0.02:
                row["status"] = "too small to enter (liquidity or exposure cap)"
                rows_e.append(row)
                continue
            slots -= 1
            gross += qty * entry
            tp = (entry + tp_r * dist if lg else entry - tp_r * dist) if tp_r else None
            ev, stop_now, share, x = walk(bars, entry, stop, tp, tp_frac, be, o["side"])
            row.update(entry=entry, entry_utc=iso_ms(bars[0][0]), stop=stop, qty=qty, notional=qty * entry,
                       size_pct=100 * qty * entry / eq if eq else None, tp=tp, events=ev)
            if x:
                why, px, t = x
                row.update(status="stopped", exit=px, exit_utc=iso_ms(t))
            else:
                px = live_px.get(o["symbol"]) or bars[-1][4]
                row.update(status="entered", last=px, stop_now=stop_now, open_share=round(share, 4))
            row["pnl_pct"] = 100 * ((px / entry - 1) if lg else (1 - px / entry))
            row["r"] = ((px - entry) if lg else (entry - px)) / dist if dist else None
            rows_e.append(row)
        out[b["id"]] = {"entries": rows_e, "positions": rows_p}
    return {"since_utc": iso_ms(start), "bots": out}


def _slip(adv):
    import bot_sim
    return bot_sim.slippage(adv)


def fade4h(con, P, live_px, now_ms):
    if not P:
        return {}
    cost = (P.get("rules", {}).get("cost_pct") or 0.15) / 100
    risk = (P.get("rules", {}).get("risk_pct") or 1) / 100
    ents, poss = [], []
    for p in P.get("pending", []):
        start = p["signal_ms"] + H4                            # the candle right after the signal candle
        bars = hourly(con, p["symbol"], start, now_ms)
        row = {"symbol": p["symbol"], "side": "short", "signal_utc": iso_ms(p["signal_ms"] + H4), "stop": p["stop"], "events": []}
        if not bars:
            row["status"] = "waiting for the first hourly candle"
            ents.append(row)
            continue
        entry = bars[0][1] * (1 - cost)
        if p["stop"] <= entry:
            row["status"] = "opened above the stop: skipped"
            ents.append(row)
            continue
        _, _, _, x = walk(bars, entry, p["stop"], None, 0, False, "short")
        row.update(entry=entry, entry_utc=iso_ms(bars[0][0]), qty=risk * P["equity"] / (p["stop"] - entry))
        if x:
            row.update(status="stopped", exit=x[1] * (1 + cost), exit_utc=iso_ms(x[2]))
            px = row["exit"]
        else:
            px = live_px.get(p["symbol"]) or bars[-1][4]
            row.update(status="entered", last=px)
        row["pnl_pct"] = 100 * (1 - px / entry)        # a short: gain as a share of the entry
        row["r"] = (entry - px) / (p["stop"] - entry)
        ents.append(row)
    for q in P.get("open", []):
        start = q["entry_ms"] + q.get("bars", 0) * H4          # the running 4-hour candle
        bars = hourly(con, q["symbol"], start, now_ms)
        row = {"symbol": q["symbol"], "side": "short", "entry": q["entry"], "stop": q["stop"], "events": []}
        _, _, _, x = walk(bars, q["entry"], q["stop"], None, 0, False, "short") if bars else ([], 0, 0, None)
        if x:
            row.update(status="stopped", exit=x[1] * (1 + cost), exit_utc=iso_ms(x[2]))
            px = row["exit"]
        else:
            px = live_px.get(q["symbol"]) or (bars[-1][4] if bars else q.get("price"))
            row.update(status="holding", last=px)
        if px:
            row["pnl_pct"] = 100 * (1 - px / q["entry"])
            row["r"] = (q["entry"] - px) / (q["stop"] - q["entry"]) if q["stop"] != q["entry"] else None
        poss.append(row)
    return {"entries": ents, "positions": poss}


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run(con, out_dir, now_ms=None, log=print):
    """Rebuild bot_live.json. Returns True when it changed."""
    now_ms = now_ms or int(time.time() * 1000)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='candles_1h'").fetchone():
        return False
    bot, p4 = _read(os.path.join(out_dir, "bot.json")), _read(os.path.join(out_dir, "paper4h.json"))
    pr = _read(os.path.join(out_dir, "prices.json")) or {}
    live_px = {}                                           # prices.json rows: {symbol: [last, chg, high, low, qvol]}
    rows = pr.get("rows") if isinstance(pr.get("rows"), dict) else {}
    for sym, v in rows.items():
        if isinstance(v, list) and v and isinstance(v[0], (int, float)) and v[0] > 0:
            live_px[sym] = float(v[0])
    body = {"daily": daily_bots(con, bot, live_px, now_ms) if bot else {}, "fade4h": fade4h(con, p4, live_px, now_ms)}
    path = os.path.join(out_dir, "bot_live.json")
    old = _read(path)
    if old and {k: v for k, v in old.items() if k != "generated_utc"} == json.loads(json.dumps(body)):
        return False
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dict(body, generated_utc=iso_ms(now_ms)), f, separators=(",", ":"))
    os.replace(tmp, path)
    return True
