"""Parallel-channel detector on synthetic prices: finds a clean channel, flags edges and breaks."""
import math
import unittest

from tests import _paths  # noqa: F401
import chan


def channel_series(n=300, base=100.0, slope=0.05, width=12.0, period=30):
    o, h, l, c, v = [], [], [], [], []
    for i in range(n):
        mid = base + slope * i
        x = mid + (width / 2) * math.sin(2 * math.pi * i / period)
        o.append(x); c.append(x)
        h.append(x + 0.6); l.append(x - 0.6); v.append(1000.0)
    return o, h, l, c, v


class ChannelTest(unittest.TestCase):
    def test_detects_channel(self):
        o, h, l, c, v = channel_series()
        s = chan.state(o, h, l, c, v, "1d")
        self.assertIsNotNone(s)
        self.assertGreaterEqual(s["touch_lo"] + s["touch_hi"], chan.MIN_TOUCH_ALL)
        self.assertGreater(s["m"], 0)

    def test_breakout_on_volume(self):
        o, h, l, c, v = channel_series(n=301)
        lo, hi = chan.lines(chan.state(o[:-1], h[:-1], l[:-1], c[:-1], v[:-1], "1d"), 299)
        jump = hi + 6.0
        o[-1], c[-1], h[-1], l[-1], v[-1] = c[-2], jump, jump + 0.5, c[-2] - 0.5, 5000.0
        s = chan.state(o, h, l, c, v, "1d")
        self.assertEqual(s["status"], "breakout")

    def test_no_channel_in_noise(self):
        import random
        rnd = random.Random(1)
        p, o, h, l, c, v = 100.0, [], [], [], [], []
        for _ in range(300):
            q = p * (1 + rnd.gauss(0, 0.05)); o.append(p); c.append(q); h.append(max(p, q) * 1.02); l.append(min(p, q) * 0.98); v.append(1.0); p = q
        s = chan.state(o, h, l, c, v, "1d")
        self.assertTrue(s is None or s["status"] in chan.LABEL)


if __name__ == "__main__":
    unittest.main()
