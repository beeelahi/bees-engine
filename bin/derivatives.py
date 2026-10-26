"""
Binance USDT-M futures market data (public, no keys): funding rates, open interest, long/short ratios.
Python 3.9 stdlib only. Runs after the hourly sync; never blocks it.

  funding        full history (every 8h settlement since the perpetual listed), first download spread over runs
  oi_daily       open interest (contracts and USDT) per day: Binance keeps only the last 30 days of history,
                 so we store it from now on (the database becomes the long history)
  ls_daily       global long/short account ratio per day (same 30-day limit)
Scope: perpetuals whose spot pair is among the DERIV_TOP most liquid pairs of the universe, plus BTC and ETH.
"""
import time
from datetime import datetime, timezone

FAPI = "https://fapi.binance.com"
DERIV_TOP = 150
BUDGET_S = 600
FUNDING_START = 1567296000000          # 2019-09-01, before the first USDT-M perpetual: page forward from here
MAX_CONSEC_ERRORS = 8
SCHEMA = """
CREATE TABLE IF NOT EXISTS funding(symbol TEXT, time_ms INTEGER, rate REAL, mark REAL, PRIMARY KEY(symbol, time_ms)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS oi_daily(symbol TEXT, date TEXT, oi REAL, oi_usdt REAL, PRIMARY KEY(symbol, date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ls_daily(symbol TEXT, date TEXT, ratio REAL, long_pct REAL, PRIMARY KEY(symbol, date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS deriv_state(symbol TEXT PRIMARY KEY, listed INTEGER, funding_last_ms INTEGER,
    funding_done INTEGER DEFAULT 0, oi_date TEXT, updated_utc TEXT);
"""


def _get(fetch, path, params):
    """fetch(url_with_query) -> parsed json; Binance futures public endpoints."""
    import urllib.parse
    return fetch(FAPI + path + "?" + urllib.parse.urlencode(params))


def scope(con):
    rows = con.execute("""SELECT symbol FROM v_universe""").fetchall()
    uni = [s for (s,) in rows]
    liq = []
    for s in uni:
        v = con.execute("SELECT AVG(volume_usdt) FROM (SELECT volume_usdt FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 20)", (s,)).fetchone()[0] or 0
        liq.append((v, s))
    liq.sort(reverse=True)
    pick = [s for _, s in liq[:DERIV_TOP]]
    for s in ("BTCUSDT", "ETHUSDT"):
        if s not in pick:
            pick.insert(0, s)
    return pick


