"""
ZIG archive (Python 3.9 stdlib only): everything MEXC publishes about ZIGUSDT, kept in our own database
(data/zig.db), beyond the daily / 4h / 1h candles the engine already stores in binance.db (venues.py):

  k15      15-minute candles, back to the oldest MEXC serves, then every new one
  flow     every completed hour of trades: count, taker buys vs taker sells (coins and USDT), VWAP, biggest trade
           (MEXC aggTrades, fetched in windows small enough to never miss a trade)
  book     an order-book snapshot every run: best bid / ask, spread, and USDT resting within 1%, 2% and 5% of mid
  ticker   the 24h ticker every run: last price, 24h volume, change, high, low
Public market data only; no key is read. Runs from the engine every 15 minutes (refresh() is throttled) and writes
web_export/zig.json for the pair page.

    python3 bin/zig_archive.py            refresh now
    python3 bin/zig_archive.py --probe    what MEXC returns (ops: zig-probe)
    python3 bin/zig_archive.py --status   what the archive holds (ops: zig-status)
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import venues  # noqa: E402

SYM = "ZIGUSDT"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(ROOT, "data", "zig.db")
OUT = os.path.join(ROOT, "web_export", "zig.json")
EVERY_S, M15, H = 10 * 60, 900_000, 3_600_000
FLOW_BACK_H = 30                # first run: MEXC keeps roughly the last day of trades (probed 2026-11-01: 24h yes, 36h no)
SCHEMA = """
CREATE TABLE IF NOT EXISTS k15(open_ms INTEGER PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, vol REAL, qvol REAL);
CREATE TABLE IF NOT EXISTS flow(hour_ms INTEGER PRIMARY KEY, n INTEGER, buy_qty REAL, sell_qty REAL, buy_usdt REAL, sell_usdt REAL,
    vwap REAL, big_usdt REAL, complete INTEGER);
CREATE TABLE IF NOT EXISTS book(t INTEGER PRIMARY KEY, bid REAL, ask REAL, spread_bps REAL, b1 REAL, b2 REAL, b5 REAL, a1 REAL, a2 REAL, a5 REAL,
    levels INTEGER);
CREATE TABLE IF NOT EXISTS ticker(t INTEGER PRIMARY KEY, last REAL, qvol24 REAL, chg24 REAL, high REAL, low REAL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def now_ms():
    return int(time.time() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(fetch, path, params):
    return venues._get(fetch, path, params)


# ---------------------------------------------------------------- 15-minute candles
def fill_k15(con, fetch=None, now=None, log=print):
    """Backfill to the oldest 15m bar MEXC serves, then keep up. Returns rows added."""
    now = now or now_ms()
    last = con.execute("SELECT MAX(open_ms), MIN(open_ms) FROM k15").fetchone()
    added = 0

    def put(rows):
        n = 0
        for r in rows:
            ot = int(r[0])
            if ot + M15 > now:
                continue                                   # the running bar is not stored
            con.execute("INSERT OR REPLACE INTO k15 VALUES(?,?,?,?,?,?,?)", (ot, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7])))
            n += 1
        return n
    if last[0] is None:                                    # backfill, newest first, until MEXC returns nothing
        end, empty = now, 0
        while empty < 2:
            start = end - 500 * M15
            rows = get(fetch, "/api/v3/klines", {"symbol": SYM, "interval": "15m", "startTime": start, "endTime": end - 1, "limit": 500})
            n = put(rows)
            added += n
            empty = empty + 1 if not rows else 0
            nxt = min([int(r[0]) for r in rows], default=start)
            if nxt >= end:
                break                                      # no older bars came back: done
            end = nxt
            con.commit()
            time.sleep(0.15 if fetch is None else 0)
        log(f"zig: 15m candles backfilled, {added} bars from {iso(con.execute('SELECT MIN(open_ms) FROM k15').fetchone()[0] or now)}")
    else:
        start = last[0] + M15
        while start + M15 <= now:
            rows = get(fetch, "/api/v3/klines", {"symbol": SYM, "interval": "15m", "startTime": start, "endTime": min(now, start + 500 * M15) - 1, "limit": 500})
            added += put(rows)
            if not rows:
                start += 500 * M15
                continue
            start = max(int(r[0]) for r in rows) + M15
    con.commit()
    return added


