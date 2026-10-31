"""
Bee's Shortlist, automatic picks (Python 3.9 stdlib only).

Once per new daily close, the engine applies the shortlist rules itself and publishes the result; nobody approves
or removes picks by hand.

Rules v3 (2026-10-27), long AND short ideas, published whatever BTC does (the owner's call), each tagged with the
market state so the record can be read honestly. Measured with bin/shortlist_study.py on the full history:
  * longs on the 20 most liquid pairs: since 2022 positive while BTC was above its 200-day average (profit factor
    1.09-1.12, also out of sample) and losing while it was below (0.84-0.92). Tagged "BTC above / below 200d";
  * shorts on all liquid pairs: since 2022 profit factor 1.23 (also out of sample), but they lost before 2022 in the
    bull market (0.87). Shorting needs a futures or margin account.

Long entry: one of the TOP_LIQ most liquid USDT pairs (20-day average volume; stablecoins excluded), daily bull score
  >= 7 (of 10), ATR(14) known, and a PROVEN bullish signal on the daily chart in the last 3 days or on the latest
  closed weekly candle (proven = 100+ cases, a positive 30-day edge vs. the average pair in the full history and in
  both halves, up to 2021 and 2022 on, 30+ cases each). Stop = entry - 2 x ATR(14), fixed.
Short entry: any liquid pair (20-day average volume $2M+), bull score <= 3, a PROVEN bearish signal (mirror rule).
  Stop = entry + 2 x ATR(14), fixed.
Up to MAX_NEW per side per day, best first (more proven signals, more volume-confirmed, stronger bull / bear score).
Not already an active pick on that side; after a close only a signal dated later than the exit can re-open it.
Exit, checked on every daily close: the stop (a daily low below it for longs, a high above it for shorts; exit at the
stop), a STRONG proven signal the other way (30-day edge of 2%+; exit at that close), or delisting.

Every opened or closed pick is appended to data/auto_picks.log, one JSON line each, chained by SHA-256 (each line
carries the hash of the previous one), so the published head hash pins the whole history.
"""
import hashlib
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATE = os.path.join(ROOT, "data", "auto_picks.json")
LEDGER = os.path.join(ROOT, "data", "auto_picks.log")
MIN_SCORE, MAX_SCORE_SHORT, MAX_NEW, DAILY_LOOKBACK, TOP_LIQ, STRONG_EDGE, RULES, MIN_ADV = 7, 3, 25, 2, 20, 2.0, 3, 2e6
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "USD1", "AEUR", "XUSD", "USDE", "BFUSD", "PAXG", "XAUT", "WBTC", "WBETH"}


def _objs(t):
    return [dict(zip(t["columns"], r)) for r in t.get("rows", [])] if t and "columns" in t else []


def _days_before(d, n):
    from datetime import date, timedelta
    return (date.fromisoformat(d) - timedelta(days=n)).isoformat()


def proven(signal_stats):
    bull, bear = set(), set()
    for r in _objs(signal_stats):
        if r.get("variant") != "all" or (r.get("n") or 0) < 100 or r.get("tf") not in ("1d", "1w"):
            continue
        e, e1, e2 = r.get("edge_30d"), r.get("edge_30d_early"), r.get("edge_30d_late")
        if None in (e, e1, e2) or (r.get("n_early") or 0) < 30 or (r.get("n_late") or 0) < 30:
            continue
        if not (e > 0 and e1 > 0 and e2 > 0) and not (e < 0 and e1 < 0 and e2 < 0):
            continue
        k = (r["tf"], r["signal"], r["direction"])
        if r["direction"] == "bullish" and e > 0:
            bull.add(k)
        elif r["direction"] == "bearish" and e < 0:
            bear.add(k)
    return bull, bear


def market_ok(con, last_date):
    """BTC's close on last_date above its 200-day simple average."""
    c = [r[0] for r in con.execute("SELECT close FROM daily WHERE symbol='BTCUSDT' AND date<=? ORDER BY date DESC LIMIT 200", (last_date,))]
    return len(c) == 200 and c[0] > sum(c) / 200