def run(con, fetch, budget_s=BUDGET_S, now_ms=None):
    t0 = time.time()
    con.executescript(SCHEMA)
    now_ms = now_ms or int(time.time() * 1000)
    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    # which perpetuals exist (one call)
    info = _get(fetch, "/fapi/v1/exchangeInfo", {})
    perps = {x["symbol"] for x in info.get("symbols", []) if x.get("contractType") == "PERPETUAL" and x.get("quoteAsset") == "USDT"
             and x.get("status") == "TRADING"}
    want = [s for s in scope(con) if s in perps]
    st = {r[0]: r[1:] for r in con.execute("SELECT symbol, listed, funding_last_ms, funding_done, oi_date FROM deriv_state")}
    stats = {"perps": len(want), "funding_rows": 0, "oi_rows": 0, "ls_rows": 0, "pending": 0, "errors": 0}
    consec = 0
    for sym in want:
        if time.time() - t0 > budget_s:
            stats["pending"] += 1
            continue
        s = st.get(sym, (None, None, 0, None))
        if s[2] == 2 and s[1] and s[1] + 8 * 3600 * 1000 > now_ms and s[3] == today:
            continue                            # nothing new yet: next funding settles within 8 hours
        try:
            # funding: done=2 means the full history is stored, so only newer settlements are fetched. Otherwise page
            # forward from FUNDING_START (Binance returns the oldest records after startTime); rows already stored are
            # skipped by jumping to the newest stored settlement once a page reaches them.
            lo, hi = con.execute("SELECT MIN(time_ms), MAX(time_ms) FROM funding WHERE symbol=?", (sym,)).fetchone()
            full = s[2] == 2
            start = (hi + 1) if (full and hi) else FUNDING_START
            done = 2 if full else 0
            while True:
                if time.time() - t0 > budget_s:
                    break
                batch = _get(fetch, "/fapi/v1/fundingRate", {"symbol": sym, "startTime": start, "limit": 1000})
                if not batch:
                    done = 2
                    break
                before = con.total_changes
                con.executemany("INSERT OR IGNORE INTO funding VALUES(?,?,?,?)",
                                [(sym, int(b["fundingTime"]), float(b["fundingRate"]), float(b["markPrice"] or 0) if b.get("markPrice") else None) for b in batch])
                stats["funding_rows"] += con.total_changes - before
                start = int(batch[-1]["fundingTime"]) + 1
                if not full and lo and hi and start >= lo:
                    start = max(start, hi + 1)         # reached what we already have: continue after it
                if len(batch) < 1000:
                    done = 2
                    break
                time.sleep(0.7)                  # fundingRate: 500 requests / 5 min / IP
            last_f = con.execute("SELECT MAX(time_ms) FROM funding WHERE symbol=?", (sym,)).fetchone()[0]
            # open interest and long/short: once a day (30 days of history on the first call)
            oi_date = s[3]
            if oi_date != today:
                oi = _get(fetch, "/futures/data/openInterestHist", {"symbol": sym, "period": "1d", "limit": 30})
                rows = [(sym, datetime.fromtimestamp(int(x["timestamp"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                         float(x["sumOpenInterest"]), float(x["sumOpenInterestValue"])) for x in oi]
                con.executemany("INSERT OR REPLACE INTO oi_daily VALUES(?,?,?,?)", rows)
                ls = _get(fetch, "/futures/data/globalLongShortAccountRatio", {"symbol": sym, "period": "1d", "limit": 30})
                lrows = [(sym, datetime.fromtimestamp(int(x["timestamp"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                          float(x["longShortRatio"]), float(x["longAccount"])) for x in ls]
                con.executemany("INSERT OR REPLACE INTO ls_daily VALUES(?,?,?,?)", lrows)
                stats["oi_rows"] += len(rows)
                stats["ls_rows"] += len(lrows)
                oi_date = today
            con.execute("INSERT OR REPLACE INTO deriv_state VALUES(?,?,?,?,?,?)",
                        (sym, 1, last_f, done, oi_date, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
            con.commit()
            consec = 0
            if done != 2:
                stats["pending"] += 1
        except Exception as e:
            stats["errors"] += 1
            consec += 1
            if stats["errors"] <= 3:
                stats.setdefault("first_errors", []).append(f"{sym}: {str(e)[:120]}")
            if consec >= MAX_CONSEC_ERRORS:
                break                           # endpoint unreachable from here: stop quietly, retry next run
    stats["seconds"] = round(time.time() - t0, 1)
    return stats


def snapshot(con, symbols):
    """Latest derivatives state per symbol for the export: funding (last, 7-day avg, annualised), OI change, L/S."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='funding'").fetchone():
        return {}
    out = {}
    for s in symbols:
        f = con.execute("SELECT time_ms, rate FROM funding WHERE symbol=? ORDER BY time_ms DESC LIMIT 21", (s,)).fetchall()
        if not f:
            continue
        o = con.execute("SELECT date, oi_usdt FROM oi_daily WHERE symbol=? ORDER BY date DESC LIMIT 8", (s,)).fetchall()
        l = con.execute("SELECT ratio, long_pct FROM ls_daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (s,)).fetchone()
        avg7 = sum(r for _, r in f) / len(f)
        out[s] = {"funding": f[0][1], "funding_7d": avg7, "funding_apr": avg7 * 3 * 365 * 100,
                  "oi_usdt": o[0][1] if o else None,
                  "oi_chg_7d": (o[0][1] / o[-1][1] - 1) * 100 if len(o) >= 8 and o[-1][1] else None,
                  "ls_ratio": l[0] if l else None, "long_pct": l[1] if l else None}
    return out
