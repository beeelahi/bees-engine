"""
Big 4-hour moves (Python 3.9 stdlib only): checked on every 15-minute run, as soon as a 4h candle closes.

Liquid coins only (average 4h volume over the previous 30 candles >= $2M a day; stablecoins excluded). One event per
coin per candle, the strongest that applies:
  pump_15 / dump_15   the candle closed 15%+ above / below the previous close
  pump_8 / dump_8     8%+ on 3x the average volume of the previous 30 candles
  high_30d / low_30d  closed above the highest high / below the lowest low of the previous 30 days, on 2x volume
  market              BTC or ETH moved 3%+ in one 4h candle
The measured record of each kind (research/moves_4h.py -> bin/move_stats.json) is published with the events:
since late 2023, big 4h pumps on heavy volume mostly FADED over the next 1-3 days. These are moves to know about,
not entries.

Output: web_export/moves.json (the last 7 days of events, with the move since). Returns the new events.
"""
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
H4_MS = 4 * 3600 * 1000
LOOK, KEEP_DAYS, MIN_ADV, TOP = 180, 7, 2e6, 200
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "USD1", "AEUR", "XUSD", "USDE"}
ORDER = ("pump_15", "dump_15", "pump_8", "dump_8", "high_30d", "low_30d", "market")
LABEL = {"pump_15": "Pumped 15%+ in 4h", "dump_15": "Dumped 15%+ in 4h", "pump_8": "Up 8%+ in 4h on 3× volume",
         "dump_8": "Down 8%+ in 4h on 3× volume", "high_30d": "New 30-day high on 2× volume",
         "low_30d": "New 30-day low on 2× volume", "market": "Big market move (BTC / ETH 3%+ in 4h)"}


def classify(X, k, sym=""):
    """X: list of (open_ms, o, h, l, c, v) sorted; k: index of a closed candle with LOOK candles before it."""
    c, v, prev = X[k][4], X[k][5], X[k - 1][4]
    if prev <= 0:
        return None
    ret = c / prev - 1
    vavg = sum(x[5] for x in X[k - 30:k]) / 30
    rv = v / vavg if vavg else 0
    if ret >= 0.15:
        return "pump_15"
    if ret <= -0.15:
        return "dump_15"
    if ret >= 0.08 and rv >= 3:
        return "pump_8"
    if ret <= -0.08 and rv >= 3:
        return "dump_8"
    if sym in ("BTCUSDT", "ETHUSDT") and abs(ret) >= 0.03:
        return "market"
    if rv >= 2 and c > max(x[2] for x in X[k - LOOK:k]):
        return "high_30d"
    if rv >= 2 and c < min(x[3] for x in X[k - LOOK:k]):
        return "low_30d"
    return None


def run(con, out_dir, now_ms, stats_path=None):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='candles_4h'").fetchone() or not os.path.isdir(out_dir):
        return []
    keep_from = now_ms - KEEP_DAYS * 86_400_000
    from binance_sync import stock_symbols            # bStocks are not crypto: no move cards for them
    stocks = stock_symbols(con)
    syms = [s for (s,) in con.execute("SELECT symbol FROM h4_state WHERE backfilled=1") if s not in stocks]
    liq = []
    for s in syms:
        if s.endswith("USDT") and s[:-4] in STABLE:
            continue
        X = con.execute("SELECT open_ms, open, high, low, close, volume_usdt FROM candles_4h WHERE symbol=? AND close_ms < ? "
                        "ORDER BY open_ms DESC LIMIT ?", (s, now_ms, LOOK + KEEP_DAYS * 6 + 2)).fetchall()[::-1]
        if len(X) < LOOK + 2:
            continue
        adv = sum(x[5] or 0 for x in X[-31:-1]) / 30 * 6
        if adv >= MIN_ADV:
            liq.append((adv, s, X))
    liq.sort(reverse=True)
    rows = []
    for rank, (adv, s, X) in enumerate(liq[:TOP], 1):
        X = [(t, o, h, lo, c, v or 0.0) for t, o, h, lo, c, v in X]
        for k in range(LOOK + 1, len(X)):
            if X[k][0] < keep_from:
                continue
            vavg = sum(x[5] for x in X[k - 30:k]) / 30
            if vavg * 6 < MIN_ADV:
                continue
            kind = classify(X, k, s)
            if kind:
                rows.append({"symbol": s, "t": X[k][0] // 1000, "kind": kind, "close": X[k][4],
                             "ret_pct": round(100 * (X[k][4] / X[k - 1][4] - 1), 1),
                             "rvol": round(X[k][5] / vavg, 1) if vavg else None,
                             "since_pct": round(100 * (X[-1][4] / X[k][4] - 1), 1), "liq_rank": rank})
    rows.sort(key=lambda r: (-r["t"], ORDER.index(r["kind"]), r["liq_rank"]))
    stats = {}
    p = stats_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "move_stats.json")
    if os.path.exists(p):
        with open(p) as f:
            stats = json.load(f)
    data = {"rows": rows, "labels": LABEL, "stats": stats,
            "rules": {"keep_days": KEEP_DAYS, "min_adv": MIN_ADV, "top": TOP, "lookback_days": LOOK // 6}}
    dst = os.path.join(out_dir, "moves.json")
    old = {}
    if os.path.exists(dst):
        with open(dst) as f:
            try:
                old = json.load(f)
            except ValueError:
                old = {}
    seen = {(r["symbol"], r["t"], r["kind"]) for r in old.get("rows", [])}
    new = [r for r in rows if (r["symbol"], r["t"], r["kind"]) not in seen]
    data["as_of"] = max([r["t"] for r in rows], default=None)
    text = json.dumps(data, separators=(",", ":"))
    if json.dumps(old, separators=(",", ":")) != text:
        tmp = dst + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, dst)
    return new
