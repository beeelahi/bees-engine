"""
Collect public MEXC market data for market-making research (no keys, read-only).

  python3 ~/BinanceData/bin/mexc_probe.py SYMBOL            (e.g. ABCUSDT)
saves ~/BinanceData/research/mexc_SYMBOL.json: exchange rules, 24h ticker, order book (100 levels),
the last 1000 trades, and candles at 1m / 5m / 60m / 1d. The hourly job also appends a small order-book
snapshot to research/mexc_SYMBOL_book.jsonl so we can see how spread and depth change through the day.
The hourly job does this for every coin listed in the private ~/BinanceData/mm.json (none if it is absent).
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "research")
BASE = "https://api.mexc.com"


def configured_symbols():
    """Coins of the market-making config (private file on the server, never in the repository)."""
    try:
        with open(os.path.join(ROOT, "mm.json")) as f:
            return [c["symbol"] for c in json.load(f).get("coins", []) if re.fullmatch(r"[A-Z0-9]{2,20}USDT", c.get("symbol", ""))]
    except Exception:
        return []


def get(path, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(4):                       # retry slow or dropped replies (curl 28 = timeout)
        p = subprocess.run(["curl", "-sS", "--max-time", "20", url], capture_output=True, text=True)
        if p.returncode == 0:
            return json.loads(p.stdout)
        time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"network error (curl {p.returncode}) for {path}")


def full(sym):
    d = {"symbol": sym, "taken_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    d["info"] = get("/api/v3/exchangeInfo", symbol=sym)
    d["ticker"] = get("/api/v3/ticker/24hr", symbol=sym)
    d["book"] = get("/api/v3/depth", symbol=sym, limit=100)
    d["trades"] = get("/api/v3/trades", symbol=sym, limit=1000)
    for iv in ("1m", "5m", "60m", "1d"):
        d["k_" + iv] = get("/api/v3/klines", symbol=sym, interval=iv, limit=1000)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"mexc_{sym}.json")
    with open(path, "w") as f:
        json.dump(d, f)
    t = d["ticker"]
    b = d["book"]
    print(f"{sym}: last {t.get('lastPrice')}, 24h volume {float(t.get('quoteVolume') or 0):,.0f} USDT, "
          f"best bid {b['bids'][0][0] if b.get('bids') else '-'} / ask {b['asks'][0][0] if b.get('asks') else '-'}")
    print("saved", path)


IV_MS = {"5m": 300_000, "60m": 3_600_000, "4h": 14_400_000}


def history(sym, iv="60m", days=400):
    """Keep research/mexc_<sym>_<iv>.json = full candle history (public data, incremental). Returns bars added."""
    path = os.path.join(OUT, f"mexc_{sym}_{iv}.json")
    bars = []
    if os.path.exists(path):
        with open(path) as f:
            bars = json.load(f)
    step = IV_MS[iv]
    now = int(time.time() * 1000)
    start = bars[-1][0] + step if bars else now - days * 86_400_000
    added = 0

    def save():
        os.makedirs(OUT, exist_ok=True)
        tmp = path + ".%d.tmp" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(bars, f)
        os.replace(tmp, path)
    pages = 0
    while start < now - step:
        end = start + 499 * step
        k = get("/api/v3/klines", symbol=sym, interval=iv, startTime=start, endTime=end, limit=500)
        if isinstance(k, list) and k:
            new = [x[:6] for x in k if (not bars or x[0] > bars[-1][0]) and x[0] + step <= now]   # closed bars only
            bars += new
            added += len(new)
        start = end + 1
        pages += 1
        if pages % 20 == 0:                        # keep progress: a rerun continues from here
            save()
        time.sleep(0.15)
    save()
    return added


def snapshot(sym):
    for iv, days in (("60m", 400), ("5m", 370)):          # candle history for strategy research (incremental, non-fatal)
        try:
            history(sym, iv, days)
        except Exception:
            pass
    b = get("/api/v3/depth", symbol=sym, limit=50)
    bids = [(float(p), float(q)) for p, q in b.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in b.get("asks", [])]
    if not bids or not asks:
        return
    mid = (bids[0][0] + asks[0][0]) / 2
    depth = lambda side, pct: sum(p * q for p, q in side if abs(p / mid - 1) <= pct)
    row = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mid": mid,
           "spread_bps": (asks[0][0] / bids[0][0] - 1) * 1e4,
           "bid_usdt_1pct": depth(bids, 0.01), "ask_usdt_1pct": depth(asks, 0.01),
           "bid_usdt_2pct": depth(bids, 0.02), "ask_usdt_2pct": depth(asks, 0.02)}
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"mexc_{sym}_book.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    if len(sys.argv) < 2 and not configured_symbols():
        sys.exit("usage: mexc_probe.py SYMBOL [--snapshot | --history]")
    sym = (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else configured_symbols()[0]).upper()
    if "--snapshot" in sys.argv:
        snapshot(sym)
    elif "--history" in sys.argv:
        for iv, days in (("60m", 400), ("5m", 370)):
            print(f"{sym} {iv}: +{history(sym, iv, days)} candles")
    else:
        full(sym)