# ---------------------------------------------------------------- trade flow
def window_trades(fetch, a, b, depth=0):
    """Every aggregated trade in [a, b) (ms). Windows that return MEXC's 1,000-row cap are split in two, so no
    trade is lost."""
    rows = get(fetch, "/api/v3/aggTrades", {"symbol": SYM, "startTime": a, "endTime": b - 1, "limit": 1000})
    if len(rows) >= 1000 and b - a > 2000 and depth < 12:
        m = (a + b) // 2
        return window_trades(fetch, a, m, depth + 1) + window_trades(fetch, m, b, depth + 1)
    return rows


def hour_volumes(fetch, a, b):
    """MEXC's own 1-hour candles for [a, b): open_ms -> USDT traded. Every hour of trades is checked against it."""
    out = {}
    while a < b:
        rows = get(fetch, "/api/v3/klines", {"symbol": SYM, "interval": "60m", "startTime": a, "endTime": b - 1, "limit": 1000})
        if not rows:
            break
        out.update({int(r[0]): float(r[7]) for r in rows})
        nxt = max(int(r[0]) for r in rows) + H
        if nxt <= a:
            break
        a = nxt
    return out


def fill_flow(con, fetch=None, now=None, log=print):
    """Summarise every completed hour since the last one stored (first run: as far back as MEXC serves trades).
    Each hour is checked against MEXC's 1-hour candle: it is marked complete only when its trades add up to the candle's
    volume (within 0.5%). An hour MEXC no longer serves (no trades but candle volume) is not stored at all: a gap is never
    recorded as a quiet hour. Returns hours stored."""
    now = now or now_ms()
    last = con.execute("SELECT MAX(hour_ms) FROM flow").fetchone()[0]
    h = (last + H) if last else (now // H - FLOW_BACK_H) * H
    if h + H > now:
        return 0
    vol = hour_volumes(fetch, h, now // H * H)
    added = gaps = 0
    while h + H <= now:
        kq = vol.get(h)
        if kq is None:
            break                                          # MEXC has not closed this hour's candle yet: next run
        try:
            tr = window_trades(fetch, h, h + H)
        except Exception as e:
            if not added:
                raise                                      # nothing fetched at all: report it as a failed step
            log(f"zig: trades for {iso(h)} not fetched ({str(e)[:100]})")
            break
        n = len(tr)
        bq = sum(float(t["q"]) for t in tr if not t.get("m"))           # buyer was the taker (m = buyer is maker)
        sq = sum(float(t["q"]) for t in tr if t.get("m"))
        bu = sum(float(t["q"]) * float(t["p"]) for t in tr if not t.get("m"))
        su = sum(float(t["q"]) * float(t["p"]) for t in tr if t.get("m"))
        if n == 0 and kq > 0:
            gaps += 1                                      # trades no longer served for this hour: leave it out
            h += H
            continue
        ok = abs(bu + su - kq) <= max(1.0, 0.005 * kq)
        vw = (bu + su) / (bq + sq) if bq + sq else None
        big = max((float(t["q"]) * float(t["p"]) for t in tr), default=0.0)
        con.execute("INSERT OR REPLACE INTO flow VALUES(?,?,?,?,?,?,?,?,?)", (h, n, bq, sq, bu, su, vw, big, 1 if ok else 0))
        if not ok:
            log(f"zig: trades for {iso(h)} add up to ${bu + su:,.0f} but the candle says ${kq:,.0f}; kept, marked incomplete")
        added += 1
        h += H
        if fetch is None:
            time.sleep(0.12)
    if gaps and last:
        log(f"zig: {gaps} hours of trades were no longer served by MEXC (the archive was not refreshed in time)")
    con.commit()
    return added


# ---------------------------------------------------------------- book and ticker
def snap_book(con, fetch=None, now=None):
    d = get(fetch, "/api/v3/depth", {"symbol": SYM, "limit": 1000})
    bids = [(float(p), float(q)) for p, q, *_ in d.get("bids", [])]
    asks = [(float(p), float(q)) for p, q, *_ in d.get("asks", [])]
    if not bids or not asks:
        return None
    bid, ask = bids[0][0], asks[0][0]
    mid = (bid + ask) / 2
    dep = lambda side, pct, below: sum(p * q for p, q in side if (p >= mid * (1 - pct) if below else p <= mid * (1 + pct)))  # noqa: E731
    row = ((now or now_ms()) // 1000, bid, ask, round(1e4 * (ask - bid) / mid, 2), dep(bids, .01, True), dep(bids, .02, True), dep(bids, .05, True),
           dep(asks, .01, False), dep(asks, .02, False), dep(asks, .05, False), len(bids) + len(asks))
    con.execute("INSERT OR REPLACE INTO book VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
    return row


def snap_ticker(con, fetch=None, now=None):
    t = venues.ticker24(fetch, SYM)
    row = ((now or now_ms()) // 1000, float(t["lastPrice"]), float(t["quoteVolume"]), float(t["priceChangePercent"]), float(t["highPrice"]), float(t["lowPrice"]))
    con.execute("INSERT OR REPLACE INTO ticker VALUES(?,?,?,?,?,?)", row)
    return row


# ---------------------------------------------------------------- export
def build(con, now=None):
    now = now or now_ms()
    q = lambda sql, a=(): [list(r) for r in con.execute(sql, a)]  # noqa: E731
    flow = q("SELECT hour_ms, n, buy_usdt, sell_usdt, vwap, big_usdt FROM flow WHERE complete=1 AND hour_ms >= ? ORDER BY hour_ms", (now - 14 * 86400_000,))
    d24 = [r for r in flow if r[0] >= now - 25 * H][-24:]
    book = q("SELECT t, bid, ask, spread_bps, b1, b2, b5, a1, a2, a5 FROM book WHERE t >= ? ORDER BY t", (now // 1000 - 14 * 86400,))
    hourly_book, seen = [], set()
    for r in book:                                        # one snapshot an hour for the chart
        k = r[0] // 3600
        if k not in seen:
            seen.add(k)
            hourly_book.append(r)
    tick = con.execute("SELECT t, last, qvol24, chg24, high, low FROM ticker ORDER BY t DESC LIMIT 1").fetchone()
    k15 = q("SELECT open_ms, o, h, l, c, qvol FROM k15 WHERE open_ms >= ? ORDER BY open_ms", (now - 7 * 86400_000,))
    span = con.execute("SELECT MIN(open_ms), MAX(open_ms), COUNT(*) FROM k15").fetchone()
    fl = con.execute("SELECT MIN(hour_ms), COUNT(*), SUM(n) FROM flow WHERE complete=1").fetchone()
    return {"generated_utc": iso(now), "symbol": SYM, "venue": "MEXC",
            "now": {"book": book[-1] if book else None, "ticker": list(tick) if tick else None,
                    "day": {"trades": sum(r[1] for r in d24), "buy_usdt": sum(r[2] for r in d24), "sell_usdt": sum(r[3] for r in d24),
                            "big_usdt": max((r[5] for r in d24), default=0), "hours": len(d24)}},
            "flow": flow, "book": hourly_book, "k15": k15,
            "archive": {"k15_from": iso(span[0]) if span[0] else None, "k15_bars": span[2], "flow_from": iso(fl[0]) if fl[0] else None,
                        "flow_hours": fl[1], "trades": fl[2] or 0,
                        "flow_incomplete": con.execute("SELECT COUNT(*) FROM flow WHERE complete=0").fetchone()[0], "book_snaps": con.execute("SELECT COUNT(*) FROM book").fetchone()[0]},
            "columns": {"flow": ["hour_ms", "trades", "buy_usdt", "sell_usdt", "vwap", "biggest_usdt"],
                        "book": ["t", "bid", "ask", "spread_bps", "bid_1pct", "bid_2pct", "bid_5pct", "ask_1pct", "ask_2pct", "ask_5pct"],
                        "k15": ["open_ms", "o", "h", "l", "c", "usdt"]}}


def refresh(force=False, log=print, fetch=None, out=OUT, db=DB, now=None):
    try:
        if not force and time.time() - os.path.getmtime(out) < EVERY_S:
            return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(db), exist_ok=True)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    con = sqlite3.connect(db, timeout=60)
    con.executescript(SCHEMA)
    ok = 0
    for name, fn in (("book", lambda: snap_book(con, fetch, now)), ("ticker", lambda: snap_ticker(con, fetch, now)),
                     ("trades", lambda: fill_flow(con, fetch, now, log)), ("15m candles", lambda: fill_k15(con, fetch, now, log))):
        try:
            fn()
            ok += 1
        except Exception as e:
            log(f"zig: {name} skipped ({str(e)[:120]})")
    con.commit()
    if not ok:
        con.close()
        return False
    d = build(con, now)
    con.close()
    with open(out + ".tmp", "w") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(out + ".tmp", out)
    return True


def status(db=DB):
    if not os.path.exists(db):
        print(f"{db} not created yet (the engine's next run creates it, or run zig-refresh)")
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    d = build(con)
    a = d["archive"]
    print(f"zig.db: 15m candles {a['k15_bars']} from {a['k15_from']}; trade flow {a['flow_hours']} hours ({a['trades']} trades) from {a['flow_from']}; "
          f"{a['book_snaps']} order-book snapshots; {a['flow_incomplete']} hours kept but not matching the candle")
    if d["now"]["book"]:
        b = d["now"]["book"]
        print(f"now: bid {b[1]} ask {b[2]} spread {b[3]} bp; resting within 2%: bids ${b[5]:,.0f}, asks ${b[8]:,.0f}")
    dy = d["now"]["day"]
    print(f"last 24h: {dy['trades']} trades, taker buys ${dy['buy_usdt']:,.0f} vs sells ${dy['sell_usdt']:,.0f}, biggest ${dy['big_usdt']:,.0f}")


def probe():
    """What MEXC really serves: how far back trades go, and whether the trades add up to the candles' volume."""
    t0 = now_ms() // H * H
    for back in (1, 2, 4, 8, 12, 24, 36, 48, 72, 120):
        a = t0 - back * H
        try:
            tr = window_trades(None, a, a + H)
            k = get(None, "/api/v3/klines", {"symbol": SYM, "interval": "60m", "startTime": a, "endTime": a + H - 1, "limit": 2})
            kq = float(k[0][7]) if k else None
            tq = sum(float(t["q"]) * float(t["p"]) for t in tr)
            span = f"{iso(min(t['T'] for t in tr))}..{iso(max(t['T'] for t in tr))}" if tr else "-"
            print(f"hour {iso(a)} ({back}h ago): {len(tr)} trades ${tq:,.0f} | 1h candle ${kq:,.0f} | trade times {span}" if kq is not None
                  else f"hour {iso(a)}: {len(tr)} trades ${tq:,.0f} | no candle")
        except Exception as e:
            print(f"hour {iso(a)}: FAILED {str(e)[:160]}")
    try:
        r = get(None, "/api/v3/aggTrades", {"symbol": SYM, "limit": 1000})
        print(f"latest 1000 (no time): {len(r)} rows, {iso(min(t['T'] for t in r))}..{iso(max(t['T'] for t in r))}, ${sum(float(t['q']) * float(t['p']) for t in r):,.0f}")
        r = get(None, "/api/v3/trades", {"symbol": SYM, "limit": 1000})
        print(f"/trades latest: {len(r)} rows, {iso(min(t['time'] for t in r))}..{iso(max(t['time'] for t in r))}, ${sum(float(t['quoteQty']) for t in r):,.0f}")
    except Exception as e:
        print(f"latest: FAILED {str(e)[:160]}")
    for p in ({"interval": "15m", "startTime": 1625097600000, "limit": 3}, {"interval": "15m", "startTime": t0 - 400 * 86400_000, "limit": 3},
              {"interval": "15m", "startTime": t0 - 200 * 86400_000, "limit": 3}, {"interval": "15m", "startTime": t0 - 60 * 86400_000, "limit": 3}):
        r = get(None, "/api/v3/klines", dict(symbol=SYM, endTime=p["startTime"] + 3 * M15, **p))
        print(f"15m candles from {iso(p['startTime'])}: {len(r)} rows")
    d = get(None, "/api/v3/depth", {"symbol": SYM, "limit": 1000})
    print(f"depth: bids {len(d.get('bids', []))}, asks {len(d.get('asks', []))}, top {d['bids'][0]} / {d['asks'][0]}")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    elif "--status" in sys.argv:
        status()
    else:
        refresh(force=True)
