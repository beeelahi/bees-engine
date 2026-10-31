"""bStocks (Binance tokenized US stocks, ticker + B, listed from 11 Jun 2026) are kept out of the crypto universe,
are not reported as 'left the universe', and older crypto coins ending in B stay crypto."""
import sqlite3
import unittest

from tests import _paths  # noqa: F401
import binance_sync as B


class StocksTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(B.SCHEMA)
        for sym, fc in (("BNBUSDT", "2017-11-06"), ("ARBUSDT", "2023-03-23"), ("NVDABUSDT", "2026-06-11"), ("MUBUSDT", "2026-06-11"),
                        ("HYPEUSDT", "2026-09-24"), ("SPYBUSDT", "2026-07-07"), ("NEWBUSDT", None)):
            self.con.execute("INSERT INTO pairs(symbol, token, status, first_seen, backfilled, first_candle) VALUES(?,?,?,?,1,?)",
                             (sym, sym[:-4], "TRADING", "2026-10-01", fc))
            self.con.execute("INSERT INTO ticker_snapshots VALUES(?,?,?,?,?)", ("2026-10-30", sym, 1.0, 5e6, "t"))

    def test_rule(self):
        self.assertTrue(B.is_stock("NVDAB", "2026-06-11"))
        self.assertTrue(B.is_stock("MUB", "2026-06-11"))
        self.assertFalse(B.is_stock("BNB", "2017-11-06"))
        self.assertFalse(B.is_stock("HYPE", "2026-09-24"))
        self.assertFalse(B.is_stock("BB", "2026-07-01"))          # too short to be ticker + B
        self.assertFalse(B.is_stock("NEWB", None))               # not backfilled yet: never guessed from the engine's own first_seen

    def test_universe_and_helpers(self):
        self.assertEqual(B.stock_tokens(self.con), {"NVDAB", "MUB", "SPYB"})
        self.assertEqual(B.stock_symbols(self.con, trading_only=True), {"NVDABUSDT", "MUBUSDT", "SPYBUSDT"})
        uni = {s for (s,) in self.con.execute(B.FILTERED_SQL)}
        self.assertEqual(uni, {"BNBUSDT", "ARBUSDT", "HYPEUSDT", "NEWBUSDT"})
        self.assertEqual(B.stock_tokens(sqlite3.connect(":memory:")), set())     # no pairs table: nothing, no error


if __name__ == "__main__":
    unittest.main()
