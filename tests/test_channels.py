"""Parallel-channel detector: finds clean channels on synthetic prices, ignores noise, and labels the statuses."""
import math
import random
import unittest

from tests import _paths  # noqa: F401
import chan


def channel_series(n=400, base=100.0, slope=0.05, width=20.0, period=40):
    o, h, l, c = [], [], [], []
    for i in range(n):
        mid = base + slope * i
        x = mid + (width / 2) * math.sin(2 * math.pi * i / period)
        y = mid + (width / 2) * math.sin(2 * math.pi * (i + 1) / period)
        o.append(x)
        c.append(y)
        h.append(max(x, y) + 0.3)
        l.append(min(x, y) - 0.3)
    return o, h, l, c


class DetectTest(unittest.TestCase):
    def test_finds_the_channel(self):
        o, h, l, c = channel_series()
        i = len(c) - 1
        F = chan.find(h, l, c, i)
        self.assertTrue(F)
        f = F[0]
        self.assertGreaterEqual(f["touch_lo"] + f["touch_hi"], 5)
        self.assertAlmostEqual(f["m"], 0.05, delta=0.01)
        lo, hi = chan.lines(f, i)
        self.assertAlmostEqual(hi - lo, 20.6, delta=2.0)
        self.assertGreaterEqual(f["bars"], 300)                 # starts at its first touch, near the beginning

    def test_extended_lines_are_linear(self):
        o, h, l, c = channel_series()
        f = chan.find(h, l, c, len(c) - 1)[0]
        a, b = chan.lines(f, 0), chan.lines(f, 500)
        self.assertAlmostEqual((b[0] - a[0]) / 500, f["m"])

    def test_noise_rarely_qualifies(self):
        rnd = random.Random(7)
        found = 0
        for _ in range(5):
            p, o, h, l, c = 100.0, [], [], [], []
            for _ in range(400):
                q = p * (1 + rnd.gauss(0, 0.04))
                o.append(p); c.append(q); h.append(max(p, q) * 1.01); l.append(min(p, q) * 0.99)
                p = q
            for f in chan.find(h, l, c, 399):
                found += 1
                s = chan.state(f, o, h, l, c, 399)
                self.assertTrue(s is None or s["status"] in chan.LABEL)
        self.assertLessEqual(found, 15)


def flat(n, price):
    return [price] * n


class StatusTest(unittest.TestCase):
    """state() on a hand-made horizontal channel: bottom 100, top 120, midline 110."""
    F = {"m": 0.0, "lo_b": 100.0, "hi_b": 120.0, "w": 20.0, "first": 0, "bars": 200}

    def run_state(self, closes):
        c = flat(200, 105.0) + closes
        o = [c[0]] + c[:-1]
        h = [max(a, b) + 0.2 for a, b in zip(o, c)]
        l = [min(a, b) - 0.2 for a, b in zip(o, c)]
        return chan.state(self.F, o, h, l, c, len(c) - 1)

    def test_breakout_and_breakdown(self):
        self.assertEqual(self.run_state([105, 106, 107, 108, 109, 122])["status"], "breakout")
        self.assertEqual(self.run_state([105, 104, 103, 102, 101, 98])["status"], "breakdown")

    def test_midline_reclaim_and_loss(self):
        s = self.run_state([105, 106, 107, 108, 109, 112])
        self.assertEqual((s["status"], s["level"]), ("mid_reclaim", 110.0))
        c = flat(200, 115.0) + [115, 114, 113, 112, 111, 108]
        o = [c[0]] + c[:-1]
        s = chan.state(self.F, o, [x + 0.2 for x in c], [x - 0.2 for x in c], c, len(c) - 1)
        self.assertEqual(s["status"], "mid_loss")

    def test_edges_and_channel_end(self):
        self.assertEqual(self.run_state([105, 104, 103, 102, 102, 101.5])["status"], "near_bottom")
        self.assertIsNone(self.run_state([105, 104, 103, 102, 101, 85]))      # more than half a width outside: over


if __name__ == "__main__":
    unittest.main()