def liquid(con, last_date, top=TOP_LIQ):
    """The `top` most liquid USDT pairs by 20-day average volume up to last_date (stablecoins and wrapped excluded)."""
    frm = _days_before(last_date, 27)
    rows = con.execute("SELECT symbol, AVG(volume_usdt) FROM (SELECT symbol, volume_usdt FROM daily WHERE date>? AND date<=?) "
                       "GROUP BY symbol", (frm, last_date)).fetchall()
    from binance_sync import stock_tokens
    stocks = stock_tokens(con)                          # bStocks do not take crypto liquidity ranks
    rows = [(v or 0, s) for s, v in rows if s.endswith("USDT") and s[:-4] not in STABLE and s[:-4] not in stocks]
    rows.sort(reverse=True)
    return {s for _, s in rows[:top]}


def strong_sets(signal_stats):
    """Proven signals whose 30-day edge is at least STRONG_EDGE: (strong bullish, strong bearish)."""
    bull, bear = proven(signal_stats)
    sb, sr = set(), set()
    for r in _objs(signal_stats):
        k = (r.get("tf"), r.get("signal"), r.get("direction"))
        if r.get("variant") != "all":
            continue
        if k in bull and (r.get("edge_30d") or 0) >= STRONG_EDGE:
            sb.add(k)
        if k in bear and (r.get("edge_30d") or 0) <= -STRONG_EDGE:
            sr.add(k)
    return sb, sr


def strong_bear(signal_stats):
    return strong_sets(signal_stats)[1]


def liquid_all(con, last_date):
    frm = _days_before(last_date, 27)
    from binance_sync import stock_tokens
    stocks = stock_tokens(con)
    return {s for s, v in con.execute("SELECT symbol, AVG(volume_usdt) FROM (SELECT symbol, volume_usdt FROM daily WHERE date>? AND date<=?) "
                                      "GROUP BY symbol", (frm, last_date)) if (v or 0) >= MIN_ADV and s.endswith("USDT") and s[:-4] not in STABLE
            and s[:-4] not in stocks}


def candidates(screener, signals, signal_stats, last_date, active=(), after=None, side="long"):
    bull, bear = proven(signal_stats)
    if side == "short":
        bull = bear                                     # the mirror: proven bearish signals
    sig = [x for x in _objs(signals) if not x.get("provisional")]
    wk = max((x["period_start"] for x in sig if x["tf"] == "1w"), default="")
    frm = _days_before(last_date, DAILY_LOOKBACK)
    by = {}
    for x in sig:
        if (x["tf"], x["signal"], x["direction"]) not in bull or x["period_start"] <= (after or {}).get(x["symbol"], ""):
            continue
        if (x["tf"] == "1d" and frm <= x["period_start"] <= last_date) or (x["tf"] == "1w" and x["period_start"] == wk):
            by.setdefault(x["symbol"], {})[x["tf"] + ":" + x["signal"]] = x
    out = []
    for r in _objs(screener):
        s = r.get("symbol")
        bs = r.get("d_bull_score")
        if s in active or s not in by or bs is None or (side == "long" and bs < MIN_SCORE) or (side == "short" and bs > MAX_SCORE_SHORT):
            continue
        if not r.get("d_atr14_pct") or not r.get("last_close"):
            continue
        sg = by[s]
        out.append({"r": r, "why": sorted(sg), "conf": sum(1 for x in sg.values() if x.get("confirmed"))})
    sgn = 1 if side == "long" else -1
    out.sort(key=lambda c: (-len(c["why"]), -c["conf"], -sgn * (c["r"].get("d_bull_score") or 0),
                            -(c["r"]["d_rs30"] if c["r"].get("d_rs30") is not None else -1e9), c["r"]["symbol"]))
    return out


