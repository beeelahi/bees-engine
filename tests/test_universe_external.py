"""The universe filter: Binance pairs need the 24h volume floor; pairs tracked from another venue on purpose (ZIG on
MEXC) stay in while they trade, even on a quiet day or without a snapshot, and leave when they stop trading."""
import sqlite3
import unittest

from tests import _paths  # noqa: F401
import binance_sync as B


class UniverseExternalTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(B.SCHEMA)
        for sym, st in (("BTCUSDT", "TRADING"), ("QUIETUSDT", "TRADING"), ("ZIGUSDT", "TRADING"), ("OLDUSDT", "BREAK")):
            self.con.execute("INSERT INTO pairs(symbol, token, status, first_seen, backfilled, first_candle) VALUES(?,?,?,?,1,?)",
                             (sym, sym[:-4], st, "2026-10-01", "2020-01-01"))

    def snap(self, day, sym, vol):
        self.con.execute("INSERT INTO ticker_snapshots VALUES(?,?,?,?,?)", (day, sym, 1.0, vol, "t"))

    def uni(self):
        return {s for (s,) in self.con.execute(B.FILTERED_SQL)}

    def test_quiet_day_keeps_external_pair(self):
        for sym, vol in (("BTCUSDT", 5e8), ("QUIETUSDT", 5e4), ("ZIGUSDT", 5e4), ("OLDUSDT", 5e6)):
            self.snap("2026-11-01", sym, vol)
        self.assertEqual(self.uni(), {"BTCUSDT", "ZIGUSDT"})          # QUIET under the floor, OLD not trading

    def test_missing_snapshot_keeps_external_pair(self):
        self.snap("2026-10-31", "ZIGUSDT", 3e6)                        # yesterday only
        self.snap("2026-11-01", "BTCUSDT", 5e8)
        self.assertEqual(self.uni(), {"BTCUSDT", "ZIGUSDT"})

    def test_external_pair_leaves_when_not_trading(self):
        self.con.execute("UPDATE pairs SET status='BREAK' WHERE symbol='ZIGUSDT'")
        self.snap("2026-11-01", "ZIGUSDT", 3e6)
        self.assertNotIn("ZIGUSDT", self.uni())

    def test_binance_pair_without_snapshot_stays_out(self):
        self.snap("2026-11-01", "BTCUSDT", 5e8)
        self.assertNotIn("QUIETUSDT", self.uni())


if __name__ == "__main__":
    unittest.main()
