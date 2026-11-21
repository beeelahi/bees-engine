"""A pair whose 4h/1h candles fail to download is named in the engine log (sync.log), not only counted.

The 4h and 1h modules used a logger ("binance") that had no handler, so the per-pair warning never reached
sync.log: the runs logged "1 errors" with no way to tell which pair, and the daily check saw 0 warnings.
"""
import logging
import sqlite3
import unittest
from datetime import datetime, timezone

from tests import _paths  # noqa: F401
import binance_sync
import sync_1h
import sync_4h

DAY = 86_400_000
T0 = 1_760_000_000_000 // DAY * DAY


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _failing_api(path, q):
    raise binance_sync.HttpError(400, '{"code":-1121,"msg":"Invalid symbol."}')


class SyncLoggingTest(unittest.TestCase):
    def _db(self):
        con = sqlite3.connect(":memory:")
        con.executescript("CREATE TABLE pairs(symbol, token, status); CREATE TABLE daily(symbol, date, open, high, low, close, volume_usdt);")
        con.execute("INSERT INTO pairs VALUES('ABCUSDT','ABC','TRADING')")
        days = [datetime.fromtimestamp((T0 + k * DAY) / 1000, tz=timezone.utc).strftime("%Y-%m-%d") for k in range(3)]
        for d in days:
            con.execute("INSERT INTO daily VALUES('ABCUSDT',?,100,101,99,100.5,240000)", (d,))
        return con, days[-1]

    def test_failed_pair_reaches_the_engine_log(self):
        for mod, days_attr, label in ((sync_4h, "H4_DAYS", "4h"), (sync_1h, "H1_DAYS", "1h")):
            with self.subTest(mod=label):
                self.assertIs(mod.log, binance_sync.log)
                cap = _Capture()
                binance_sync.log.addHandler(cap)
                self.addCleanup(binance_sync.log.removeHandler, cap)
                self.addCleanup(setattr, mod, days_attr, getattr(mod, days_attr))
                setattr(mod, days_attr, 2)
                con, today = self._db()
                r = mod.run(con, _failing_api, today, "s1", now_ms=T0 + 3 * DAY)
                self.assertEqual(r["errors"], 1)
                self.assertTrue(any(f"{label} ABCUSDT" in x and "Invalid symbol" in x for x in cap.lines), cap.lines)


if __name__ == "__main__":
    unittest.main()
