"""
Fade 4h: an EXPERIMENTAL paper-trading bot on 4-hour candles (Python 3.9 stdlib only). Paper only: it never places
an order anywhere.

Why this rule: on 4-hour candles the one effect that held in both halves of the history (research/bot4h.py,
Oct 2023 on) is that big pumps fade. 4h breakout longs lost money in both halves and were rejected.

Rules (checked on every 15-minute run, on 4h candles that have closed):
  * universe: coins with a USDT perpetual (they can be shorted) whose 30-candle average volume is $2M+ a day;
  * signal: a 4h candle closes 15%+ above the previous close;
  * entry: SHORT at the next candle's open; stop = that candle's high + 0.5 x ATR(14, 4h);
  * exit: at the stop (or the open, if it gapped through), else after 18 candles (3 days) at the close;
  * size: risk 1% of paper equity per trade (equity starts at 10,000 USDT), at most MAX_OPEN open, one per coin;
  * costs: 0.1% a side + 0.05% slippage (funding not included).
Every open and close is appended to data/paper4h.log, chained by SHA-256 (each line holds the previous hash), so the
record cannot be edited quietly. The bot starts trading from the moment it is installed; it never back-fills.
"""
import hashlib
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATE = os.path.join(ROOT, "data", "paper4h.json")
LEDGER = os.path.join(ROOT, "data", "paper4h.log")
H4 = 4 * 3600 * 1000
PUMP, STOP_ATR, HOLD, RISK, MAX_OPEN, START_EQ, COST, MIN_ADV = 0.15, 0.5, 18, 0.01, 5, 10_000.0, 0.0015, 2e6


def _append(ledger, head, event):
    line = json.dumps(dict(event, prev=head), separators=(",", ":"), sort_keys=True)
    with open(ledger, "a") as f:
        f.write(line + "\n")
    return hashlib.sha256(line.encode()).hexdigest()


def verify(ledger=LEDGER):
    head, n = "", 0
    if os.path.exists(ledger):
        with open(ledger) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                if json.loads(line).get("prev") != head:
                    raise ValueError(f"paper4h ledger broken at line {n + 1}")
                head, n = hashlib.sha256(line.encode()).hexdigest(), n + 1
    return head, n


def _atr(X, n=14):
    a = None
    for k in range(1, len(X)):
        tr = max(X[k][2], X[k - 1][4]) - min(X[k][3], X[k - 1][4])
        a = tr if a is None else a + (tr - a) / n
    return a