def _load(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def _append(ledger, head, event):
    line = json.dumps(dict(event, prev=head), separators=(",", ":"), sort_keys=True)
    h = hashlib.sha256(line.encode()).hexdigest()
    with open(ledger, "a") as f:
        f.write(line + "\n")
    return h


def verify(ledger=LEDGER):
    """Recompute the hash chain. Returns (head, events) or raises ValueError."""
    head, n = "", 0
    if not os.path.exists(ledger):
        return head, 0
    with open(ledger) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            if json.loads(line).get("prev") != head:
                raise ValueError(f"auto picks ledger broken at line {n + 1}")
            head, n = hashlib.sha256(line.encode()).hexdigest(), n + 1
    return head, n


def _f(x):
    return float(f"{x:.8g}")


def run(con, export_dir, log=print, state_path=STATE, ledger=LEDGER, now=None):
    """Apply the rules for each new daily close. Returns True when the book changed."""
    from datetime import datetime, timezone
    rd = lambda n: _load(os.path.join(export_dir, n + ".json"), None)   # noqa: E731
    ov, scr, sig = rd("overview"), rd("screener"), rd("signals")
    if not ov or not scr or not sig:
        return False
    last = ov["last_date"]
    st = _load(state_path, {"last_date": None, "rows": [], "head": ""})
    if st.get("last_date") == last:
        return False
    head, _ = verify(ledger)
    if head != st.get("head", ""):
        raise ValueError("auto picks ledger does not match the saved state")
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s_bull, s_bear = strong_sets(ov["signal_stats"])
    rows = {r["symbol"]: r for r in _objs(scr)}
    live_sig = [x for x in _objs(sig) if not x.get("provisional")]
    changed = False
    for p in st["rows"]:
        if p["status"] != "active":
            continue
        s, ex, short = p["symbol"], None, p.get("side") == "short"
        bars = con.execute("SELECT date, low, high FROM daily WHERE symbol=? AND date>? AND date<=? ORDER BY date",
                           (s, p["entry_date"], last)).fetchall()
        hit = next((d for d, lo, hi in bars if ((hi is not None and hi > p["stop"]) if short else (lo is not None and lo < p["stop"]))), None)
        against = s_bull if short else s_bear
        if hit:
            ex = (p["stop"], hit, "Stop hit")
        elif s not in rows:
            c = con.execute("SELECT date, close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (s,)).fetchone()
            ex = (c[1], c[0], "Removed") if c else (p["entry"], last, "Removed")
        elif any(x["symbol"] == s and x["period_start"] > p["entry_date"] and (x["tf"], x["signal"], x["direction"]) in against for x in live_sig):
            ex = (rows[s]["last_close"], last, "Signal exit")
        if ex:
            p.update(status="closed", exit=_f(ex[0]), exit_date=ex[1], closed_at=now, close_reason=ex[2])
            head = _append(ledger, head, {"event": "close", "symbol": s, "side": p.get("side", "long"), "exit": p["exit"],
                                          "exit_date": ex[1], "reason": ex[2], "at": now})
            changed = True
    ok = market_ok(con, last)
    st["market_ok"], st["rules"] = ok, RULES
    opened = 0
    for side, universe in (("long", liquid(con, last)), ("short", liquid_all(con, last))):
        active = {p["symbol"] for p in st["rows"] if p["status"] == "active" and p.get("side", "long") == side}
        after = {}                                 # after a close, only a signal dated later can re-open the pair
        for p in st["rows"]:
            if p["status"] == "closed" and p.get("side", "long") == side:
                after[p["symbol"]] = max(after.get(p["symbol"], ""), p["exit_date"])
        new = [c for c in candidates(scr, sig, ov["signal_stats"], last, active, after, side) if c["r"]["symbol"] in universe][:MAX_NEW]
        sg = 1 if side == "long" else -1
        for c in new:
            r = c["r"]
            px, atr = float(r["last_close"]), float(r["d_atr14_pct"])
            p = {"symbol": r["symbol"], "side": side, "status": "active", "entry": _f(px), "entry_date": last,
                 "stop": _f(px * (1 - sg * 2 * atr / 100)), "risk_pct": round(2 * atr, 2), "approved_at": now, "source": "auto",
                 "why": c["why"][:6], "rules": RULES, "market": "btc_above_200d" if ok else "btc_below_200d"}
            st["rows"].append(p)
            head = _append(ledger, head, {"event": "open", "symbol": p["symbol"], "side": side, "entry": p["entry"], "entry_date": last,
                                          "stop": p["stop"], "why": p["why"], "market": p["market"], "at": now})
            changed = True
            opened += 1
    st.update(last_date=last, head=head)
    tmp = state_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, state_path)
    log(f"auto picks {last}: BTC {'above' if ok else 'below'} its 200-day, {opened} opened, {sum(1 for p in st['rows'] if p['status'] == 'active')} active, "
        f"{sum(1 for p in st['rows'] if p['status'] == 'closed')} closed")
    return changed
