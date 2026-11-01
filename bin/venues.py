"""
Pairs tracked from another exchange alongside Binance (Python 3.9 stdlib only).

EXTERNAL lists USDT pairs that are not on Binance spot but are tracked like any other pair, from the named venue's
public market data: daily / 4h / 1h candles, the 24h ticker and the listing status. Everything downstream
(indicators, signals, channels, patterns, the website) treats them like Binance pairs; the Binance trading bots
never trade them (bot_sim excludes them) and the Binance-vs-native audits skip them.

Two kinds:
  * PINNED: tracked on purpose whatever its volume (ZIG on MEXC); it skips the liquidity filters everywhere.
  * the coin library's pairs (data/external_pairs.json, written by coin_library.py): CMC top-1,000 coins that are not
    on Binance, from whichever of KuCoin / Gate / MEXC trades the most of them. They follow the same rules as any
    Binance pair (the $100k volume floor, the top-N liquidity cuts).
If a pair is ever listed on Binance, Binance wins: the external entry is ignored for that symbol.

Venue differences handled here, so the rest of the engine sees Binance-shaped rows
[open_ms, o, h, l, c, vol_base, close_ms, vol_usdt, None, None, None, '0'] (no trade count, no taker volume: None,
never invented):
  * MEXC: 500 bars per call; a call with only startTime returns the LATEST bars, so every request is an explicit
    [start, end] window; close time = open + interval; 1h is "60m", 1w "1W"; ticker change is a fraction.
  * Gate: /api/v4/spot/candlesticks, at most 1,000 points per call, seconds; rows [t, quote_vol, close, high, low,
    open, base_vol, closed]; windows Gate refuses as too old (HTTP 400) are skipped, never fatal.
  * KuCoin: /api/v1/market/candles, at most 1,500 rows per call, newest first, seconds; rows [t, open, close, high,
    low, base_vol, quote_vol]; ticker change is a fraction.
"""
import json
import os
import time
import urllib.parse

PINNED = {"ZIGUSDT": "mexc"}
EXT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "external_pairs.json")
BASE = {"mexc": "https://api.mexc.com", "gate": "https://api.gateio.ws", "kucoin": "https://api.kucoin.com"}
NAME = {"mexc": "MEXC", "gate": "Gate", "kucoin": "KuCoin"}
TV_PREFIX = {"mexc": "MEXC", "gate": "GATEIO", "kucoin": "KUCOIN"}
MEXC = BASE["mexc"]
MEXC_LIMIT = 500
GATE_LIMIT = 999
KUCOIN_LIMIT = 1500
IV_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
MEXC_IV = {"1h": "60m", "4h": "4h", "1d": "1d", "1w": "1W", "1M": "1M"}
GATE_IV = {"1h": "1h", "4h": "4h", "1d": "1d"}
KUCOIN_IV = {"1h": "1hour", "4h": "4hour", "1d": "1day", "1w": "1week"}


SWITCH = {}                    # symbol -> last Binance day, for a coin Binance stopped that continues from another exchange


def _load(path=EXT_FILE):
    ext, native = dict(PINNED), {s: s for s in PINNED}
    SWITCH.clear()
    try:
        with open(path) as f:
            d = json.load(f)
        for sym, v in d.items():
            if sym not in PINNED and v.get("venue") in BASE and v.get("native") and sym.isascii() and sym.isalnum():
                ext[sym], native[sym] = v["venue"], v["native"]
                if v.get("continues_binance_after"):
                    SWITCH[sym] = v["continues_binance_after"]
    except (OSError, ValueError, AttributeError):
        pass
    return ext, native


EXTERNAL, NATIVE = _load()


def is_external(sym):
    return sym in EXTERNAL


def is_pinned(sym):
    return sym in PINNED


def venue(sym):
    return EXTERNAL.get(sym, "binance")


def venue_name(sym):
    return NAME.get(EXTERNAL.get(sym), "Binance")


def native(sym):
    return NATIVE.get(sym, sym)


def default_fetch(url):
    """(status, headers, body) via curl (the URL is read from stdin, never put on a command line)."""
    import subprocess
    p = subprocess.run(["curl", "-sS", "--compressed", "--max-time", "60", "-w", "\n%{http_code}", "--config", "-"],
                       input='url = "%s"\n' % url, capture_output=True, text=True)
    if p.returncode != 0:
        raise ConnectionError(f"curl exit {p.returncode}")
    body, _, code = p.stdout.rpartition("\n")
    return int(code or 0), {}, body


