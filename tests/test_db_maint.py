"""Daily database upkeep: integrity check, verified backups with rotation, once a day only, never raises."""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tests import _paths  # noqa: F401
import db_maint as M


class DbMaintTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.data, self.bk, self.st = os.path.join(self.d, "data"), os.path.join(self.d, "data", "backups"), os.path.join(self.d, "st.json")
        os.makedirs(self.data)
        con = sqlite3.connect(os.path.join(self.data, "binance.db"))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t(a INTEGER PRIMARY KEY, b TEXT)")
        con.executemany("INSERT INTO t VALUES(?,?)", [(i, "x" * 50) for i in range(5000)])
        con.commit()
        con.close()

    def run_at(self, day, **kw):
        old = M.MIN_FREE_GB
        M.MIN_FREE_GB = 0
        try:
            return M.run(log=lambda *a: None, now=day, data=self.data, backups=self.bk, status=self.st, **kw)
        finally:
            M.MIN_FREE_GB = old

    def test_backup_rotation_and_once_a_day(self):
        d0 = datetime(2026, 10, 26, 2, tzinfo=timezone.utc)
        for k in range(4):
            self.assertEqual(self.run_at(d0 + timedelta(days=k)), [])
        files = sorted(os.listdir(self.bk))
        self.assertEqual(files, ["binance-2026-10-28.db", "binance-2026-10-29.db"])     # KEEP = 2
        con = sqlite3.connect(os.path.join(self.bk, files[-1]))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM t").fetchone()[0], 5000)
        con.close()
        st = json.load(open(self.st))
        self.assertIn("ok", st["databases"]["binance.db"]["check"])
        before = os.path.getmtime(os.path.join(self.bk, files[-1]))
        self.run_at(d0 + timedelta(days=3, hours=5))                                       # same day again: nothing
        self.assertEqual(os.path.getmtime(os.path.join(self.bk, files[-1])), before)

    def test_corrupt_database_is_reported_not_raised(self):
        with open(os.path.join(self.data, "broken.db"), "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\xff" * 4096)
        probs = self.run_at(datetime(2026, 10, 26, 2, tzinfo=timezone.utc))
        self.assertTrue(any("broken.db" in p for p in probs))
        self.assertTrue(os.path.exists(os.path.join(self.bk, "binance-2026-10-26.db")))   # the good one still backed up

    def test_low_disk_skips_backup(self):
        old = M.MIN_FREE_GB
        M.MIN_FREE_GB = 10 ** 9
        try:
            probs = M.run(log=lambda *a: None, now=datetime(2026, 10, 26, 2, tzinfo=timezone.utc), data=self.data, backups=self.bk, status=self.st)
        finally:
            M.MIN_FREE_GB = old
        self.assertTrue(any("skipped" in p for p in probs))


if __name__ == "__main__":
    unittest.main()
