"""
Bee's Shortlist, automatic picks (Python 3.9 stdlib only).

Once per new daily close, the engine applies the shortlist rules itself and publishes the result; nobody approves
or removes picks by hand.

Rules v2 (2026-10-27), chosen with bin/shortlist_study.py on the full history: the v1 rule (no market or liquidity
filter, exit on any proven bearish signal) lost money since 2022 (11,197 trades, -1.3% average, profit factor 0.77).
Adding the market filter and the liquidity limit was the only variant that stayed positive since 2022, also when
'proven' was decided with data up to 2021 only (profit factor 1.09-1.12). The edge is small; most trades lose and a
few win big.

Entry:
  * BTC closed above its 200-day average (no new picks otherwise);
  * the pair is among the TOP_LIQ most liquid USDT pairs (20-day average volume; stablecoins excluded);
  * daily bull score >= 7 (of 10) and ATR(14) known;
  * a PROVEN bullish signal fired on the pair: on the daily chart in the last 3 days (close date, and the 2 before),
    or on the latest closed weekly candle. Proven = 100+ cases, the 30-day edge vs. the average pair is positive
    in the full history AND in both halves (up to 2021, 2022 on), 30+ cases in each half;
  * not already an active pick; after a close only a signal dated later than the exit can re-open the pair. Up to
    MAX_NEW per day, best first (more proven signals, more volume-confirmed, higher bull score, stronger vs BTC).
  Entry = that day's close. Stop = entry - 2 x ATR(14), fixed at entry.
Exit, checked on every new daily close:
  * Stop hit: a daily low after the entry day traded below the stop -> exit at the stop;
  * Signal exit: a STRONG proven bearish signal (30-day edge of 2% or more) fired after the entry day -> exit at
    that day's close;
  * Removed: the pair left the universe -> exit at its last close.

Every opened or closed pick is appended to data/auto_picks.log, one JSON line each, chained by SHA-256 (each line
carries the hash of the previous one), so the published head hash pins the whole history.
"""
import hashlib
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATE = os.path.join(ROOT, "data", "auto_picks.json")
LEDGER = os.path.join(ROOT, "data", "auto_picks.log")
MIN_SCORE, MAX_NEW, DAILY_LOOKBACK, TOP_LIQ, STRONG_EDGE, RULES = 7, 25, 2, 20, 2.0, 2
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
    rows = [(v or 0, s) for s, v in rows if s.endswith("USDT") and s[:-4] not in STABLE]
    rows.sort(reverse=True)
    return {s for _, s in rows[:top]}


def strong_bear(signal_stats):
    _, bear = proven(signal_stats)
    strong = set()
    for r in _objs(signal_stats):
        k = (r.get("tf"), r.get("signal"), r.get("direction"))
        if r.get("variant") == "all" and k in bear and (r.get("edge_30d") or 0) <= -STRONG_EDGE:
            strong.add(k)
    return strong


def candidates(screener, signals, signal_stats, last_date, active=(), after=None):
    bull, _ = proven(signal_stats)
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
        if s in active or s not in by or (r.get("d_bull_score") or 0) < MIN_SCORE:
            continue
        if not r.get("d_atr14_pct") or not r.get("last_close"):
            continue
        sg = by[s]
        out.append({"r": r, "why": sorted(sg), "conf": sum(1 for x in sg.values() if x.get("confirmed"))})
    out.sort(key=lambda c: (-len(c["why"]), -c["conf"], -(c["r"].get("d_bull_score") or 0),
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
    bear = strong_bear(ov["signal_stats"])
    rows = {r["symbol"]: r for r in _objs(scr)}
    sigs = [x for x in _objs(sig) if not x.get("provisional") and (x["tf"], x["signal"], x["direction"]) in bear]
    changed = False
    for p in st["rows"]:
        if p["status"] != "active":
            continue
        s, ex = p["symbol"], None
        lows = con.execute("SELECT date, low FROM daily WHERE symbol=? AND date>? AND date<=? ORDER BY date",
                           (s, p["entry_date"], last)).fetchall()
        hit = next((d for d, lo in lows if lo is not None and lo < p["stop"]), None)
        if hit:
            ex = (p["stop"], hit, "Stop hit")
        elif s not in rows:
            c = con.execute("SELECT date, close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (s,)).fetchone()
            ex = (c[1], c[0], "Removed") if c else (p["entry"], last, "Removed")
        else:
            b = sorted(x["period_start"] for x in sigs if x["symbol"] == s and x["period_start"] > p["entry_date"])
            if b:
                ex = (rows[s]["last_close"], last, "Signal exit")
        if ex:
            p.update(status="closed", exit=_f(ex[0]), exit_date=ex[1], closed_at=now, close_reason=ex[2])
            head = _append(ledger, head, {"event": "close", "symbol": s, "exit": p["exit"], "exit_date": ex[1],
                                          "reason": ex[2], "at": now})
            changed = True
    active = {p["symbol"] for p in st["rows"] if p["status"] == "active"}
    after = {}                                     # after a close, only a signal dated later can re-open the pair
    for p in st["rows"]:
        if p["status"] == "closed":
            after[p["symbol"]] = max(after.get(p["symbol"], ""), p["exit_date"])
    ok, liq = market_ok(con, last), liquid(con, last)
    new = [c for c in candidates(scr, sig, ov["signal_stats"], last, active, after) if c["r"]["symbol"] in liq][:MAX_NEW] if ok else []
    st["market_ok"], st["rules"] = ok, RULES
    for c in new:
        r = c["r"]
        px, atr = float(r["last_close"]), float(r["d_atr14_pct"])
        p = {"symbol": r["symbol"], "status": "active", "entry": _f(px), "entry_date": last, "stop": _f(px * (1 - 2 * atr / 100)),
             "risk_pct": round(2 * atr, 2), "approved_at": now, "source": "auto", "why": c["why"][:6], "rules": RULES}
        st["rows"].append(p)
        head = _append(ledger, head, {"event": "open", "symbol": p["symbol"], "entry": p["entry"], "entry_date": last,
                                      "stop": p["stop"], "why": p["why"], "at": now})
        changed = True
    st.update(last_date=last, head=head)
    tmp = state_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, state_path)
    log(f"auto picks {last}: market {'on' if ok else 'off (BTC below its 200-day)'}, {len(new)} opened, {sum(1 for p in st['rows'] if p['status'] == 'active')} active, "
        f"{sum(1 for p in st['rows'] if p['status'] == 'closed')} closed")
    return changed
