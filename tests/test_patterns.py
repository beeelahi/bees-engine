"""Double tops / bottoms and cup and handle (bin/patterns.py) on hand-made price paths."""
import unittest
from datetime import date, timedelta

from tests import _paths  # noqa: F401
import patterns as PT


def path(points):
    """Piecewise-linear closes through (bar, price) points; highs / lows 0.5% around the close."""
    c = []
    for (a, pa), (b, pb) in zip(points, points[1:]):
        for k in range(a, b):
            c.append(pa + (pb - pa) * (k - a) / (b - a))
    c.append(points[-1][1])
    return [x * 1.005 for x in c], [x * 0.995 for x in c], c


class DoubleTest(unittest.TestCase):
    cfg = PT.TF["1d"]

    def test_double_top_found_and_broken(self):
        h, l, c = path([(0, 60), (40, 100), (55, 86), (70, 100), (82, 84), (84, 84.5)])
        ps = [p for p in PT.scan(h, l, c, self.cfg) if p["type"] == "double" and p["kind"] == "top"]
        self.assertEqual(len(ps), 1)
        p = ps[0]
        self.assertAlmostEqual(p["line"], 86 * 0.995, delta=0.01)
        st = PT.state(p, h, l, c, len(c) - 1, self.cfg)
        self.assertEqual(st["status"], "broken")
        self.assertAlmostEqual(st["target"], p["line"] - p["height"], places=6)
        self.assertEqual([e[0] for e in PT.events(p, h, l, c, self.cfg)][-1], "broken")

    def test_double_top_killed_by_a_higher_high(self):
        h, l, c = path([(0, 60), (40, 100), (55, 86), (70, 100), (80, 95), (90, 108)])
        for p in PT.scan(h, l, c, self.cfg):
            if p["kind"] == "top" and p["z"] < 75:
                self.assertIsNone(PT.state(p, h, l, c, len(c) - 1, self.cfg))

    def test_double_bottom_near_the_neckline(self):
        h, l, c = path([(0, 160), (40, 100), (55, 115), (70, 100), (82, 113), (84, 113.5)])
        ps = [p for p in PT.scan(h, l, c, self.cfg) if p["kind"] == "bottom"]
        self.assertEqual(len(ps), 1)
        self.assertEqual(PT.state(ps[0], h, l, c, len(c) - 1, self.cfg)["status"], "near_neckline")

    def test_no_double_without_a_move_into_it(self):
        h, l, c = path([(0, 90), (40, 100), (55, 86), (70, 100), (82, 84)])
        self.assertFalse([p for p in PT.scan(h, l, c, self.cfg) if p["kind"] == "top"])


class CupTest(unittest.TestCase):
    cfg = PT.TF["1d"]

    def cup(self, end):
        # run-up 50 -> 100, a rounded cup down to 70 and back to 99 over 100 bars, a shallow handle, then `end`
        pts = [(0, 50), (40, 100), (60, 80), (75, 71), (90, 70), (105, 72), (120, 82), (140, 99), (150, 92), (158, 94)]
        return path(pts + end)

    def test_cup_and_handle_breakout(self):
        h, l, c = self.cup([(162, 102), (163, 102.5)])
        ps = [p for p in PT.scan(h, l, c, self.cfg) if p["type"] == "cup"]
        self.assertEqual(len(ps), 1)
        p = ps[0]
        st = PT.state(p, h, l, c, len(c) - 1, self.cfg)
        self.assertEqual(st["status"], "broken")
        self.assertAlmostEqual(st["target"], p["line"] + p["height"], places=6)
        self.assertGreater(st["stop"], p["inval"])                         # handle low, above the cup midpoint

    def test_handle_forming(self):
        h, l, c = self.cup([])
        ps = [p for p in PT.scan(h, l, c, self.cfg) if p["type"] == "cup"]
        self.assertEqual(PT.state(ps[0], h, l, c, len(c) - 1, self.cfg)["status"], "handle")

    def test_deep_handle_kills_it(self):
        h, l, c = self.cup([(170, 78)])
        for p in PT.scan(h, l, c, self.cfg):
            if p["type"] == "cup":
                self.assertIsNone(PT.state(p, h, l, c, len(c) - 1, self.cfg))

    def test_v_shape_is_not_a_cup(self):
        h, l, c = path([(0, 50), (40, 100), (90, 70), (140, 99), (150, 93), (158, 94)])
        self.assertFalse([p for p in PT.scan(h, l, c, self.cfg) if p["type"] == "cup"])


class WeeklyTest(unittest.TestCase):
    def test_complete_weeks_only(self):
        d0 = date(2026, 1, 5)                                              # a Monday
        dates = [(d0 + timedelta(days=k)).isoformat() for k in range(17)]  # 2 full weeks + 3 days
        x = [float(k + 1) for k in range(17)]
        wd, wo, wh, wl, wc, last = PT.weekly(dates, x, [v + 1 for v in x], [v - 1 for v in x], x)
        self.assertEqual(wd, ["2026-01-05", "2026-01-12"])
        self.assertEqual((wo[0], wh[0], wl[0], wc[0], last[0]), (1.0, 8.0, 0.0, 7.0, 6))


if __name__ == "__main__":
    unittest.main()
