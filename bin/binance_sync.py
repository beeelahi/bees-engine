#!/usr/bin/env python3
"""
BinanceData sync engine - Binance spot USDT pairs, daily candles.

Design goals: no silent errors, no lost history, safe to run any number of times.

  * First run backfills every USDT spot pair's full daily history since its Binance listing
    (needed so long EMAs/RSI/MACD on weekly and monthly are exact). Exports show the last
    DISPLAY_DAYS; the database keeps everything and grows forward from there.
  * Every later run fetches only what is missing (catch-up after days offline works
    the same way: every missing day is detected and fetched).
  * Only CLOSED daily candles are stored (Binance day = 00:00-23:59:59 UTC).
  * Raw data for all USDT pairs is kept, including pairs that later get delisted
    (keeps the record free of survivorship bias). The rules (USDT only, >= $100k 24h
    volume, no stablecoins) are applied to the exports and the dashboard view.
  * Every candle is sanity-checked (OHLC consistency, positive prices); problems go
    to the `anomalies` table. Days Binance itself has no candle for are recorded in
    `gaps` so they are never mistaken for fetch failures.
  * Fast path: if the last completed UTC day is already fully synced, the run exits
    without touching the network. Running hourly costs essentially nothing.
  * Single-instance lock, atomic export writes, run log, status.json.

Usage
  python3 binance_sync.py              # normal sync (what the scheduler runs)
  python3 binance_sync.py --force      # ignore fast path, re-check everything
  python3 binance_sync.py --audit 100  # re-download 100 random stored candles and
                                       # compare against the DB field-by-field
Python 3.8+, standard library only. Uses the system `curl` for HTTPS.
"""
import argparse, csv, fcntl, json, logging, logging.handlers, os, platform
import re, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import venues  # noqa: E402  pairs tracked from another exchange (ZIG on MEXC)

# ---------------------------------------------------------------- settings
HISTORY_START = "2017-07-01"   # before Binance's first candles: = full history since each listing
DISPLAY_DAYS = 730             # CSV exports show the last 2 years; the DB keeps everything
QUOTE = "USDT"
MIN_VOL_USDT = 100_000          # 24h quote volume floor for the filtered view
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "EUR", "EURI", "AEUR",
                "GBP", "AUD", "TRY", "BRL", "PAX", "USD1", "XUSD", "USDS", "SUSD", "UST",
                "BFUSD", "USDE", "PYUSD", "RLUSD", "USDD", "FRAX", "LUSD", "GUSD", "U"}
HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")
EXPORTS = os.path.join(ROOT, "exports")
LOGS = os.path.join(ROOT, "logs")
DB = os.path.join(DATA, "binance.db")
STATUS = os.path.join(ROOT, "status.json")
DAY_MS = 86_400_000

log = logging.getLogger("binance_sync")


# ---------------------------------------------------------------- helpers
def utc_now():
    return datetime.now(timezone.utc)


