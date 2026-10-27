"""
Database upkeep for BinanceData (Python 3.9 stdlib only). Called at the end of every engine run; does real work at
most once per UTC day, so it never slows the 15-minute refreshes.

Once a day:
  * integrity: PRAGMA quick_check on every database (binance.db, mm.db, live.db, the paper market-making files);
    a full PRAGMA integrity_check on binance.db once a week;
  * backup: an online, consistent copy of every database (SQLite backup API, safe while readers and the market
    maker keep working) into data/backups/<name>-YYYY-MM-DD.db, verified with quick_check, keeping the last KEEP
    copies of each; skipped when the disk would drop below MIN_FREE_GB or below twice the database size;
  * the WAL file is checkpointed (TRUNCATE) so it cannot grow without bound;
  * PRAGMA optimize (refreshes the query planner's statistics where useful).
The result is written to db_maint.json (shown by the ops "status" command); any problem is returned so the engine
can raise an alert. Nothing here deletes data other than backups older than the newest KEEP.
"""
import glob
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")
BACKUPS = os.path.join(DATA, "backups")
STATUS = os.path.join(ROOT, "db_maint.json")
KEEP = 2
MIN_FREE_GB = 10
FULL_CHECK_WEEKDAY = 6          # Sunday


def _dbs(data=DATA):
    return sorted(p for p in glob.glob(os.path.join(data, "*.db")) if os.path.isfile(p) and not os.path.islink(p))


def _check(path, full=False):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    try:
        r = con.execute("PRAGMA integrity_check" if full else "PRAGMA quick_check").fetchall()
    finally:
        con.close()
    return [x[0] for x in r] == ["ok"], "; ".join(str(x[0]) for x in r[:3])


def _backup(path, day, backups=BACKUPS, keep=None, min_free_gb=None):
    keep = KEEP if keep is None else keep
    min_free_gb = MIN_FREE_GB if min_free_gb is None else min_free_gb
    os.makedirs(backups, mode=0o700, exist_ok=True)
    name = os.path.basename(path)[:-3]
    size = os.path.getsize(path)
    free = shutil.disk_usage(backups).free
    if free - size < max(min_free_gb * 1e9, size):
        return False, f"skipped: {free / 1e9:.1f} GB free, database {size / 1e9:.1f} GB"
    dst = os.path.join(backups, f"{name}-{day}.db")
    tmp = dst + ".part"
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    out = sqlite3.connect(tmp)
    try:
        src.backup(out, pages=4096, sleep=0.005)      # in steps: writers are never blocked for long
        out.execute("PRAGMA journal_mode=DELETE")       # a backup is one self-contained file
    finally:
        out.close()
        src.close()
    ok, msg = _check(tmp)
    if not ok:
        os.remove(tmp)
        return False, "backup failed its check: " + msg
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)
    olds = sorted(glob.glob(os.path.join(backups, f"{name}-????-??-??.db")))
    for p in olds[:-keep]:
        os.remove(p)
    return True, f"{os.path.basename(dst)} ({size / 1e9:.2f} GB)"


def run(log=print, now=None, data=DATA, backups=BACKUPS, status=STATUS, force=False):
    """Daily upkeep. Returns a list of problems (empty = all good, or nothing to do today)."""
    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    prev = {}
    if os.path.exists(status):
        try:
            with open(status) as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = {}
    if not force and prev.get("day") == day:
        return []
    t0, problems, rep = time.time(), [], {"day": day, "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "databases": {}}
    full = now.weekday() == FULL_CHECK_WEEKDAY
    for path in _dbs(data):
        name, r = os.path.basename(path), {}
        try:
            ok, msg = _check(path, full=full and name == "binance.db")
            r["check"] = ("integrity_check" if full and name == "binance.db" else "quick_check") + (": ok" if ok else ": " + msg)
            if not ok:
                problems.append(f"{name} failed its integrity check: {msg}")
            con = sqlite3.connect(path, timeout=60)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.execute("PRAGMA optimize")
            finally:
                con.close()
            if ok:
                bok, bmsg = _backup(path, day, backups)
                r["backup"] = bmsg
                if not bok:
                    problems.append(f"{name} backup: {bmsg}")
        except Exception as e:                    # never stop the engine; report instead
            r["error"] = str(e)[:200]
            problems.append(f"{name}: {str(e)[:120]}")
        rep["databases"][name] = r
    rep["seconds"] = round(time.time() - t0, 1)
    rep["problems"] = problems
    tmp = status + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rep, f, indent=1)
    os.replace(tmp, status)
    log(f"db upkeep: {len(rep['databases'])} databases checked and backed up in {rep['seconds']}s" + (f", problems: {problems}" if problems else ""))
    return problems


if __name__ == "__main__":
    import sys
    sys.exit(1 if run(force="--force" in sys.argv) else 0)
