"""
4-hour candles for BinanceData (Python 3.9 stdlib only).

Scope: every USDT pair (trading, halted or delisted; stablecoins and wrapped duplicates excluded) that traded at
least $100k on some day in the last H4_DAYS days. Delisted pairs are kept so 4h backtests are free of
survivorship bias. Candles are stored exactly as Binance returns them (closed candles only), in candles_4h.

The first download is spread over several hourly runs (H4_BUDGET_S seconds each) so the daily data, analytics
and website are never delayed for long. After that each run only fetches the few new candles.

Accuracy: every run rebuilds a sample of complete UTC days from six 4h candles and compares them with the
daily candles already stored (open, high, low, close, volume); any mismatch is logged and kept in
h4_audit for review.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("binance")

H4_DAYS = 1095            # 3 years (approved 2026-10-25)
H4_MS = 4 * 3600 * 1000
DAY_MS = 86_400_000
H4_BUDGET_S = 2700        # max seconds of 4h downloading per hourly run (first download only)
H4_MIN_DAY_VOL = 100_000
AUDIT_SAMPLE = 30
PARALLEL = 12              # concurrent requests for top-ups (~1,100 weight a minute of the 6,000 allowed)

SCHEMA_4H = """
CREATE TABLE IF NOT EXISTS candles_4h(
    symbol TEXT NOT NULL, open_ms INTEGER NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume_base REAL, volume_usdt REAL, trades INTEGER, taker_buy_base REAL, taker_buy_usdt REAL,
    close_ms INTEGER NOT NULL, fetched_utc TEXT,
    PRIMARY KEY(symbol, open_ms)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_c4h_time ON candles_4h(open_ms);
CREATE TABLE IF NOT EXISTS h4_state(symbol TEXT PRIMARY KEY, start_ms INTEGER, last_ms INTEGER,
    backfilled INTEGER DEFAULT 0, finished INTEGER DEFAULT 0, updated_utc TEXT);
CREATE TABLE IF NOT EXISTS h4_anomalies(symbol TEXT, open_ms INTEGER, issue TEXT, logged_utc TEXT,
    PRIMARY KEY(symbol, open_ms, issue));
CREATE TABLE IF NOT EXISTS h4_audit(run_utc TEXT, symbol TEXT, date TEXT, field TEXT, daily REAL, from_4h REAL);
"""


def _validate(o, h, l, c, vb, vq):
    issues = []
    if min(o, h, l, c) <= 0:
        issues.append("non_positive_price")
    if h < max(o, c) or l > min(o, c) or h < l:
        issues.append("ohlc_inconsistent")
    if vb < 0 or vq < 0:
        issues.append("negative_volume")
    return issues


def scope(con, today):
    """Pairs that need 4h candles, with the first day to fetch from."""
    from binance_sync import STABLE_BASES
    from bot_core import excluded_tokens
    tokens = dict(con.execute("SELECT symbol, token FROM pairs"))
    excl = excluded_tokens(set(tokens.values())) | set(STABLE_BASES)
    since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=H4_DAYS)).strftime("%Y-%m-%d")
    rows = con.execute("SELECT symbol, MIN(date), MAX(date) FROM daily WHERE date >= ? AND volume_usdt >= ? GROUP BY symbol",
                       (since, H4_MIN_DAY_VOL)).fetchall()
    status = dict(con.execute("SELECT symbol, status FROM pairs"))
    out = []
    for sym, first, last in rows:
        if tokens.get(sym) in excl:
            continue
        out.append((sym, since, status.get(sym) == "TRADING"))
    return out


def _ms(d):
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _fetch(api, sym, start_ms, now_ms, deadline):
    """Closed 4h candles from start_ms on. Returns (rows, reached_end); stops early at the deadline."""
    rows, cur = [], start_ms
    while cur < now_ms:
        if time.time() > deadline:
            return rows, False
        batch = api("/api/v3/klines", {"symbol": sym, "interval": "4h", "startTime": cur, "limit": 1000})
        if not batch:
            break
        rows.extend(k for k in batch if int(k[6]) < now_ms)
        nxt = int(batch[-1][0]) + H4_MS
        if nxt <= cur or len(batch) < 1000:
            break
        cur = nxt
    return rows, True


def _store(con, sym, klines, stamp):
    out = []
    for k in klines:
        o, h, l, c, vb, vq = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[7])
        for issue in _validate(o, h, l, c, vb, vq):
            con.execute("INSERT OR IGNORE INTO h4_anomalies VALUES(?,?,?,?)", (sym, int(k[0]), issue, stamp))
        out.append((sym, int(k[0]), o, h, l, c, vb, vq, None if k[8] is None else int(k[8]),
                    None if k[9] is None else float(k[9]), None if k[10] is None else float(k[10]), int(k[6]), stamp))
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO candles_4h VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    return con.total_changes - before


def audit(con, stamp, n=AUDIT_SAMPLE):
    """Rebuild sampled complete UTC days from 4h candles and compare with the stored daily candle."""
    days = con.execute("""SELECT symbol, open_ms / 86400000 AS dd, COUNT(*) FROM candles_4h
                          GROUP BY symbol, dd HAVING COUNT(*) = 6 ORDER BY RANDOM() LIMIT ?""", (n,)).fetchall()
    bad = checked = 0
    import venues
    for sym, dd, _ in days:
        if venues.is_external(sym):          # this audit checks Binance's own consistency; other venues round differently
            continue
        c4 = con.execute("SELECT open, high, low, close, volume_usdt FROM candles_4h WHERE symbol=? AND open_ms >= ? AND open_ms < ? ORDER BY open_ms",
                         (sym, dd * DAY_MS, (dd + 1) * DAY_MS)).fetchall()
        d = datetime.fromtimestamp(dd * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
        row = con.execute("SELECT open, high, low, close, volume_usdt FROM daily WHERE symbol=? AND date=?", (sym, d)).fetchone()
        if not row or len(c4) != 6:
            continue
        checked += 1
        agg = (c4[0][0], max(x[1] for x in c4), min(x[2] for x in c4), c4[-1][3], sum(x[4] or 0 for x in c4))
        ok = True
        for name, a, b in zip(("open", "high", "low", "close", "volume_usdt"), row, agg):
            tol = 1e-9 * max(abs(a), 1) if name != "volume_usdt" else 1e-6 * max(abs(a), 1)
            if a is None or b is None or abs(a - b) > tol:
                con.execute("INSERT INTO h4_audit VALUES(?,?,?,?,?,?)", (stamp, sym, d, name, a, b))
                ok = False
        bad += 0 if ok else 1
    return checked, bad


def run(con, api, today, stamp, now_ms=None, budget_s=H4_BUDGET_S):
    """One pass. Returns a dict for the log."""
    t0 = time.time()
    con.executescript(SCHEMA_4H)
    now_ms = now_ms or int(time.time() * 1000)
    deadline = t0 + budget_s
    pairs = scope(con, today)
    state = {r[0]: r[1:] for r in con.execute("SELECT symbol, start_ms, last_ms, backfilled, finished FROM h4_state")}
    added = done = pending = errors = 0
    consec = 0
    hard_stop = deadline + 900              # incremental updates may run past the budget, but never this far
    # incremental updates first (cheap), then first-time downloads oldest-first within the time budget
    order = sorted(pairs, key=lambda p: (0 if (p[0] in state and state[p[0]][2]) else 1, p[0]))
    # top-ups of pairs already downloaded: fetched in parallel (a few hundred small requests, ~2 weight each, well
    # under the 6000/minute limit), stored serially below
    todo = [(sym, state[sym][1] + H4_MS) for sym, _, _ in order
            if sym in state and state[sym][2] and not state[sym][3] and state[sym][1] and state[sym][1] + 2 * H4_MS <= now_ms]
    pre = {}
    if todo:
        from concurrent.futures import ThreadPoolExecutor

        def _one(job):
            try:
                return job[0], _fetch(api, job[0], job[1], now_ms, time.time() + 120), None
            except Exception as e:      # recorded and retried serially below
                return job[0], None, e
        with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
            for sym, res, err in ex.map(_one, todo):
                if res is not None:
                    pre[sym] = res
    for sym, since, trading in order:
        st = state.get(sym)
        if st and st[3]:                    # delisted and complete: nothing more will come
            done += 1
            continue
        start = (st[1] + H4_MS) if (st and st[1]) else _ms(since)
        if st and st[2] and start + H4_MS > now_ms:
            done += 1                       # up to date: the next 4h candle has not closed yet
            continue
        if (not (st and st[2]) and time.time() > deadline) or time.time() > hard_stop or consec >= 5:
            pending += 1                    # out of time, or the network is down (5 failures in a row): next run
            continue
        try:
            rows, reached_end = pre.pop(sym) if sym in pre else _fetch(api, sym, start, now_ms, deadline if not (st and st[2]) else time.time() + 120)
        except Exception as e:              # one bad symbol must not stop the rest
            errors += 1
            consec += 1
            log.warning(f"  4h {sym}: {e}")
            continue
        consec = 0
        n = _store(con, sym, rows, stamp)
        added += n
        last = con.execute("SELECT MAX(open_ms) FROM candles_4h WHERE symbol=?", (sym,)).fetchone()[0]
        backfilled = 1 if ((st and st[2]) or reached_end) else 0
        finished = 1 if (not trading and reached_end) else 0
        con.execute("INSERT OR REPLACE INTO h4_state VALUES(?,?,?,?,?,?)",
                    (sym, _ms(since), last, backfilled, finished, stamp))
        con.commit()
        if backfilled:
            done += 1
        else:
            pending += 1
    checked, bad = audit(con, stamp)
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM candles_4h").fetchone()[0]
    return {"pairs": len(pairs), "complete": done, "pending": pending, "added": added, "errors": errors,
            "rows": total, "audit_checked": checked, "audit_bad": bad, "seconds": round(time.time() - t0, 1)}
