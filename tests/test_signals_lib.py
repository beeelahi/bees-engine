"""Signal library (bin/signals_lib.py), which writes every row of the `signals` table behind the Signals views:
crossovers follow Pine ta.crossover / ta.crossunder; pivots are strict on the left and confirmed `right` bars later;
textbook divergences and a break of structure are found once, on the right bar; on a long synthetic series no event
depends on candles after it (cutting the history never changes an earlier event) and every event name has a label."""
import random
import unittest
from datetime import date, timedelta

from tests import _paths  # noqa: F401
import analytics as A
import signals_lib as SL


def candles(n=700, seed=7):
    """A seeded random walk of daily candles (analytics.resample layout) with a steady rally at the end, plus BTC closes."""
    rnd = random.Random(seed)
    p, rows, btc = 100.0, [], {}
    for k in range(n):
        o = p
        drift = 0.02 if k >= n - 60 else 0.0                       # the last 60 days climb to new highs
        p = max(1.0, p * (1 + drift + rnd.gauss(0, 0.035)))
        hi, lo = max(o, p) * (1 + abs(rnd.gauss(0, 0.01))), min(o, p) * (1 - abs(rnd.gauss(0, 0.01)))
        vq = 1e6 * (1 + abs(rnd.gauss(0, 1))) * (4 if rnd.random() < 0.03 else 1)
        d = (date(2023, 1, 1) + timedelta(k)).isoformat()
        rows.append([d, o, hi, lo, p, vq / p, vq, 1000, vq * rnd.uniform(0.3, 0.7), 1, 1])
        btc[d] = 20000 * (1 + 0.0005 * k)
    return rows, btc


def run(cs, btc):
    X = A.compute(cs, "1d", btc)
    return SL.events("TESTUSDT", "1d", cs, X, lambda col: A.warm_from(col, "1d"))


class Crosses(unittest.TestCase):
    def test_pine_crossover_and_crossunder(self):
        a, b = [1, 2, 3, 2, 1, 0], [2, 2, 2, 2, 2, 2]
        self.assertIsNone(SL.crosses(a, b, 1))                     # touching is not crossing
        self.assertEqual(SL.crosses(a, b, 2), "bullish")           # from <= to >
        self.assertIsNone(SL.crosses(a, b, 3))                     # back to touching
        self.assertEqual(SL.crosses(a, b, 4), "bearish")           # from >= to <
        self.assertIsNone(SL.crosses(a, b, 5))                     # already below

    def test_missing_values_give_nothing(self):
        self.assertIsNone(SL.crosses([None, 3], [2, 2], 1))
        self.assertIsNone(SL.crosses([1, 3], [2, None], 1))


class Pivots(unittest.TestCase):
    def test_strict_left_inclusive_right(self):
        x = [1, 2, 3, 2, 1, 1, 1]
        self.assertEqual(SL.pivots(x, 2, 2, high=True), [2])
        self.assertEqual(SL.pivots([1, 2, 3, 3, 1, 0, 0], 2, 2, high=True), [2])    # the first of two equal tops
        self.assertEqual(SL.pivots([3, 2, 1, 2, 3, 3, 3], 2, 2, high=False), [2])

    def test_needs_right_bars_and_skips_gaps(self):
        self.assertEqual(SL.pivots([1, 2, 3, 2], 2, 2, high=True), [])              # not confirmed yet
        self.assertEqual(SL.pivots([1, None, 3, 2, 1], 2, 2, high=True), [])        # a gap in the window


def zigzag(points, step=6):
    out = []
    for a, b in zip(points, points[1:]):
        out += [a + (b - a) * k / step for k in range(step)]
    return out + [points[-1]]


class Divergences(unittest.TestCase):
    def test_regular_bullish(self):
        # price: lower low at the second swing; oscillator: higher low
        low = zigzag([50, 40, 50, 35, 50, 50])
        osc = zigzag([50, 20, 50, 30, 50, 50])
        ev = SL.divergences(osc, low, [x + 1 for x in low], osc_warm=0)
        self.assertEqual(ev, [(18 + SL.PIVOT_R, "regular", "bullish")])           # stamped on the confirmation bar

    def test_hidden_bearish(self):
        # price: lower high; oscillator: higher high
        high = zigzag([50, 70, 50, 65, 50, 50])
        osc = zigzag([50, 70, 50, 80, 50, 50])
        ev = SL.divergences(osc, [x - 1 for x in high], high, osc_warm=0)
        self.assertEqual(ev, [(18 + SL.PIVOT_R, "hidden", "bearish")])

    def test_first_swing_inside_warm_up_is_ignored(self):
        low = zigzag([50, 40, 50, 35, 50, 50])
        osc = zigzag([50, 20, 50, 30, 50, 50])
        self.assertEqual(SL.divergences(osc, low, [x + 1 for x in low], osc_warm=7), [])


class Structure(unittest.TestCase):
    def test_uptrend_and_one_break_of_structure(self):
        close = zigzag([10, 20, 15, 25, 18, 30, 30])        # higher highs and higher lows
        high, low = [c + 0.5 for c in close], [c - 0.5 for c in close]
        ev, state = SL.structure(high, low, close)
        names = [e[1] for e in ev]
        self.assertEqual(names.count("structure_up"), 1)
        self.assertNotIn("structure_down", names)
        self.assertEqual(state[-1], 1)
        bos = [e[0] for e in ev if e[1] == "bos_up"]
        self.assertTrue(bos)
        swing_highs = SL.pivots(high, high=True)
        self.assertLessEqual(len(bos), len(swing_highs))    # at most one break per swing high, not every close above it
        for i in bos:
            self.assertGreater(i, swing_highs[0] + SL.PIVOT_R - 1)   # only after a swing high is confirmed


class Events(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.btc = candles()
        cls.full = run(cls.rows, cls.btc)

    def test_every_event_has_a_label(self):
        names = {e[3] for e in self.full}
        self.assertGreater(len(names), 40)                   # the series exercises most of the catalogue
        self.assertEqual(names - set(SL.CATALOG), set())
        self.assertIn("new_ath", names)

    def test_no_look_ahead(self):
        for cut in (250, 400, 555, 699):
            earlier = [e for e in self.full if e[2] < self.rows[cut][0]]
            self.assertEqual(run(self.rows[:cut], self.btc), earlier, f"cut at bar {cut}")

    def test_row_shape(self):
        for sym, tf, ps, name, direction, warm, prov, close, confirmed, rv in self.full:
            self.assertIn(direction, ("bullish", "bearish", "neutral"))
            self.assertIn(warm, (0, 1))
            self.assertEqual(prov, 0)                        # every daily candle here is complete
            self.assertIn(confirmed, (0, 1))
            if confirmed:
                self.assertGreaterEqual(rv, SL.CONFIRM_RVOL)
            if name in SL.ONE_WAY:
                self.assertEqual(direction, SL.ONE_WAY[name])


if __name__ == "__main__":
    unittest.main()
