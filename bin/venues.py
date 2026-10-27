"""
Pairs tracked from another exchange alongside Binance (Python 3.9 stdlib only).

EXTERNAL lists USDT pairs that are not on Binance spot but are tracked like any other pair, from the named venue's
public market data: daily / 4h / 1h candles, the 24h ticker and the listing status. Everything downstream
(indicators, signals, channels, patterns, the website) treats them like Binance pairs; the Binance trading bots
never trade them (bot_sim excludes them) and the Binance-vs-native audits skip them.

MEXC differences handled here, so the rest of the engine sees Binance-shaped rows:
  * klines: 500 bars per call at most, and a call with only startTime returns the LATEST bars, so every request is
    sent as an explicit [start, end] window and paged here up to the limit the caller asked for;
  * kline rows have 8 fields (no trade count, no taker-buy volume): those come back as None, never invented;
  * close time = open + interval (Binance: open + interval - 1) and 1h is called "60m", 1w "1W";
  * the 24h ticker's priceChangePercent is a fraction (0.0109 = 1.09%): converted to percent.
If a pair is ever listed on Binance, Binance wins: the external entry is ignored for that symbol.
"""
import json
import time
import urllib.parse

EXTERNAL = {"ZIGUSDT": "mexc"}
MEXC = "https://api.mexc.com"
MEXC_LIMIT = 500
IV_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
MEXC_IV = {"1h": "60m", "4h": "4h", "1d": "1d", "1w": "1W", "1M": "1M"}


def is_external(sym):
    return sym in EXTERNAL


def venue(sym):
    return EXTERNAL.get(sym, "binance")


def default_fetch(url):
    """(status, headers, body) via curl (the URL is read from stdin, never put on a command line)."""
    import subprocess
    p = subprocess.run(["curl", "-sS", "--compressed", "--max-time", "60", "-w", "\n%{http_code}", "--config", "-"],
                       input='url = "%s"\n' % url, capture_output=True, text=True)
    if p.returncode != 0:
        raise ConnectionError(f"curl exit {p.returncode}")
    body, _, code = p.stdout.rpartition("\n")
    return int(code or 0), {}, body


def _get(fetch, path, params, retries=4):
    """fetch(url) -> (status, headers, body). Retries network errors and 429 / 5xx."""
    fetch = fetch or default_fetch
    url = MEXC + path + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            code, _, body = fetch(url)
        except Exception as e:                       # network
            last = e
            time.sleep(min(2 ** attempt, 20))
            continue
        if code == 200:
            return json.loads(body)
        last = RuntimeError(f"MEXC {path} HTTP {code}: {body[:160]}")
        if code == 400:
            raise last
        time.sleep(min(2 ** attempt, 20))
    raise last


def klines(fetch, params, now_ms=None):
    """Binance-compatible /api/v3/klines for an external symbol: up to params['limit'] rows from startTime on
    (oldest first), each [open_ms, o, h, l, c, vol_base, close_ms, vol_usdt, None, None, None, '0']."""
    sym, iv = params["symbol"], params["interval"]
    if iv not in IV_MS and iv != "1M":
        raise ValueError(f"venues: unsupported interval {iv}")
    now_ms = now_ms or int(time.time() * 1000)
    step = IV_MS.get(iv, 31 * 86_400_000)
    want = int(params.get("limit", 500))
    cur = int(params.get("startTime", now_ms - want * step))
    end = int(params.get("endTime", now_ms))
    out, seen = [], set()
    while cur <= end and len(out) < want:
        w_end = min(end, cur + MEXC_LIMIT * step - 1)
        batch = _get(fetch, "/api/v3/klines", {"symbol": sym, "interval": MEXC_IV[iv], "startTime": cur, "endTime": w_end,
                                               "limit": MEXC_LIMIT})
        rows = sorted((r for r in batch if cur <= int(r[0]) <= end and int(r[0]) not in seen), key=lambda r: int(r[0]))
        for r in rows:
            seen.add(int(r[0]))
            ot = int(r[0])
            out.append([ot, str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5]), int(r[6]) - 1 if int(r[6]) > ot else ot + step - 1,
                        str(r[7]), None, None, None, "0"])
            if len(out) >= want:
                break
        cur = (int(rows[-1][0]) + step) if rows else w_end + 1          # empty window (before listing): skip ahead
    return out


def exchange_info(fetch, sym):
    """{'symbol', 'baseAsset', 'status': 'TRADING' | 'BREAK'} or None if the venue doesn't list it."""
    try:
        d = _get(fetch, "/api/v3/exchangeInfo", {"symbol": sym})
    except Exception:
        return None
    s = next((x for x in d.get("symbols", []) if x.get("symbol") == sym), None)
    if not s:
        return None
    ok = str(s.get("status")) in ("1", "ENABLED", "TRADING") and s.get("isSpotTradingAllowed", True)
    return {"symbol": sym, "baseAsset": s["baseAsset"], "quoteAsset": s.get("quoteAsset"), "status": "TRADING" if ok else "BREAK"}


def ticker24(fetch, sym):
    """Binance-shaped 24h ticker (priceChangePercent in percent)."""
    t = _get(fetch, "/api/v3/ticker/24hr", {"symbol": sym})
    if isinstance(t, list):
        t = t[0] if t else {}
    return {"symbol": sym, "lastPrice": t["lastPrice"], "priceChangePercent": str(100.0 * float(t["priceChangePercent"])),
            "highPrice": t["highPrice"], "lowPrice": t["lowPrice"], "quoteVolume": t["quoteVolume"]}
