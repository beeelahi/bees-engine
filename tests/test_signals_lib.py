"""Signal library (bin/signals_lib.py): crosses, swing pivots, divergences, market structure, and the promise in
its header that every event is stamped on the bar where it becomes knowable (no look-ahead)."""
import random
import unittest
from datetime import date, timedelta

from tests import _paths  # noqa: F401
import analytics as A
import signals_lib as SL


def candles(n, seed):
    """Random daily candles in the analytics layout [day, o, h, l, c, vb, vq, trades, tb, n_days, complete]."""
    rnd, p, out, d0 = random.Random(seed), 100.0, [], date(2022, 1, 1)
    for i in range(n):
        o = p
        p = max(1.0, p * (1 + rnd.gauss(0, 0.03)))
        h = max(o, p) * (1 + abs(rnd.gauss(0, 0.01)))
        lo = min(o, p) * (1 - abs(rnd.gauss(0, 0.01)))
        vb = rnd.uniform(100, 1000) * (5 if rnd.random() < 0.03 else 1)
        out.append([(d0 + timedelta(i)).isoformat(), o, h, lo, p, vb, vb * p, 100, vb * rnd.uniform(0.3, 0.7), 1, 1])
    return out


class Crosses(unittest.TestCase):
    def test_up_down_and_none(self):
        a, b = [1, 2, 3, 2, 2], [2, 2, 2, 2, 3]
        self.assertIsNone(SL.crosses(a, b, 1))           # touching from below is not a cross yet
        self.assertEqual(SL.crosses(a, b, 2), "bullish")  # 2 <= 2 then 3 > 2
        self.assertEqual(SL.crosses(a, b, 3), None)       # 2 == 2: not below, so no cross down
        self.assertEqual(SL.crosses(a, b, 4), "bearish")  # 2 >= 2 then 2 < 3
        self.assertIsNone(SL.crosses([None, 3], [2, 2], 1))


class Pivots(unittest.TestCase):
    def test_strict_left_inclusive_right(self):
        # a swing high at 5 needs 5 strictly lower bars before it and 5 bars after that are not higher
        src = [1, 2, 3, 4, 5, 9, 8, 7, 9, 2, 1, 0]
        self.assertEqual(SL.pivots(src, high=True), [5])  # 9 at index 8 ties: still a pivot at 5
        self.assertEqual(SL.pivots([9, 1, 2, 3, 4, 9, 8, 7, 6, 5, 4], high=True), [])   # left tie: not a pivot

    def test_low_and_gaps(self):
        src = [9, 8, 7, 6, 5, 1, 2, 3, 4, 5, 6]
        self.assertEqual(SL.pivots(src, high=False), [5])
        src[7] = None
        self.assertEqual(SL.pivots(src, high=False), [])   # a missing value in the window: no pivot

    def test_needs_right_bars(self):
        # the last swing is not a pivot until PIVOT_R bars have closed after it
        src = [1, 2, 3, 4, 5, 9, 8, 7, 6, 5]   # 4 bars after the swing
        self.assertEqual(SL.pivots(src, high=True), [])
        self.assertEqual(SL.pivots(src + [4], high=True), [5])


def two_lows(osc_vals, low_vals, gap=12):
    """Series with two clear swing lows (osc and price) `gap` bars apart, on a flat background."""
    n = 5 + gap + 5 + 2
    osc, low = [50.0] * n, [100.0] * n
    for k, (o, lo) in enumerate(zip(osc_vals, low_vals)):
        p = 5 + k * gap
        osc[p], low[p] = o, lo
    return osc, low, [200.0] * n, (5, 5 + gap)


class Divergences(unittest.TestCase):
    def test_regular_bullish(self):
        osc, low, high, (p1, p2) = two_lows((20, 30), (90, 80))     # price lower low, oscillator higher low
        self.assertEqual(SL.divergences(osc, low, high, 0), [(p2 + SL.PIVOT_R, "regular", "bullish")])

    def test_hidden_bullish(self):
        osc, low, high, (p1, p2) = two_lows((30, 20), (80, 90))     # price higher low, oscillator lower low
        self.assertEqual(SL.divergences(osc, low, high, 0), [(p2 + SL.PIVOT_R, "hidden", "bullish")])

    def test_cold_first_pivot_is_ignored(self):
        osc, low, high, (p1, p2) = two_lows((20, 30), (90, 80))
        self.assertEqual(SL.divergences(osc, low, high, p1 + 1), [])

    def test_too_far_apart(self):
        osc, low, high, _ = two_lows((20, 30), (90, 80), gap=SL.DIV_RANGE[1] + 5)
        self.assertEqual(SL.divergences(osc, low, high, 0), [])


class Structure(unittest.TestCase):
    def zigzag(self, swings, leg=6):
        """Straight legs between the given swing prices; highs/lows a hair either side of the close."""
        c = []
        for a, b in zip(swings, swings[1:]):
            c += [a + (b - a) * k / leg for k in range(leg)]
        c.append(swings[-1])
        return [x + 0.01 for x in c], [x - 0.01 for x in c], c

    def test_uptrend_then_break(self):
        h, lo, c = self.zigzag([10, 20, 15, 25, 18, 30, 22, 35])
        ev, state = SL.structure(h, lo, c)
        names = [e[1] for e in ev]
        self.assertIn("structure_up", names)
        self.assertNotIn("structure_down", names)
        self.assertEqual(state[-1], 1)
        self.assertIn("bos_up", names)
        # each swing high is broken at most once
        self.assertEqual(len([e for e in ev if e[1] == "bos_up"]), len(set(e[0] for e in ev if e[1] == "bos_up")))

    def test_downtrend(self):
        h, lo, c = self.zigzag([40, 30, 35, 25, 32, 20, 28, 15])
        ev, state = SL.structure(h, lo, c)
        self.assertIn("structure_down", [e[1] for e in ev])
        self.assertIn("bos_down", [e[1] for e in ev])
        self.assertEqual(state[-1], -1)


class NoLookAhead(unittest.TestCase):
    """The whole pipeline (indicators + every signal) on the first k candles must give exactly the events the full
    history stamps on bars before k: nothing learned later may change or add an earlier event."""

    def test_events_on_a_prefix_match_the_full_history(self):
        for seed in (0, 1):
            cs = candles(420, seed)
            btc = {c[0]: c[4] for c in candles(420, seed + 50)}
            warm = lambda col: A.warm_from(col, "1d")  # noqa: E731
            full = SL.events("X", "1d", cs, A.compute(cs, "1d", btc), warm)
            self.assertGreater(len(full), 100)
            idx = {c[0]: i for i, c in enumerate(cs)}
            for k in range(60, 420, 3):            # every 3rd bar, so rare events land near a cut
                pre = SL.events("X", "1d", cs[:k], A.compute(cs[:k], "1d", btc), warm)
                self.assertEqual(sorted(pre), sorted(e for e in full if idx[e[2]] < k), f"seed {seed}, first {k} bars")

    def test_every_event_name_is_in_the_catalogue(self):
        cs = candles(420, 3)
        ev = SL.events("X", "1d", cs, A.compute(cs, "1d", {}), lambda col: A.warm_from(col, "1d"))
        self.assertEqual({e[3] for e in ev} - set(SL.CATALOG), set())


if __name__ == "__main__":
    unittest.main()