def run(con, out_dir, now_ms, state_path=STATE, ledger=LEDGER, log=print):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='candles_4h'").fetchone():
        return 0
    st = {"start_ms": now_ms, "equity": START_EQ, "open": [], "closed": [], "last_ms": {}, "pending": [], "head": ""}
    if os.path.exists(state_path):
        with open(state_path) as f:
            st = json.load(f)
    for t in st["closed"]:                      # % for a short is the gain on the entry (closed before 31 Oct 2026: entry / exit - 1)
        if t.get("exit"):
            t["pnl_pct"] = 100 * (1 - t["exit"] / t["entry"])
    head, _ = verify(ledger)
    if head != st.get("head", ""):
        raise ValueError("paper4h ledger does not match the saved state")
    shortable = set()
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='deriv_state'").fetchone():
        shortable = {s for (s,) in con.execute("SELECT symbol FROM deriv_state WHERE listed=1")}
    syms = sorted({s for (s,) in con.execute("SELECT symbol FROM h4_state WHERE backfilled=1")} & shortable
                  | {p["symbol"] for p in st["open"]} | {p["symbol"] for p in st["pending"]})
    changed = 0
    for s in syms:
        X = con.execute("SELECT open_ms, open, high, low, close, volume_usdt FROM candles_4h WHERE symbol=? AND close_ms < ? "
                        "ORDER BY open_ms DESC LIMIT 60", (s, now_ms)).fetchall()[::-1]
        if len(X) < 35:
            continue
        last = st["last_ms"].get(s, max(st["start_ms"] - H4, X[-2][0]))
        for k in range(31, len(X)):
            t = X[k][0]
            if t <= last:
                continue
            # 1) a pending entry fills at this candle's open
            for p in [p for p in st["pending"] if p["symbol"] == s]:
                st["pending"].remove(p)
                if len(st["open"]) >= MAX_OPEN or any(q["symbol"] == s for q in st["open"]):
                    continue
                ent = X[k][1] * (1 - COST)
                if p["stop"] <= ent:
                    continue                                   # opened above the stop: skipped
                qty = RISK * st["equity"] / (p["stop"] - ent)
                pos = {"symbol": s, "side": "short", "entry": ent, "entry_ms": t, "stop": p["stop"], "qty": qty,
                       "signal_ms": p["signal_ms"], "pump_pct": p["pump_pct"], "bars": 0}
                st["open"].append(pos)
                head = _append(ledger, head, {"event": "open", **pos})
                changed += 1
            # 2) manage an open position on this candle
            for pos in [q for q in st["open"] if q["symbol"] == s and q["entry_ms"] <= t]:
                pos["bars"] += 1
                out, why = None, None
                if X[k][2] >= pos["stop"]:
                    out, why = max(X[k][1], pos["stop"]) * (1 + COST), "stop"
                elif pos["bars"] >= HOLD:
                    out, why = X[k][4] * (1 + COST), "time"
                if out:
                    pnl = (pos["entry"] - out) * pos["qty"]
                    st["equity"] += pnl
                    st["open"].remove(pos)
                    done = dict(pos, exit=out, exit_ms=t, reason=why, pnl=pnl,
                                r=(pos["entry"] - out) / (pos["stop"] - pos["entry"]), pnl_pct=100 * (1 - out / pos["entry"]))   # a short: gain as a share of the entry
                    st["closed"].append(done)
                    head = _append(ledger, head, {"event": "close", "symbol": s, "exit": out, "exit_ms": t, "reason": why,
                                                  "pnl": pnl, "equity": st["equity"]})
                    changed += 1
            # 3) a new signal on this closed candle
            prev, c = X[k - 1][4], X[k][4]
            adv = sum(x[5] or 0 for x in X[k - 30:k]) / 30 * 6
            if prev > 0 and c / prev - 1 >= PUMP and adv >= MIN_ADV and not any(q["symbol"] == s for q in st["open"]):
                a = _atr(X[max(0, k - 30):k + 1]) or 0
                st["pending"].append({"symbol": s, "signal_ms": t, "stop": X[k][2] + STOP_ATR * a, "pump_pct": 100 * (c / prev - 1)})
                changed += 1
            st["last_ms"][s] = t
    st["head"] = head
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, state_path)
    export(con, st, out_dir)
    if changed:
        log(f"paper 4h bot: {len(st['open'])} open, {len(st['closed'])} closed, equity {st['equity']:,.2f}")
    return changed


def export(con, st, out_dir, stats_path=None):
    if not os.path.isdir(out_dir):
        return
    p = stats_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper4h_stats.json")
    stats = {}
    if os.path.exists(p):
        with open(p) as f:
            stats = json.load(f)
    opn = []
    for q in st["open"]:
        r = con.execute("SELECT close FROM candles_4h WHERE symbol=? ORDER BY open_ms DESC LIMIT 1", (q["symbol"],)).fetchone()
        px = r[0] if r else None
        opn.append(dict(q, price=px, unreal=(q["entry"] - px) * q["qty"] if px else None))
    data = {"name": "Fade 4h (experimental)", "start_ms": st["start_ms"], "equity": st["equity"], "start_equity": START_EQ,
            "open": opn, "pending": st["pending"], "closed": st["closed"][-200:], "n_closed": len(st["closed"]),
            "ledger_head": st["head"], "stats": stats,
            "rules": {"pump_pct": 100 * PUMP, "stop_atr": STOP_ATR, "hold_candles": HOLD, "risk_pct": 100 * RISK,
                      "max_open": MAX_OPEN, "cost_pct": 100 * COST, "min_adv": MIN_ADV}}
    dst = os.path.join(out_dir, "paper4h.json")
    text = json.dumps(data, separators=(",", ":"))
    if os.path.exists(dst):
        with open(dst) as f:
            if f.read() == text:
                return
    with open(dst + ".tmp", "w") as f:
        f.write(text)
    os.replace(dst + ".tmp", dst)