def d2ms(d):
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def ms2d(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def day_range(a, b):
    cur, end = datetime.strptime(a, "%Y-%m-%d"), datetime.strptime(b, "%Y-%m-%d")
    while cur <= end:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def setup_logging(verbose):
    os.makedirs(LOGS, exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(os.path.join(LOGS, "sync.log"),
                                              maxBytes=2_000_000, backupCount=5)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    if verbose or sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(sh)


# ---------------------------------------------------------------- HTTP
class HttpError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body[:200]}")
        self.code, self.body = code, body


_CURL = shutil.which("curl")


def _fetch(url):
    """Return (status, headers{lower}, body_text). curl first (system CA store), urllib fallback."""
    if _CURL:
        with tempfile.TemporaryDirectory() as td:
            hf, bf = os.path.join(td, "h"), os.path.join(td, "b")
            p = subprocess.run([_CURL, "-sS", "--compressed", "--connect-timeout", "15", "--max-time", "180", "-A", "BinanceData/2.0",
                                "-D", hf, "-o", bf, "-w", "%{http_code}", url],
                               capture_output=True, text=True)
            if p.returncode != 0:
                raise ConnectionError(f"curl exit {p.returncode}: {p.stderr.strip()[:200]}")
            code = int(p.stdout.strip() or 0)
            headers = {}
            with open(hf, encoding="latin-1") as f:
                for line in f:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
            with open(bf, encoding="utf-8") as f:
                return code, headers, f.read()
    req = urllib.request.Request(url, headers={"User-Agent": "BinanceData/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode(errors="replace")


def api(path, params=None, retries=5):
    if path == "/api/v3/klines" and params and venues.is_external(params.get("symbol")):
        return venues.klines(_fetch, params)             # same rows as Binance, from the pair's own venue
    q = ("?" + urllib.parse.urlencode(params)) if params else ""   # encodes non-ASCII symbols
    last = None
    for host in HOSTS:
        for attempt in range(retries):
            try:
                code, h, body = _fetch(host + path + q)
            except Exception as e:
                last = e
                log.warning(f"  {path} attempt {attempt + 1} via {host}: {e}")
                time.sleep(min(2 ** attempt, 30))
                continue
            if code == 200:
                w = h.get("x-mbx-used-weight-1m")
                if w and int(w) > 4800:                      # hard limit 6000/min
                    log.info(f"  weight {w}/6000, pausing 20s")
                    time.sleep(20)
                return json.loads(body)
            last = HttpError(code, body)
            if code in (418, 429):
                wait = int(h.get("retry-after", "60"))
                log.warning(f"  rate limited ({code}), sleeping {wait}s")
                time.sleep(wait)
            elif code == 400:                                # bad symbol etc: don't retry
                raise last
            elif code in (403, 451):                         # host blocked here: try next host
                break
            else:
                time.sleep(min(2 ** attempt, 30))
    raise last if last else RuntimeError("request failed")


# ---------------------------------------------------------------- DB
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pairs(
    symbol TEXT PRIMARY KEY, token TEXT NOT NULL, status TEXT NOT NULL,
    first_seen TEXT, last_seen_trading TEXT, delisted_on TEXT,
    backfilled INTEGER DEFAULT 0, first_candle TEXT);
CREATE TABLE IF NOT EXISTS daily(
    symbol TEXT NOT NULL, date TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume_base REAL, volume_usdt REAL, trades INTEGER,
    taker_buy_base REAL, taker_buy_usdt REAL,
    open_time_ms INTEGER, close_time_ms INTEGER, fetched_utc TEXT,
    PRIMARY KEY(symbol, date));
CREATE INDEX IF NOT EXISTS ix_daily_date ON daily(date);
CREATE TABLE IF NOT EXISTS ticker_snapshots(
    snap_date TEXT, symbol TEXT, last_price REAL, quote_volume_24h REAL, taken_utc TEXT,
    PRIMARY KEY(snap_date, symbol));
CREATE TABLE IF NOT EXISTS gaps(symbol TEXT, date TEXT, confirmed_utc TEXT,
    PRIMARY KEY(symbol, date));
CREATE TABLE IF NOT EXISTS anomalies(symbol TEXT, date TEXT, issue TEXT, logged_utc TEXT,
    PRIMARY KEY(symbol, date, issue));
CREATE TABLE IF NOT EXISTS supply(token TEXT PRIMARY KEY, circulating REAL NOT NULL, name TEXT, as_of TEXT);
CREATE TABLE IF NOT EXISTS fear_greed(date TEXT PRIMARY KEY, value INTEGER NOT NULL, label TEXT);
CREATE TABLE IF NOT EXISTS runs(run_utc TEXT, ok INTEGER, synced_through TEXT,
    pairs_checked INTEGER, rows_added INTEGER, failures INTEGER, seconds REAL, note TEXT);
"""


def meta_get(con, k, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


# ---------------------------------------------------------------- core
def validate(sym, d, o, h, l, c, vb, vq):
    issues = []
    if min(o, h, l, c) <= 0:
        issues.append("non_positive_price")
    if h < max(o, c) or l > min(o, c) or h < l:
        issues.append("ohlc_inconsistent")
    if vb < 0 or vq < 0:
        issues.append("negative_volume")
    return issues


def fetch_range(sym, start_d, end_d, now_ms):
    """All CLOSED daily candles for sym between dates inclusive."""
    rows, cur, end_ms = [], d2ms(start_d), d2ms(end_d)
    while cur <= end_ms:
        batch = api("/api/v3/klines", {"symbol": sym, "interval": "1d", "startTime": cur,
                                       "endTime": end_ms + DAY_MS - 1, "limit": 1000})
        if not batch:
            break
        rows.extend(k for k in batch if int(k[6]) < now_ms)   # closed candles only
        cur = int(batch[-1][0]) + DAY_MS
        if len(batch) < 1000:
            break
    return rows


def store(con, sym, klines, stamp):
    out, n_anom = [], 0
    for k in klines:
        d = ms2d(int(k[0]))
        o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        vb, vq = float(k[5]), float(k[7])
        for issue in validate(sym, d, o, h, l, c, vb, vq):
            con.execute("INSERT OR IGNORE INTO anomalies VALUES(?,?,?,?)", (sym, d, issue, stamp))
            n_anom += 1
        out.append((sym, d, o, h, l, c, vb, vq, _int(k[8]), _num(k[9]), _num(k[10]),
                    int(k[0]), int(k[6]), stamp))
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    return con.total_changes - before, n_anom


def _num(x):
    return None if x is None else float(x)


def _int(x):
    return None if x is None else int(x)


def contiguous(dates):
    """['2024-01-01','2024-01-02','2024-01-05'] -> [('2024-01-01','2024-01-02'),('2024-01-05','2024-01-05')]"""
    out = []
    for d in sorted(dates):
        if out and (datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(out[-1][1], "%Y-%m-%d")).days == 1:
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


def sync_pair(con, sym, hist_start, last_day, now_ms, stamp):
    """Bring one pair fully up to date. Returns (rows_added, complete_bool)."""
    backfilled, first_candle = con.execute(
        "SELECT backfilled, first_candle FROM pairs WHERE symbol=?", (sym,)).fetchone()
    added = 0
    if not backfilled:
        rows = fetch_range(sym, hist_start, last_day, now_ms)
        a, _ = store(con, sym, rows, stamp)
        added += a
        fc = ms2d(int(rows[0][0])) if rows else None
        con.execute("UPDATE pairs SET backfilled=1, first_candle=? WHERE symbol=?", (fc, sym))
        first_candle = fc
    if first_candle is None:
        # Pair had no candles at backfill time (listed after). Look again from hist_start.
        rows = fetch_range(sym, hist_start, last_day, now_ms)
        if rows:
            a, _ = store(con, sym, rows, stamp)
            added += a
            first_candle = ms2d(int(rows[0][0]))
            con.execute("UPDATE pairs SET first_candle=? WHERE symbol=?", (first_candle, sym))
        con.commit()
        return added, True                      # no data yet is a valid state for a new listing

    start = max(first_candle, hist_start)
    have = {r[0] for r in con.execute("SELECT date FROM daily WHERE symbol=? AND date>=?", (sym, start))}
    have |= {r[0] for r in con.execute("SELECT date FROM gaps WHERE symbol=?", (sym,))}
    missing = [d for d in day_range(start, last_day) if d not in have]
    for a_d, b_d in contiguous(missing):
        rows = fetch_range(sym, a_d, b_d, now_ms)
        a, _ = store(con, sym, rows, stamp)
        added += a
    if missing:
        have = {r[0] for r in con.execute("SELECT date FROM daily WHERE symbol=? AND date>=?", (sym, start))}
        latest = max(have) if have else None
        still = [d for d in missing if d not in have]
        for d in still:
            if latest and d < latest:           # Binance has later data but not this day: real gap
                con.execute("INSERT OR IGNORE INTO gaps VALUES(?,?,?)", (sym, d, stamp))
        tail_missing = [d for d in still if not latest or d > latest]
        con.commit()
        if tail_missing:
            log.warning(f"  {sym}: no candle yet for {tail_missing[0]}..{tail_missing[-1]} (will retry)")
            return added, False
    con.commit()
    return added, True


def refresh_universe(con, today, stamp):
    info = api("/api/v3/exchangeInfo", {"permissions": "SPOT"})
    log.info(f"  exchangeInfo: {len(info['symbols'])} spot symbols")
    tick = {t["symbol"]: t for t in api("/api/v3/ticker/24hr", {"type": "MINI"})}
    log.info(f"  24h tickers: {len(tick)}")
    seen = set()
    for s in info["symbols"]:
        if s.get("quoteAsset") != QUOTE:
            continue
        if not s["symbol"].isascii():          # non-Latin tickers excluded by policy
            continue
        sym, st = s["symbol"], s.get("status", "")
        seen.add(sym)
        row = con.execute("SELECT status FROM pairs WHERE symbol=?", (sym,)).fetchone()
        if row is None:
            con.execute("INSERT INTO pairs(symbol, token, status, first_seen, last_seen_trading)"
                        " VALUES(?,?,?,?,?)", (sym, s["baseAsset"], st, today,
                                               today if st == "TRADING" else None))
        else:
            # delisted_on = the day we saw a pair go from TRADING to not trading. Pairs that were already halted
            # when tracking began keep NULL (we don't know when they stopped).
            con.execute("UPDATE pairs SET delisted_on=CASE WHEN ?='TRADING' THEN NULL"
                        " WHEN status='TRADING' THEN ? ELSE delisted_on END,"
                        " status=?, last_seen_trading=COALESCE(?, last_seen_trading) WHERE symbol=?",
                        (st, today, st, today if st == "TRADING" else None, sym))
        t = tick.get(sym)
        if t and st == "TRADING":
            con.execute("INSERT OR IGNORE INTO ticker_snapshots VALUES(?,?,?,?,?)",
                        (today, sym, float(t["lastPrice"]), float(t["quoteVolume"]), stamp))
    for sym in venues.EXTERNAL:                  # pairs from another venue; Binance wins if it lists the same symbol
        if sym in seen:
            continue
        s = venues.exchange_info(_fetch, sym)
        if not s:
            continue
        seen.add(sym)
        st = s["status"]
        row = con.execute("SELECT status FROM pairs WHERE symbol=?", (sym,)).fetchone()
        if row is None:
            con.execute("INSERT INTO pairs(symbol, token, status, first_seen, last_seen_trading) VALUES(?,?,?,?,?)",
                        (sym, s["baseAsset"], st, today, today if st == "TRADING" else None))
        else:
            con.execute("UPDATE pairs SET delisted_on=CASE WHEN ?='TRADING' THEN NULL WHEN status='TRADING' THEN ? ELSE delisted_on END,"
                        " status=?, last_seen_trading=COALESCE(?, last_seen_trading) WHERE symbol=?",
                        (st, today, st, today if st == "TRADING" else None, sym))
        try:
            t = venues.ticker24(_fetch, sym)
            if st == "TRADING":
                con.execute("INSERT OR IGNORE INTO ticker_snapshots VALUES(?,?,?,?,?)",
                            (today, sym, float(t["lastPrice"]), float(t["quoteVolume"]), stamp))
        except Exception as e:
            log.warning(f"  {sym}: 24h ticker from {venues.venue(sym)} failed: {e}")
        log.info(f"  {sym}: tracked from {venues.venue(sym)} ({st})")
    for (sym,) in con.execute("SELECT symbol FROM pairs").fetchall():
        if not sym.isascii():
            for t in ("pairs", "daily", "gaps", "anomalies", "ticker_snapshots"):
                con.execute(f"DELETE FROM {t} WHERE symbol=?", (sym,))
    for (sym,) in con.execute("SELECT symbol FROM pairs WHERE status!='DELISTED'").fetchall():
        if sym not in seen:
            con.execute("UPDATE pairs SET delisted_on=CASE WHEN status='TRADING' THEN ? ELSE delisted_on END,"
                        " status='DELISTED' WHERE symbol=?", (today, sym))
    con.commit()


def run_sync(con, force):
    t0 = time.time()
    now = utc_now()
    now_ms = int(now.timestamp() * 1000)
    today = now.strftime("%Y-%m-%d")
    last_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")    # last completed UTC day
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    hist_start = meta_get(con, "history_start")
    if not hist_start or hist_start > HISTORY_START:
        if hist_start:
            log.info(f"History start moved {hist_start} -> {HISTORY_START}: extending every pair back to listing")
            con.execute("UPDATE pairs SET backfilled=0")
            con.execute("DELETE FROM meta WHERE key IN ('synced_through', 'analytics_built_utc')")
        else:
            log.info(f"First run: full history from listing (start {HISTORY_START})")
        hist_start = HISTORY_START
        meta_set(con, "history_start", hist_start)
        con.commit()

    new_ext = [x for x in venues.EXTERNAL if not con.execute("SELECT 1 FROM pairs WHERE symbol=?", (x,)).fetchone()]
    if not force and not new_ext and meta_get(con, "synced_through") == last_day and meta_get(con, "universe_date") == today:
        log.info(f"Up to date through {last_day}. Nothing to do.")
        return {"ok": True, "noop": True, "synced_through": last_day}

    log.info("Refreshing universe (exchangeInfo + 24h tickers) ...")
    refresh_universe(con, today, stamp)
    meta_set(con, "universe_date", today)

    targets = [r[0] for r in con.execute(
        "SELECT symbol FROM pairs WHERE status='TRADING' ORDER BY symbol")]
    # Pairs that stopped trading recently: try once to collect their final candles.
    recent_off = [r[0] for r in con.execute(
        "SELECT symbol FROM pairs WHERE status!='TRADING' AND delisted_on>=? AND backfilled=1",
        ((now - timedelta(days=7)).strftime("%Y-%m-%d"),))]
    log.info(f"Syncing {len(targets)} trading USDT pairs through {last_day} ...")

    added, failures, incomplete = 0, [], []
    for i, sym in enumerate(targets, 1):
        try:
            a, complete = sync_pair(con, sym, hist_start, last_day, now_ms, stamp)
            added += a
            if not complete:
                incomplete.append(sym)
        except Exception as e:
            con.rollback()
            failures.append(sym)
            log.error(f"  {sym}: {e}")
        if i % 50 == 0 or i == len(targets):
            log.info(f"  {i}/{len(targets)} pairs, {added:,} rows added")
    for sym in recent_off:
        try:
            added += sync_pair(con, sym, hist_start, last_day, now_ms, stamp)[0]
        except Exception:
            con.rollback()                       # expected for removed symbols
    # Survivorship: fetch the history of halted/delisted pairs once, so backtests also see the
    # coins that failed. They never enter the screener (status != TRADING).
    dead = [r[0] for r in con.execute("SELECT symbol FROM pairs WHERE status!='TRADING' AND backfilled=0")]
    if dead:
        log.info(f"Backfilling history of {len(dead)} halted/delisted pairs (for unbiased backtests) ...")
        got = 0
        for sym in dead:
            try:
                a = sync_pair(con, sym, hist_start, last_day, now_ms, stamp)[0]
                added += a
                got += a > 0
            except Exception:
                con.rollback()
                con.execute("UPDATE pairs SET backfilled=1 WHERE symbol=?", (sym,))   # no history available
                con.commit()
        log.info(f"  halted/delisted pairs with history: {got}/{len(dead)}")

    ok = not failures and not incomplete
    if ok:
        meta_set(con, "synced_through", last_day)
    con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
                (stamp, int(ok), meta_get(con, "synced_through"), len(targets), added,
                 len(failures) + len(incomplete), round(time.time() - t0, 1),
                 ",".join((failures + incomplete)[:20])))
    con.commit()
    log.info(f"Run {'OK' if ok else 'INCOMPLETE'}: +{added:,} rows, {len(failures)} errors, "
             f"{len(incomplete)} awaiting candles, {time.time() - t0:.0f}s")
    return {"ok": ok, "noop": False, "synced_through": meta_get(con, "synced_through"),
            "rows_added": added, "failed": failures, "incomplete": incomplete}


# ---------------------------------------------------------------- circulating supply (for estimated market cap)
ENGINE_VERSION = "18"     # bump when engine/bot/export code changes so the next hourly run rebuilds once
SUPPLY_URL = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products?includeEtf=false"


def refresh_supply(con, today):
    """Once a day: each listed asset's current circulating supply as published by Binance. Used only for an
    ESTIMATED market cap (price x today's supply). Strict validation; any failure leaves the old values."""
    if meta_get(con, "supply_date") == today:
        return
    code, _, body = _fetch(SUPPLY_URL)
    if code != 200:
        raise RuntimeError(f"supply: HTTP {code}")
    data = json.loads(body).get("data")
    if not isinstance(data, list) or len(data) < 100:
        raise RuntimeError("supply: unexpected response")
    best = {}
    for d in data:
        if not isinstance(d, dict):
            continue
        tok, cs, name = d.get("b"), d.get("cs"), d.get("an")
        if not isinstance(tok, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", tok):
            continue
        try:
            cs = float(cs)
        except (TypeError, ValueError):
            continue
        if not (cs == cs and 0 < cs < 1e16):
            continue
        name = name if isinstance(name, str) and name.isprintable() else None
        best[tok] = (max(cs, best[tok][0]) if tok in best else cs, (name or "")[:60])
    if len(best) < 100:
        raise RuntimeError(f"supply: only {len(best)} valid assets")
    con.executemany("INSERT OR REPLACE INTO supply VALUES(?,?,?,?)", [(t, v[0], v[1], today) for t, v in best.items()])
    meta_set(con, "supply_date", today)
    con.commit()
    log.info(f"Circulating supply updated for {len(best)} assets")


FNG_URL = "https://api.alternative.me/fng/?limit=400&format=json"
FNG_LABELS = {"Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"}


def refresh_fear_greed(con, today):
    """Once a day: the Crypto Fear & Greed Index (alternative.me), last ~400 days. Strictly validated;
    any failure keeps the stored history."""
    if meta_get(con, "fng_date") == today:
        return
    code, _, body = _fetch(FNG_URL)
    if code != 200:
        raise RuntimeError(f"fear & greed: HTTP {code}")
    data = json.loads(body).get("data")
    if not isinstance(data, list) or len(data) < 30:
        raise RuntimeError("fear & greed: unexpected response")
    rows = []
    for d in data:
        if not isinstance(d, dict):
            continue
        try:
            v, ts = int(d.get("value")), int(d.get("timestamp"))
        except (TypeError, ValueError):
            continue
        lab = d.get("value_classification")
        if not (0 <= v <= 100) or lab not in FNG_LABELS or not (1.5e9 < ts < 4e9):
            continue
        rows.append((datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"), v, lab))
    if len(rows) < 30:
        raise RuntimeError("fear & greed: too few valid points")
    con.executemany("INSERT OR REPLACE INTO fear_greed VALUES(?,?,?)", rows)
    meta_set(con, "fng_date", today)
    con.commit()
    log.info(f"Fear & Greed index updated ({len(rows)} days)")


# ---------------------------------------------------------------- exports
FILTERED_SQL = f"""
SELECT p.symbol FROM pairs p
JOIN ticker_snapshots t ON t.symbol = p.symbol
 AND t.snap_date = (SELECT MAX(snap_date) FROM ticker_snapshots)
WHERE p.status = 'TRADING' AND t.quote_volume_24h >= {MIN_VOL_USDT}
  AND p.token NOT IN ({",".join("'" + b + "'" for b in sorted(STABLE_BASES))})
"""


def _atomic_csv(path, header, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)


def export(con):
    os.makedirs(EXPORTS, exist_ok=True)
    con.execute("DROP VIEW IF EXISTS v_universe")
    con.execute(f"CREATE VIEW v_universe AS {FILTERED_SQL}")
    con.commit()
    mx = con.execute("SELECT MAX(date) FROM daily").fetchone()[0] or "2000-01-01"
    disp_from = (datetime.strptime(mx, "%Y-%m-%d") - timedelta(days=DISPLAY_DAYS - 1)).strftime("%Y-%m-%d")
    _atomic_csv(os.path.join(EXPORTS, "universe.csv"),
                ["symbol", "token", "status", "in_filtered_universe", "vol_24h_usdt",
                 "avg_vol_30d_usdt", "first_date", "last_date", "days", "gaps", "anomalies",
                 "delisted_on"],
                con.execute("""
        SELECT p.symbol, p.token, p.status,
               CASE WHEN p.symbol IN (SELECT symbol FROM v_universe) THEN 1 ELSE 0 END,
               ROUND((SELECT quote_volume_24h FROM ticker_snapshots t WHERE t.symbol=p.symbol
                      ORDER BY snap_date DESC LIMIT 1), 2),
               ROUND((SELECT AVG(volume_usdt) FROM (SELECT volume_usdt FROM daily d2
                      WHERE d2.symbol=p.symbol ORDER BY date DESC LIMIT 30)), 2),
               MIN(d.date), MAX(d.date), COUNT(d.date),
               (SELECT COUNT(*) FROM gaps g WHERE g.symbol=p.symbol),
               (SELECT COUNT(*) FROM anomalies a WHERE a.symbol=p.symbol),
               p.delisted_on
        FROM pairs p LEFT JOIN daily d ON d.symbol=p.symbol
        GROUP BY p.symbol ORDER BY 5 DESC"""))
    _atomic_csv(os.path.join(EXPORTS, "daily_long.csv"),
                ["symbol", "token", "date", "open", "high", "low", "close", "volume_base",
                 "volume_usdt", "trades", "taker_buy_usdt"],
                con.execute("""SELECT d.symbol, p.token, d.date, d.open, d.high, d.low, d.close,
                               d.volume_base, d.volume_usdt, d.trades, d.taker_buy_usdt
                               FROM daily d JOIN pairs p USING(symbol)
                               WHERE d.symbol IN (SELECT symbol FROM v_universe) AND d.date >= ?
                               ORDER BY d.symbol, d.date""", (disp_from,)))
    syms = [r[0] for r in con.execute("SELECT symbol FROM v_universe ORDER BY symbol")]
    dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM daily WHERE date >= ? ORDER BY date", (disp_from,))]
    px = {(s, d): c for s, d, c in con.execute(
        "SELECT symbol, date, close FROM daily WHERE symbol IN (SELECT symbol FROM v_universe) AND date >= ?", (disp_from,))}
    _atomic_csv(os.path.join(EXPORTS, "close_matrix.csv"), ["date"] + syms,
                ([d] + [px.get((s, d), "") for s in syms] for d in dates))


# ---------------------------------------------------------------- audit
def audit(con, n):
    con.execute("""CREATE TABLE IF NOT EXISTS audits(run_utc TEXT, daily_checked INTEGER, daily_mismatch INTEGER,
                   resampled_checked INTEGER, resampled_mismatch INTEGER, ok INTEGER)""")
    rows = con.execute("SELECT symbol, date, open, high, low, close, volume_base, volume_usdt, trades"
                       " FROM daily ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()
    bad = 0
    for sym, d, *stored in rows:
        if venues.is_external(sym):                  # audits compare against Binance itself
            continue
        k = api("/api/v3/klines", {"symbol": sym, "interval": "1d", "startTime": d2ms(d), "limit": 1})
        if not k or ms2d(int(k[0][0])) != d:
            log.error(f"AUDIT {sym} {d}: candle not returned by Binance")
            bad += 1
            continue
        live = [float(k[0][1]), float(k[0][2]), float(k[0][3]), float(k[0][4]),
                float(k[0][5]), float(k[0][7]), int(k[0][8])]
        if live != stored:
            log.error(f"AUDIT {sym} {d}: MISMATCH db={stored} live={live}")
            bad += 1
    log.info(f"AUDIT daily: {len(rows) - bad}/{len(rows)} candles match Binance exactly")
    rc = rb = 0
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='candles'").fetchone():
        rc, rb = audit_resample(con, max(5, n // 10))
    con.execute("INSERT INTO audits VALUES(?,?,?,?,?,?)",
                (utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"), len(rows), bad, rc, rb, int(bad + rb == 0)))
    con.commit()
    return bad + rb


def audit_resample(con, n_syms):
    """Compare our weekly/monthly candles (built from daily) with Binance's native 1w/1M."""
    syms = [r[0] for r in con.execute("SELECT symbol FROM pairs WHERE status='TRADING' AND backfilled=1"
                                      " ORDER BY RANDOM() LIMIT ?", (n_syms,))]
    checked = bad = 0
    for sym in syms:
        if venues.is_external(sym):
            continue
        for tf in ("1w", "1M"):
            ours = {r[0]: r[1:] for r in con.execute(
                "SELECT period_start, open, high, low, close, volume_base, volume_usdt, trades FROM candles"
                " WHERE symbol=? AND tf=? AND complete=1", (sym, tf))}
            if not ours:
                continue
            first = min(ours)
            for k in api("/api/v3/klines", {"symbol": sym, "interval": tf, "startTime": d2ms(first), "limit": 1000}):
                d = ms2d(int(k[0]))
                if d not in ours or int(k[6]) >= int(utc_now().timestamp() * 1000):
                    continue
                o = ours[d]
                live = (float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[7]), int(k[8]))
                checked += 1
                px_ok = o[:4] == live[:4]
                vol_ok = all(abs(a - b) <= 1e-9 * max(abs(b), 1) for a, b in zip(o[4:6], live[4:6])) and o[6] == live[6]
                if not (px_ok and vol_ok):
                    bad += 1
                    log.error(f"AUDIT {sym} {tf} {d}: MISMATCH ours={o} binance={live}")
    log.info(f"AUDIT weekly/monthly: {checked - bad}/{checked} candles match Binance native candles")
    return checked, bad


# ---------------------------------------------------------------- status
def write_status(con, result):
    lo, hi, n_rows = con.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM daily").fetchone()
    st = {
        "last_run_utc": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_run_ok": result.get("ok"),
        "synced_through_utc_day": meta_get(con, "synced_through"),
        "history_start": meta_get(con, "history_start"),
        "data_range": [lo, hi],
        "daily_rows": n_rows,
        "pairs_total_tracked": con.execute("SELECT COUNT(*) FROM pairs").fetchone()[0],
        "pairs_trading": con.execute("SELECT COUNT(*) FROM pairs WHERE status='TRADING'").fetchone()[0],
        "pairs_in_filtered_universe": con.execute("SELECT COUNT(*) FROM v_universe").fetchone()[0]
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='v_universe'").fetchone() else None,
        "filter": f"USDT spot, TRADING, 24h volume >= ${MIN_VOL_USDT:,}, stablecoin bases excluded",
        "binance_gaps_recorded": con.execute("SELECT COUNT(*) FROM gaps").fetchone()[0],
        "anomalies_recorded": con.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0],
        "last_failures": (result.get("failed") or []) + (result.get("incomplete") or []),
    }
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATUS)


def repair(con):
    """Idempotent data repairs. v11: pairs that were never seen trading must not carry a delisting date
    (it was stamped on the day tracking began), nor 'stopped trading' highlights / weekly entries."""
    con.execute("UPDATE pairs SET delisted_on=NULL WHERE last_seen_trading IS NULL AND delisted_on IS NOT NULL")
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='highlight_log'").fetchone():
        con.execute("""DELETE FROM highlight_log WHERE kind='stopped_trading'
                       AND symbol IN (SELECT symbol FROM pairs WHERE last_seen_trading IS NULL)""")
    con.commit()


def notify(title, msg):
    if platform.system() == "Darwin":
        # text is passed as arguments, never spliced into the script
        subprocess.run(["osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                        "-e", "end run", str(title)[:120], str(msg)[:240]], capture_output=True, timeout=20)


def check_token_expiry():
    """Warn at each publish when the GitHub token is within 10 days of expiring."""
    try:
        with open(os.path.join(ROOT, "site_status.json")) as f:
            exp = json.load(f).get("token_expires_utc")
        if not exp:
            return
        when = datetime.strptime(exp[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        days = (when - utc_now()).days
        if days <= 10:
            log.warning(f"GitHub token expires in {days} day(s) ({exp}); create a new one and save it to ~/BinanceData/.github_token")
            notify("Bee's Dashboard", f"GitHub token expires in {days} day(s). Renew it to keep the website updating.")
    except Exception:
        pass


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--audit", type=int, default=0)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--analytics-only", action="store_true")
    a = ap.parse_args()

    os.umask(0o077)                            # everything this job creates is private to this user
    os.makedirs(DATA, exist_ok=True)
    setup_logging(a.verbose)
    lock = open(os.path.join(DATA, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("Another sync is running; exiting.")
        return 0

    if platform.system() == "Darwin" and shutil.which("caffeinate"):
        # Keep the Mac from idle-sleeping until this process exits (lid close still sleeps).
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    con = sqlite3.connect(DB, timeout=60)
    con.executescript(SCHEMA)
    repair(con)
    problems = []
    if a.audit:
        return 1 if audit(con, a.audit) else 0
    if a.analytics_only:
        import analytics
        analytics.run(con)
        return 0
    try:
        refresh_supply(con, utc_now().strftime("%Y-%m-%d"))
    except Exception as e:
        log.warning(f"Supply refresh skipped: {e}")    # estimated market cap only; never blocks the sync
    try:
        refresh_fear_greed(con, utc_now().strftime("%Y-%m-%d"))
    except Exception as e:
        log.warning(f"Fear & Greed refresh skipped: {e}")   # context only; never blocks the sync
    try:
        result = run_sync(con, a.force)
    except Exception as e:
        log.exception(f"Sync failed: {e}")
        result = {"ok": False, "failed": [str(e)[:200]]}
    need_web = not all(os.path.exists(os.path.join(ROOT, "web_export", f)) for f in ("overview.json", "bot.json"))
    upgrade = meta_get(con, "analytics_version") != "4" or meta_get(con, "engine_version") != ENGINE_VERSION
    if not result.get("noop") or meta_get(con, "analytics_built_utc") is None or need_web or upgrade:
        write_status(con, result)              # so the export carries this run's health
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            con.execute("DROP VIEW IF EXISTS v_universe")
            con.execute(f"CREATE VIEW v_universe AS {FILTERED_SQL}")
            con.commit()
            import analytics
            analytics.run(con, verbose=False)
            log.info("Analytics rebuilt (candles, indicators, signals, latest)")
            try:                               # paper-trading bot: no orders, no keys, no network
                import bot
                if not bot.run(con, log.info):
                    notify("Bee's Dashboard", "Bot integrity check failed - see ~/BinanceData/logs/sync.log")
            except Exception as e:
                log.exception(f"Bot failed: {e}")
                notify("Bee's Dashboard", "Paper bot failed - see ~/BinanceData/logs/sync.log")
            import export_web
            export_web.run(con)
            log.info("Web export written (web_export/)")
        except Exception as e:
            log.exception(f"Analytics failed: {e}")
            result["ok"] = False
        try:
            export(con)
        except Exception as e:
            log.exception(f"Export failed: {e}")
            result["ok"] = False
        if result.get("ok"):
            try:
                last_audit = con.execute("SELECT MAX(run_utc) FROM audits").fetchone()[0] if con.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='audits'").fetchone() else None
                if not last_audit or last_audit < (utc_now() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"):
                    log.info("Weekly audit: re-downloading a random sample from Binance ...")
                    audit(con, 100)
                    import export_web
                    export_web.run(con)
            except Exception as e:
                log.exception(f"Audit failed: {e}")
        try:                                   # public website (GitHub Pages), if configured
            import publish_site
            publish_site.run(log.info)
            check_token_expiry()
        except Exception as e:
            log.error(f"Website publish failed: {e}")
            notify("Bee's Dashboard", "Website publish failed - see ~/BinanceData/logs/sync.log")
            problems.append("website publish failed")
        write_status(con, result)
        if result.get("ok"):                   # only once the rebuild actually succeeded
            meta_set(con, "engine_version", ENGINE_VERSION)
            con.commit()
        if not result.get("ok"):
            problems.append("sync incomplete, retrying hourly")
            today = utc_now().strftime("%Y-%m-%d")
            if meta_get(con, "last_fail_notice") != today:   # at most one alert per day
                notify("BinanceData", "Sync incomplete - will retry hourly. See ~/BinanceData/logs/sync.log")
                meta_set(con, "last_fail_notice", today)
                con.commit()
    light_publish = False                      # small files changed since the last publish (15-minute refresh)
    try:                                       # 4-hour candles: after the website, never blocks it
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import sync_4h
        r4 = sync_4h.run(con, api, utc_now().strftime("%Y-%m-%d"), utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"))
        log.info("4h candles: {complete}/{pairs} pairs complete, {pending} pending, +{added:,} rows ({rows:,} total), "
                 "{errors} errors, audit {audit_checked} days checked, {audit_bad} mismatched, {seconds}s".format(**r4))
        if r4["audit_bad"]:
            notify("Bee's Dashboard", "4h candles disagree with daily candles - see ~/BinanceData/logs/sync.log")
            problems.append("4h candles disagree with daily candles")
        if r4["added"] and os.path.isdir(os.path.join(ROOT, "web_export")):
            # fresh 4h candles: refresh the 4h charts and republish, so the site is never more than ~4h behind
            import h4_export
            uni = [s_ for (s_,) in con.execute("SELECT symbol FROM v_universe")]
            n4 = h4_export.write(con, uni, os.path.join(ROOT, "web_export"), int(time.time() * 1000))
            log.info(f"4h charts refreshed for {n4} pairs")
            if n4:
                light_publish = True
    except Exception as e:
        log.exception(f"4h candles skipped: {e}")
    try:                                       # big 4-hour moves on liquid coins (moves.json, alerts)
        import moves_4h
        mv = moves_4h.run(con, os.path.join(ROOT, "web_export"), int(time.time() * 1000))
        if mv:
            log.info(f"4h moves: {len(mv)} new ({', '.join(x['symbol'] + ' ' + x['kind'] for x in mv[:6])})")
            light_publish = True
    except Exception as e:
        log.warning(f"4h moves skipped: {e}")
    try:                                       # long-term breakouts: one-time 180-day history, a few coins per run
        import ltbo
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='latest'").fetchone():
            con.execute("CREATE TABLE IF NOT EXISTS ltbo_backfill(symbol TEXT PRIMARY KEY, done_utc TEXT)")
            if con.execute("SELECT COUNT(*) FROM ltbo_backfill").fetchone()[0] < 200:
                bf = ltbo.backfill(con)
                log.info("long-term breakouts history: {coins} coins scanned, {added} breakouts added, {left} left".format(**bf))
                if bf["added"] and os.path.isdir(os.path.join(ROOT, "web_export")):
                    import export_web
                    lt = ltbo.export(con, con.execute("SELECT MAX(date) FROM daily").fetchone()[0],
                                     signal_stats=export_web.table(con, "SELECT * FROM signal_stats"))
                    hp = os.path.join(ROOT, "web_export", "highlights.json")
                    with open(hp) as f:
                        hl_ = json.load(f)
                    hl_["ltbo"] = lt
                    with open(hp + ".tmp", "w") as f:
                        json.dump(hl_, f, separators=(",", ":"))
                    os.replace(hp + ".tmp", hp)
                    light_publish = True
    except Exception as e:
        log.warning(f"Long-term breakouts history skipped: {e}")
    try:                                       # experimental 4h paper bot (paper only: never places an order)
        import paper4h
        if paper4h.run(con, os.path.join(ROOT, "web_export"), int(time.time() * 1000), log=log.info):
            light_publish = True
    except Exception as e:
        log.warning(f"Paper 4h bot skipped: {e}")
    try:                                       # 1-hour candles: accumulating from a 1-year start, never blocks the site
        import sync_1h
        r1 = sync_1h.run(con, api, utc_now().strftime("%Y-%m-%d"), utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"))
        log.info("1h candles: {complete}/{pairs} pairs complete, {pending} pending, +{added:,} rows ({rows:,} total), "
                 "{errors} errors, audit {audit_checked} days checked, {audit_bad} mismatched, {seconds}s".format(**r1))
        if r1["audit_bad"]:
            problems.append("1h candles disagree with daily candles")
        if r1["added"] and os.path.isdir(os.path.join(ROOT, "web_export")):      # fresh hourly candles: 1h charts
            import h4_export
            uni = [s_ for (s_,) in con.execute("SELECT symbol FROM v_universe")]
            n1 = h4_export.write_h1(con, uni, os.path.join(ROOT, "web_export"), int(time.time() * 1000))
            log.info(f"1h charts refreshed for {n1} pairs")
            if n1:
                light_publish = True
    except Exception as e:
        log.exception(f"1h candles skipped: {e}")
    try:                                       # Bee's Shortlist: automatic picks, once per new daily close
        import auto_picks
        auto_picks.run(con, os.path.join(ROOT, "web_export"), log.info)
    except Exception as e:
        log.warning(f"Automatic picks skipped: {e}")
        problems.append("automatic picks skipped")
    try:                                       # Bee's Shortlist, public read-only copy: republish when it changes
        import public_sl
        if public_sl.sync(log.info):
            light_publish = True
    except Exception as e:
        log.warning(f"Public shortlist skipped: {e}")
    try:                                       # futures market data: funding, open interest, long/short (public)
        import derivatives
        def _fj(url):
            code, _, body = _fetch(url)
            if code != 200:
                raise HttpError(code, body)
            return json.loads(body)
        rd = derivatives.run(con, _fj)
        if rd["funding_rows"] or rd["oi_rows"] or rd["errors"]:
            log.info("derivatives: {perps} perpetuals, +{funding_rows:,} funding, +{oi_rows} OI, +{ls_rows} long/short rows, "
                     "{pending} pending, {errors} errors, {seconds}s".format(**rd) + (f" ({rd['first_errors'][0]})" if rd.get("first_errors") else ""))
    except Exception as e:
        log.warning(f"Derivatives data skipped: {e}")
    try:                                       # market-making research: hourly MEXC order-book snapshot (public)
        import mexc_probe
        for sym in mexc_probe.configured_symbols():
            mexc_probe.snapshot(sym)
    except Exception as e:
        log.warning(f"MEXC snapshot skipped: {e}")
    try:                                       # live prices for the website (one public ticker call)
        import prices
        if prices.run(con, api, os.path.join(ROOT, "web_export")):
            light_publish = True
    except Exception as e:
        log.warning(f"Live prices skipped: {e}")
    try:                                       # paper bots between closes: entries and stops as they stand now
        import bot_live
        if bot_live.run(con, os.path.join(ROOT, "web_export"), log=log.info):
            light_publish = True
    except Exception as e:
        log.warning(f"Bot live view skipped: {e}")
    try:                                       # MM-Bee backtest on the real ZIG hourly candles (once per new candle)
        import mm_bee_backtest
        mm_bee_backtest.refresh(con, log=log.info)
    except Exception as e:
        log.warning(f"MM-Bee backtest skipped: {e}")
    try:                                       # MM-Bee on other coins: refreshed once a day, in the background
        mp = os.path.join(ROOT, "data", "mm_bee_multi.json")
        if not os.path.exists(mp) or time.time() - os.path.getmtime(mp) > 20 * 3600:
            lock = os.path.join(ROOT, "data", ".mm_bee_multi.started")
            if not os.path.exists(lock) or time.time() - os.path.getmtime(lock) > 6 * 3600:
                open(lock, "w").close()
                subprocess.Popen(["nice", "-n", "15", sys.executable, os.path.join(ROOT, "research", "mm_bee_multi.py")],
                                 stdout=open(os.path.join(ROOT, "logs", "bee_study.txt"), "w"), stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
                log.info("MM-Bee multi-coin study started in the background")
    except Exception as e:
        log.warning(f"MM-Bee multi-coin study not started: {e}")
    try:                                       # MM-Bee tab: paper market-making results (paper files only)
        import mm_site
        if mm_site.export(os.path.join(ROOT, "web_export")):
            light_publish = True
    except Exception as e:
        log.warning(f"MM-Bee results skipped: {e}")
    if light_publish:
        try:
            import publish_site
            publish_site.run(log.info, light=True)
        except Exception as e:
            log.warning(f"Website refresh failed: {e}")
            problems.append("website refresh failed")
    try:                                       # live execution engine (only if ~/BinanceData/live.json enables it)
        import live_engine
        def _tg(text):
            import alerts
            cred = alerts.read_cred()
            if cred:
                alerts.send(*cred, text)
        lr = live_engine.run(log.info, _tg)
        if lr.get("mode") != "off":
            log.info("live engine [{mode}] bot {bot}: equity {equity} USDT, {open} open, {n} action(s)".format(n=len(lr.get("messages", [])), **lr))
    except Exception as e:
        log.exception(f"Live engine error: {e}")
        problems.append("live trading engine error - see the log")
    try:                                       # Telegram alerts (only if set up with alerts.py --setup)
        import alerts
        n = alerts.run(con, log.info, problems)
        if n:
            log.info(f"Telegram: {n} alert(s) sent")
    except Exception as e:
        log.warning(f"Telegram alerts skipped: {e}")
    con.close()
    try:                                       # daily database upkeep: integrity check, verified backup, WAL checkpoint
        if utc_now().hour >= 1:                # after the daily rebuild that follows 00:00 UTC
            import db_maint
            dp = db_maint.run(log.info)
            if dp:
                notify("Bee's Dashboard", "Database upkeep: " + "; ".join(dp)[:300])
    except Exception as e:
        log.warning(f"Database upkeep skipped: {e}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
