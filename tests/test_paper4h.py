"""Fade 4h paper bot: shorts the next open after a 15%+ 4h candle, stops above the pump, covers after 18 candles;
starts from install time (no back-fill) and keeps a hash-chained ledger."""
import json
import os
import sqlite3
import tempfile
import unittest

from tests import _paths  # noqa: F401
import paper4h as B

H = 4 * 3600 * 1000
T0 = 1_760_000_000_000 // H * H


class Paper4hTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript("""CREATE TABLE candles_4h(symbol, open_ms, open, high, low, close, volume_usdt, close_ms);
            CREATE TABLE h4_state(symbol, backfilled); CREATE TABLE deriv_state(symbol, listed);
            INSERT INTO h4_state VALUES('ABCUSDT', 1), ('NOPERPUSDT', 1); INSERT INTO deriv_state VALUES('ABCUSDT', 1);""")
        self.d = tempfile.mkdtemp()
        self.paths = dict(state_path=os.path.join(self.d, "s.json"), ledger=os.path.join(self.d, "l.log"), log=lambda *a: None)

    def add(self, sym, k, o, h, l, c):
        self.con.execute("INSERT INTO candles_4h VALUES(?,?,?,?,?,?,?,?)", (sym, T0 + k * H, o, h, l, c, 1e6, T0 + (k + 1) * H - 1))

    def run_to(self, k):
        return B.run(self.con, self.d, T0 + (k + 1) * H, **self.paths)

    def test_time_exit_and_ledger(self):
        for k in range(40):
            self.add("ABCUSDT", k, 10, 10.1, 9.9, 10)
            self.add("NOPERPUSDT", k, 10, 10.1, 9.9, 10)
        self.run_to(39)                                         # installed: nothing traded from history
        self.add("ABCUSDT", 40, 10, 12.2, 10, 12)                # +20% pump
        self.add("NOPERPUSDT", 40, 10, 12.2, 10, 12)             # not shortable: ignored
        self.run_to(40)
        for k in range(41, 60):
            self.add("ABCUSDT", k, 12 if k == 41 else 11, 12.05 if k == 41 else 11.1, 10.8, 11)
            self.run_to(k)
        st = json.load(open(self.paths["state_path"]))
        self.assertEqual(len(st["closed"]), 1)
        x = st["closed"][0]
        self.assertEqual((x["symbol"], x["reason"], x["bars"]), ("ABCUSDT", "time", 18))
        self.assertGreater(x["pnl"], 0)
        self.assertAlmostEqual(st["equity"], 10_000 + x["pnl"])
        self.assertAlmostEqual(x["pnl_pct"], 100 * (1 - x["exit"] / x["entry"]))   # a short's % is the gain on its entry
        self.assertAlmostEqual(x["pnl"], x["qty"] * x["entry"] * x["pnl_pct"] / 100)   # and agrees with the dollar result
        self.assertEqual(B.verify(self.paths["ledger"])[1], 2)
        out = json.load(open(os.path.join(self.d, "paper4h.json")))
        self.assertEqual(out["n_closed"], 1)

    def test_stop(self):
        for k in range(40):
            self.add("ABCUSDT", k, 10, 10.1, 9.9, 10)
        self.run_to(39)
        self.add("ABCUSDT", 40, 10, 12.2, 10, 12)
        self.run_to(40)
        self.add("ABCUSDT", 41, 12, 12.1, 11.8, 12)
        self.run_to(41)
        self.add("ABCUSDT", 42, 12, 14, 12, 13.5)                # squeezes through the stop
        self.run_to(42)
        x = json.load(open(self.paths["state_path"]))["closed"][0]
        self.assertEqual(x["reason"], "stop")
        self.assertLess(x["r"], -0.9)


if __name__ == "__main__":
    unittest.main()
