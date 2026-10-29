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

    def test_needs_history_before_the_start(self):
        with self.assertRaises(RuntimeError):
            BT.run(candles(self.t0, 48, 0.05))

    def test_refresh_from_database(self):
        d = tempfile.mkdtemp()
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE candles_1h(symbol TEXT, open_ms INTEGER, open REAL, high REAL, low REAL, close REAL)")
        import time as _t
        now = int(_t.time()) - int(_t.time()) % 3600
        for t, o, h, l, c in candles(now - 40 * 86400, 40 * 24, 0.05):
            con.execute("INSERT INTO candles_1h VALUES('ZIGUSDT',?,?,?,?,?)", (t * 1000, o, h, l, c))
        out = os.path.join(d, "bt.json")
        self.assertTrue(BT.refresh(con, out, log=lambda *a: None))
        self.assertFalse(BT.refresh(con, out, log=lambda *a: None))            # same last candle: not rerun
        res = json.load(open(out))
        self.assertIn("last_candle_utc", res)
        self.assertEqual(sorted({(r["strategy"], r["id"]) for r in res["runs"]}),
                         sorted((g, w) for g, _, _ in BT.STRATEGIES for w in ("15d", "1m")))      # longer periods: not enough data here
        self.assertNotIn("rounds", res)                                     # no duplicated top-level run
        for r in res["runs"]:
            self.assertAlmostEqual(r["change_usdt"], r["value_usdt"] - r["start_value_usdt"], places=1)


class ChaseTest(unittest.TestCase):
    def test_chase_buys_back_in_a_rally(self):
        t0 = BT.t_of(BT.START)
        cs = [(t0 - 86400 + i * H, 0.05, 0.0501, 0.0499, 0.05) for i in range(24)]
        px = 0.05
        for i in range(40):                                                 # +1.5% an hour: through the first sells and on
            o, px = px, px * 1.015
            cs.append((t0 + i * H, o, px * 1.001, o * 0.999, px))
        base = {"vol_fast": 0, "btc_guard": 0}
        off = BT.run(cs, settings=base, taker_fee=0.001)
        on = BT.run(cs, settings=dict(base, chase=0.08), taker_fee=0.001)
        self.assertFalse([e for e in off["events"] if e[1] == "chase"])
        ch = [e for e in on["events"] if e[1] == "chase"]
        self.assertTrue(ch)
        self.assertGreater(on["coins"], off["coins"])                       # back in the coin
        self.assertGreater(on["vs_hold_usdt"], off["vs_hold_usdt"])         # and better off in this rally
        self.assertGreater(on["taker_fills"], 0)
        self.assertAlmostEqual(on["value_usdt"], on["coins"] * on["end_price"] + on["usdt"], places=1)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.t0 = BT.t_of(BT.START)

    def flat_day(self, t, px):
        return [(t + i * H, px, px * 1.001, px * 0.999, px) for i in range(24)]

    def test_btc_guard_pulls_sells_for_8_hours(self):
        z = self.flat_day(self.t0 - 86400, 0.05) + self.flat_day(self.t0, 0.05)
        b = self.flat_day(self.t0 - 86400, 100.0) + self.flat_day(self.t0, 100.0)
        k = 24 + 5                                                          # 05:00: BTC +3% over two hours
        b[k - 1] = (b[k - 1][0], 100.0, 101.6, 100.0, 101.5)
        b[k] = (b[k][0], 101.5, 103.1, 101.4, 103.0)
        for j in range(k + 1, len(b)):
            b[j] = (b[j][0], 103.0, 103.1, 102.9, 103.0)
        r = BT.run(z, btc=b, settings={"vol_fast": 0})
        g = [e for e in r["events"] if e[1] == "btc guard"]
        self.assertEqual(len(g), 1)
        self.assertIn("20 sells pulled for 8 h", g[0][7])
        self.assertEqual(sum(1 for u in r["units"] if u[2] == "selling"), 20)   # back on the book after 8 hours

    def test_fast_mode_on_a_10pct_move_and_back(self):
        z = self.flat_day(self.t0 - 86400, 0.05)
        z += [(self.t0 + i * H, 0.05, 0.05, 0.05, 0.05) for i in range(2)]
        z += [(self.t0 + 2 * H, 0.05, 0.0562, 0.05, 0.056)]               # +12% inside the 00:00-04:00 candle
        z += [(self.t0 + i * H, 0.056, 0.0561, 0.0559, 0.056) for i in range(3, 30)]
        r = BT.run(z, settings={"btc_guard": 0})
        ev = [e[1] for e in r["events"]]
        self.assertIn("fast mode", ev)
        self.assertIn("calm", ev)                                           # the next day / 4h candles are calm


if __name__ == "__main__":
    unittest.main()
