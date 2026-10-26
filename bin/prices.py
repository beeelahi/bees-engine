"""
Live prices for the website, refreshed on every 15-minute run (Python 3.9 stdlib only).

One call to Binance's public 24h ticker (all symbols, request weight 80) gives the latest price, the rolling
24-hour change, high, low and USDT volume of every pair. Only pairs in the dashboard's universe are kept.

Output: web_export/prices.json  {"as_of": "...Z", "columns": [...], "rows": {"BTCUSDT": [last, chg_pct, high, low, qvol]}}
The daily candles, indicators and signals still come from closed daily candles; these are context only.
"""
import json
import os
from datetime import datetime, timezone

COLUMNS = ["last", "chg_24h_pct", "high_24h", "low_24h", "qvol_24h"]


def _r(x):
    return float(f"{float(x):.8g}")


def build(con, api):
    uni = {s for (s,) in con.execute("SELECT symbol FROM latest WHERE in_universe=1")}
    rows = {}
    for t in api("/api/v3/ticker/24hr"):
        s = t.get("symbol")
        if s not in uni:
            continue
        try:
            last = float(t["lastPrice"])
            if last <= 0:
                continue
            rows[s] = [_r(last), round(float(t["priceChangePercent"]), 2), _r(t["highPrice"]), _r(t["lowPrice"]),
                       round(float(t["quoteVolume"]))]
        except (KeyError, TypeError, ValueError):
            continue
    return {"as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "columns": COLUMNS, "rows": rows}


def run(con, api, out_dir):
    """Write prices.json; returns the number of pairs, or 0 when nothing was written."""
    if not os.path.isdir(out_dir):
        return 0
    data = build(con, api)
    if len(data["rows"]) < 10:                 # partial or failed reply: keep the previous file
        return 0
    tmp = os.path.join(out_dir, ".prices.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(out_dir, "prices.json"))
    return len(data["rows"])
