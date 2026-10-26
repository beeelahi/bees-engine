"""Head and shoulders: a synthetic top is found, then labelled near the neckline, broken and retesting in turn;
the inverse pattern mirrors it."""
import unittest

from tests import _paths  # noqa: F401
import hs


def path(points, n_between=12):
    """Piecewise-linear closes through (value) points, n_between candles per leg."""
    c = []
    for a, b in zip(points, points[1:]):
        c += [a + (b - a) * k / n_between for k in range(n_between)]
    c.append(points[-1])
    return c


def ohlc(c):
    o = [c[0]] + c[:-1]
    return o, [max(a, b) * 1.002 for a, b in zip(o, c)], [min(a, b) * 0.998 for a, b in zip(o, c)], c


TOP = [60, 80, 100, 85, 120, 85, 100, 90]          # rise, LS 100, neck 85, head 120, neck 85, RS 100, falling


class HsTest(unittest.TestCase):
    def find_top(self, c):
        o, h, l, c = ohlc(c)
        pats = [p for p in hs.find(h, l, c, 5) if p["kind"] == "top"]
        self.assertTrue(pats)
        return pats[0], o, h, l, c

    def test_top_near_break_retest(self):
        p, o, h, l, c = self.find_top(path(TOP))
        self.assertEqual(hs.state(p, o, h, l, c, len(c) - 1)["status"], "near_neckline")
        p, o, h, l, c = self.find_top(path(TOP) + [84, 82])
        self.assertEqual(hs.state(p, o, h, l, c, len(c) - 1)["status"], "broken")
        p, o, h, l, c = self.find_top(path(TOP) + [84, 80, 75, 72, 78, 82, 84])
        s = hs.state(p, o, h, l, c, len(c) - 1)
        self.assertEqual(s["status"], "retest")
        self.assertAlmostEqual(s["target"], s["neck"] - p["height"])

    def test_failure_drops_the_pattern(self):
        p, o, h, l, c = self.find_top(path(TOP) + [84, 80, 75, 72, 85, 98])     # back far above the neckline
        self.assertIsNone(hs.state(p, o, h, l, c, len(c) - 1))

    def test_inverse(self):
        inv = [200 - x for x in TOP]
        o, h, l, c = ohlc(path(inv) + [116, 118])
        pats = [p for p in hs.find(h, l, c, 5) if p["kind"] == "inverse"]
        self.assertTrue(pats)
        self.assertEqual(hs.state(pats[0], o, h, l, c, len(c) - 1)["status"], "broken")


if __name__ == "__main__":
    unittest.main()
