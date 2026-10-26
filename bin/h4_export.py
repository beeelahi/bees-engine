"""
4-hour chart data for the website (Python 3.9 stdlib only).

For the H4_PAIRS most liquid pairs in the universe: the last H4_SHOW_BARS 4h candles with the same chart
indicators as the daily view (EMA 21/50/200, RSI 14, MACD 12/26/9, RVOL 20, Bollinger 20/2, Supertrend 10/3,
Ichimoku cloud), computed on the last H4_CALC_BARS candles so every line is warmed up. A series restarts after
a trading pause of 7+ days, exactly like the daily data. Times are UTC epoch seconds of the candle open.

Output: web_export/h4/index.json (symbol -> shard) and web_export/h4/sNN.json (same columns as pairs/sNN.json).
"""
import json
import os

import indicators as I

H4_PAIRS = 200
H4_SHOW_BARS = 360          # 60 days
H4_CALC_BARS = 1500         # 250 days of history behind every value
SHARDS = 8
BREAK_MS = 7 * 86_400_000
COLS = ["t", "o", "h", "l", "c", "v", "e21", "e50", "e200", "rsi", "macd", "sig", "rv", "bbu", "bbl", "st", "sa", "sb", "x"]
WARM = {"e21": I.ema_warm(21), "e50": I.ema_warm(50), "e200": I.ema_warm(200), "rsi": I.rsi_warm(14),
        "macd": I.macd_warm(12, 26, 9), "st": I.supertrend_warm(10), "cloud": 52 + 26}
BITS = [("e21", 2), ("e50", 4), ("e200", 8), ("rsi", 16), ("macd", 32), ("st", 64), ("cloud", 128)]


def _r(v):
    return None if v is None else float(f"{v:.7g}")


def _series(con, sym, now_ms):
    rows = con.execute("""SELECT open_ms, open, high, low, close, volume_usdt FROM candles_4h WHERE symbol=?
                          ORDER BY open_ms DESC LIMIT ?""", (sym, H4_CALC_BARS)).fetchall()[::-1]
    rows = [x for x in rows if x[0] + 4 * 3600 * 1000 <= now_ms]        # closed candles only
    start = 0
    for i in range(1, len(rows)):
        if rows[i][0] - rows[i - 1][0] >= BREAK_MS:
            start = i
    return rows[start:]


def build(con, universe, now_ms):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='candles_4h'").fetchone():
        return None
    have = {s for (s,) in con.execute("SELECT symbol FROM h4_state WHERE backfilled=1")}
    # liquidity: 20-day average USDT volume from the daily table
    liq = []
    for sym in universe:
        if sym not in have:
            continue
        v = con.execute("SELECT AVG(volume_usdt) FROM (SELECT volume_usdt FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 20)",
                        (sym,)).fetchone()[0] or 0
        liq.append((v, sym))
    liq.sort(reverse=True)
    pick = [s for _, s in liq[:H4_PAIRS]]
    index, shards = {}, [dict() for _ in range(SHARDS)]
    for k, sym in enumerate(pick):
        rows = _series(con, sym, now_ms)
        if len(rows) < 60:
            continue
        o, h, l, c, v = ([x[j] for x in rows] for j in range(1, 6))
        X = {"e21": I.ema(c, 21), "e50": I.ema(c, 50), "e200": I.ema(c, 200), "rsi": I.rsi(c, 14)}
        X["macd"], X["sig"], _ = I.macd(c, 12, 26, 9)
        X["rv"] = I.rvol(v, 20)[1]
        _, X["bbu"], X["bbl"] = I.bbands(c, 20, 2.0)
        X["st"], _ = I.supertrend(h, l, c, 10, 3.0)
        _, _, X["sa"], X["sb"] = I.ichimoku(h, l)
        n = len(rows)
        out = []
        for i in range(max(0, n - H4_SHOW_BARS), n):
            x = 1 + sum(b for key, b in BITS if i >= WARM[key])
            out.append([rows[i][0] // 1000] + [_r(z) for z in (o[i], h[i], l[i], c[i], v[i])] +
                       [_r(X[key][i]) for key in ("e21", "e50", "e200", "rsi", "macd", "sig", "rv", "bbu", "bbl", "st", "sa", "sb")] + [x])
        sh = k % SHARDS
        index[sym] = f"s{sh:02d}.json"
        shards[sh][sym] = out
    return index, shards


def write(con, universe, out_dir, now_ms):
    res = build(con, universe, now_ms)
    d = os.path.join(out_dir, "h4")
    os.makedirs(d, exist_ok=True)
    index, shards = res if res else ({}, [dict() for _ in range(SHARDS)])
    for k, sh in enumerate(shards):
        tmp = os.path.join(d, f".s{k:02d}.tmp")
        with open(tmp, "w") as f:
            json.dump({"columns": COLS, "pairs": sh}, f, separators=(",", ":"), allow_nan=False)
        os.replace(tmp, os.path.join(d, f"s{k:02d}.json"))
    tmp = os.path.join(d, ".index.tmp")
    with open(tmp, "w") as f:
        json.dump(index, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(d, "index.json"))
    return len(index)