class Refused(RuntimeError):
    """The venue answered 400: a bad request (for Gate, often a window too far back). Never retried."""


def _get(fetch, path, params, retries=4, base=MEXC):
    """fetch(url) -> (status, headers, body). Retries network errors and 429 / 5xx."""
    fetch = fetch or default_fetch
    url = base + path + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            code, _, body = fetch(url)
        except Exception as e:                       # network
            last = e
            time.sleep(min(2 ** attempt, 20))
            continue
        if code == 200:
            d = json.loads(body)
            if isinstance(d, dict) and base == BASE["kucoin"]:
                if d.get("code") not in ("200000", 200000):
                    raise Refused(f"KuCoin {path}: {str(d)[:160]}")
                return d.get("data")
            return d
        last = RuntimeError(f"{base.split('//')[1]} {path} HTTP {code}: {body[:160]}")
        if code == 400:
            raise Refused(str(last))
        if attempt < retries - 1:
            time.sleep(min(2 ** (attempt + 1), 30))
    raise last


def _row(ot, o, h, l, c, vb, vq, step):
    return [ot, str(o), str(h), str(l), str(c), str(vb), ot + step - 1, str(vq), None, None, None, "0"]


def klines(fetch, params, now_ms=None):
    """Binance-compatible /api/v3/klines for an external symbol: up to params['limit'] rows from startTime on
    (oldest first)."""
    sym, iv = params["symbol"], params["interval"]
    v = venue(sym)
    if v == "gate" and iv not in GATE_IV or v == "kucoin" and iv not in KUCOIN_IV or v == "mexc" and iv not in MEXC_IV:
        raise ValueError(f"venues: interval {iv} not supported for {sym} on {v}")
    now_ms = now_ms or int(time.time() * 1000)
    step = IV_MS.get(iv, 31 * 86_400_000)
    want = int(params.get("limit", 500))
    cur = int(params.get("startTime", now_ms - want * step))
    end = int(params.get("endTime", now_ms))
    lim = {"mexc": MEXC_LIMIT, "gate": GATE_LIMIT, "kucoin": KUCOIN_LIMIT}[v]
    out, seen = [], set()
    while cur <= end and len(out) < want:
        w_end = min(end, cur + lim * step - 1)
        try:
            if v == "mexc":
                batch = _get(fetch, "/api/v3/klines", {"symbol": native(sym), "interval": MEXC_IV[iv], "startTime": cur, "endTime": w_end,
                                                       "limit": MEXC_LIMIT})
                rows = [_row(int(r[0]), r[1], r[2], r[3], r[4], r[5], r[7], step) for r in batch]
            elif v == "gate":
                batch = _get(fetch, "/api/v4/spot/candlesticks", {"currency_pair": native(sym), "interval": GATE_IV[iv],
                                                                  "from": cur // 1000, "to": w_end // 1000}, base=BASE["gate"])
                rows = [_row(int(r[0]) * 1000, r[5], r[3], r[4], r[2], r[6], r[1], step) for r in batch]
            else:
                batch = _get(fetch, "/api/v1/market/candles", {"type": KUCOIN_IV[iv], "symbol": native(sym),
                                                               "startAt": cur // 1000, "endAt": w_end // 1000 + 1}, base=BASE["kucoin"]) or []
                rows = [_row(int(r[0]) * 1000, r[1], r[3], r[4], r[2], r[5], r[6], step) for r in batch]
                time.sleep(0.15)                     # KuCoin's public limit is the tightest of the three
        except Refused:
            if v != "gate":
                raise
            cur = _gate_first_ok(fetch, sym, iv, cur, w_end, step)      # the window starts before what Gate serves
            if cur is None:
                cur = w_end + 1
            continue
        rows = sorted((r for r in rows if cur <= r[0] <= end and r[0] not in seen), key=lambda r: r[0])
        for r in rows:
            seen.add(r[0])
            out.append(r)
            if len(out) >= want:
                break
        cur = (rows[-1][0] + step) if rows else w_end + 1          # empty window (before listing): skip ahead
    return out


def _gate_first_ok(fetch, sym, iv, lo, hi, step):
    """Earliest start in [lo, hi] that Gate accepts (it refuses starts too far back), by bisection; None if none."""
    def ok(t):
        try:
            _get(fetch, "/api/v4/spot/candlesticks", {"currency_pair": native(sym), "interval": GATE_IV[iv], "from": t // 1000,
                                                      "to": min(hi, t + step) // 1000}, base=BASE["gate"])
            return True
        except Refused:
            return False
    if not ok(hi):
        return None
    while hi - lo > step:
        mid = lo + (hi - lo) // 2 // step * step
        if mid <= lo:
            break
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _tick(last, chg_pct, high, low, qvol):
    return {"lastPrice": str(last), "priceChangePercent": str(chg_pct), "highPrice": str(high), "lowPrice": str(low), "quoteVolume": str(qvol)}


def bulk(fetch=None, syms=None):
    """Status and 24h ticker of every external pair, two calls per venue.
    Returns ({sym: {'status': 'TRADING'|'BREAK', 'baseAsset', 'ticker': Binance-shaped or None}}, failed_venues).
    A pair missing from its venue's list is simply absent (no longer listed); a venue that could not be read is in
    failed_venues, so callers keep its pairs as they were instead of marking them delisted."""
    syms = list(EXTERNAL) if syms is None else syms
    by = {}
    for s in syms:
        by.setdefault(venue(s), []).append(s)
    out, failed = {}, set()
    for v, ss in by.items():
        try:
            if v == "mexc":
                info = {x["symbol"]: x for x in _get(fetch, "/api/v3/exchangeInfo", {})["symbols"]}
                tl = _get(fetch, "/api/v3/ticker/24hr", {})
                tk = {t.get("symbol"): t for t in (tl if isinstance(tl, list) else [tl])}
                for s in ss:
                    x = info.get(native(s))
                    if not x:
                        continue
                    ok = str(x.get("status")) in ("1", "ENABLED", "TRADING") and x.get("isSpotTradingAllowed", True)
                    t = tk.get(native(s))
                    out[s] = {"status": "TRADING" if ok else "BREAK", "baseAsset": x["baseAsset"],
                              "ticker": _tick(t["lastPrice"], 100.0 * float(t["priceChangePercent"]), t["highPrice"], t["lowPrice"], t["quoteVolume"]) if t else None}
            elif v == "gate":
                info = {x["id"]: x for x in _get(fetch, "/api/v4/spot/currency_pairs", {}, base=BASE["gate"])}
                tk = {t["currency_pair"]: t for t in _get(fetch, "/api/v4/spot/tickers", {}, base=BASE["gate"])}
                for s in ss:
                    x = info.get(native(s))
                    if not x:
                        continue
                    t = tk.get(native(s))
                    out[s] = {"status": "TRADING" if x.get("trade_status") == "tradable" else "BREAK", "baseAsset": x["base"],
                              "ticker": _tick(t["last"], float(t.get("change_percentage") or 0), t.get("high_24h") or t["last"], t.get("low_24h") or t["last"],
                                              t.get("quote_volume") or 0) if t else None}
            else:
                info = {x["symbol"]: x for x in _get(fetch, "/api/v2/symbols", {}, base=BASE["kucoin"])}
                tk = {t["symbol"]: t for t in _get(fetch, "/api/v1/market/allTickers", {}, base=BASE["kucoin"])["ticker"]}
                for s in ss:
                    x = info.get(native(s))
                    if not x:
                        continue
                    t = tk.get(native(s))
                    out[s] = {"status": "TRADING" if x.get("enableTrading") else "BREAK", "baseAsset": x["baseCurrency"],
                              "ticker": _tick(t["last"], 100.0 * float(t.get("changeRate") or 0), t.get("high") or t["last"], t.get("low") or t["last"],
                                              t.get("volValue") or 0) if t and t.get("last") else None}
        except Exception:
            failed.add(v)
    return out, failed


def exchange_info(fetch, sym):
    """{'symbol', 'baseAsset', 'status': 'TRADING' | 'BREAK'} or None if the venue doesn't list it (or can't be read)."""
    d, _ = bulk(fetch, [sym])
    x = d.get(sym)
    return {"symbol": sym, "baseAsset": x["baseAsset"], "quoteAsset": "USDT", "status": x["status"]} if x else None


def ticker24(fetch, sym):
    """Binance-shaped 24h ticker (priceChangePercent in percent)."""
    if venue(sym) == "mexc":
        t = _get(fetch, "/api/v3/ticker/24hr", {"symbol": native(sym)})
        if isinstance(t, list):
            t = t[0] if t else {}
        return {"symbol": sym, **_tick(t["lastPrice"], 100.0 * float(t["priceChangePercent"]), t["highPrice"], t["lowPrice"], t["quoteVolume"])}
    d, failed = bulk(fetch, [sym])
    if sym not in d or not d[sym]["ticker"]:
        raise RuntimeError(f"{sym}: no 24h ticker from {venue(sym)}")
    return {"symbol": sym, **d[sym]["ticker"]}
