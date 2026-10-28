"""MM-Bee backtest: candle fill rules, accounting, and the hourly refresh from the database."""
import json
import os
import random
import sqlite3
import tempfile
import unittest

from tests import _paths  # noqa: F401
import mm_bee_backtest as BT

H = 3600


def candles(start, hours, p0, drift=0.0, vol=0.01, seed=3):
    rnd, p, out = random.Random(seed), p0, []
    for i in range(hours):
        o = p
        p = max(0.001, p * (1 + rnd.gauss(drift, vol)))
        out.append((start + i * H, o, max(o, p) * 1.003, min(o, p) * 0.997, p))
    return out


class BacktestTest(unittest.TestCase):
    def setUp(self):
        self.t0 = BT.t_of(BT.START)

    def test_base_is_previous_day_close_and_ladder_placed(self):
        cs = candles(self.t0 - 2 * 86400, 2 * 24 + 5, 0.05, vol=0.0)       # flat: nothing fills
        r = BT.run(cs)
        self.assertAlmostEqual(r["base"], cs[2 * 24 - 25 + 24][4])              # close of the last candle of the day before
        self.assertEqual(r["rounds"], 0)
        self.assertEqual(r["fills"], 0)
        self.assertEqual(sum(1 for u in r["units"] if u[2] == "selling"), 20)

    def test_one_round_by_hand(self):
        """Close 0.05; next candles go to 0.0516 (sell 0.0515 fills), then to 0.0498 (buy at 0.0515*0.969 fills)."""
        cs = [(self.t0 - 86400 + i * H, 0.05, 0.0501, 0.0499, 0.05) for i in range(24)]
        t = self.t0
        cs += [(t, 0.05, 0.0516, 0.05, 0.0512), (t + H, 0.0512, 0.0512, 0.0498, 0.0499), (t + 2 * H, 0.0499, 0.05, 0.0497, 0.0499)]
        r = BT.run(cs)
        self.assertEqual(r["sells"], 1)
        self.assertEqual(r["rounds"], 1)
        self.assertAlmostEqual(r["profit_usdt"], 15000 * (0.0515 - 0.049903), delta=0.006)   # buy 0.0515 x 0.969, tick-rounded down
        self.assertAlmostEqual(r["value_usdt"], r["coins"] * r["end_price"] + r["usdt"], places=1)

    def test_no_sell_and_buy_back_inside_one_candle(self):
        cs = [(self.t0 - 86400 + i * H, 0.05, 0.0501, 0.0499, 0.05) for i in range(24)]
        cs += [(self.t0, 0.05, 0.0516, 0.0490, 0.0500)]                        # trades through both, one candle
        r = BT.run(cs)
        self.assertEqual(r["sells"], 1)
        self.assertEqual(r["buys"], 0)

    def test_random_walk_accounting(self):
        cs = candles(self.t0 - 2 * 86400, 20 * 24, 0.058, drift=-0.0006, vol=0.012)
        r = BT.run(cs)
        self.assertEqual(r["rounds"], r["buys"])
        self.assertAlmostEqual(r["coins"], r["start_coins"] - 15000 * (r["sells"] - r["buys"]), places=2)
        self.assertAlmostEqual(r["usdt"], r["start_usdt"] + r["proceeds_usdt"] - r["spent_usdt"], places=1)
        self.assertAlmostEqual(r["profit_usdt"], sum(d["profit_usdt"] for d in r["days"]), places=1)
        self.assertEqual(len(r["equity"]), r["candles"])
        json.dumps(r)

    def test_needs_the_day_before(self):
        with self.assertRaises(RuntimeError):
            BT.run(candles(self.t0, 48, 0.05))

    def test_refresh_from_database(self):
        d = tempfile.mkdtemp()
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE candles_1h(symbol TEXT, open_ms INTEGER, open REAL, high REAL, low REAL, close REAL)")
        for t, o, h, l, c in candles(self.t0 - 2 * 86400, 16 * 24, 0.05):
            con.execute("INSERT INTO candles_1h VALUES('ZIGUSDT',?,?,?,?,?)", (t * 1000, o, h, l, c))
        out = os.path.join(d, "bt.json")
        self.assertTrue(BT.refresh(con, out, log=lambda *a: None))
        self.assertFalse(BT.refresh(con, out, log=lambda *a: None))            # same last candle: not rerun
        res = json.load(open(out))
        self.assertIn("last_candle_utc", res)
        self.assertEqual(sorted({(r["strategy"], r["id"]) for r in res["runs"]}),
                         sorted((g, w) for g, _, _ in BT.STRATEGIES for w in ("5d", "15d")))     # 1y: not enough data here
        self.assertEqual(res["rounds"], res["runs"][0]["rounds"])
        for r in res["runs"]:
            self.assertAlmostEqual(r["change_usdt"], r["value_usdt"] - r["start_value_usdt"], places=1)


if __name__ == "__main__":
    unittest.main()
