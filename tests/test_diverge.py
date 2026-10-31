"""Daily divergences: a textbook bullish divergence (lower price low, higher RSI low) is found on the day its swing is
confirmed and not before; a plain downtrend (lower lows with lower RSI) is not a divergence; the bearish mirror."""
import unittest

from tests import _paths  # noqa: F401
import diverge as DV


def series(path):
    """Candles from a list of closes (open = previous close, wick 0.5%)."""
    o = [path[0]] + path[:-1]
    h = [max(a, b) * 1.005 for a, b in zip(o, path)]
    l_ = [min(a, b) * 0.995 for a, b in zip(o, path)]
    return o, h, l_, list(path)


def leg(a, b, n):
    return [a + (b - a) * (k + 1) / n for k in range(n)]


BULL = [100.0] * 40 + leg(100, 60, 20) + leg(60, 75, 10) + leg(75, 58, 12) + leg(58, 70, 12)   # crash, bounce, slow drift to a slightly lower low
def zig(a, b, n):
    """A leg from a to b with a small counter-move every other candle (real candles are never a straight line)."""
    base = leg(a, b, n)
    return [x + (2.0 * abs(b - a) / n if k % 2 else 0.0) for k, x in enumerate(base)]


TREND = [100.0] * 40 + zig(100, 80, 16) + leg(80, 86, 6) + leg(86, 50, 8) + leg(50, 56, 10)   # the second leg falls faster: momentum agrees, no divergence


class Divergence(unittest.TestCase):
    def test_bullish_found_when_confirmed(self):
        o, h, l_, c = series(BULL)
        ev = [e for e in DV.events(o, h, l_, c) if e["side"] == "bull"]
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertLess(e["px_j"], e["px_p"])                     # lower low in price
        self.assertGreater(e["rsi_j"], e["rsi_p"])                 # higher low in RSI
        self.assertLess(e["rsi_p"], DV.RSI_ZONE_BULL)
        self.assertEqual(e["i"], e["j"] + DV.P)                    # known only when the swing is confirmed
        self.assertEqual(c.index(min(c[60:])), e["j"])
        self.assertIn(e["kind"], ("triple", "rsi"))
        self.assertGreater(e["swing"], e["px_j"])
        early = DV.events(o[:e["i"]], h[:e["i"]], l_[:e["i"]], c[:e["i"]])
        self.assertFalse([x for x in early if x["side"] == "bull"])  # no look-ahead

    def test_plain_downtrend_is_not_a_divergence(self):
        o, h, l_, c = series(TREND)
        self.assertFalse([e for e in DV.events(o, h, l_, c) if e["side"] == "bull"])

    def test_bearish_mirror(self):
        o, h, l_, c = series([200.0 - x for x in BULL])
        ev = [e for e in DV.events(o, h, l_, c) if e["side"] == "bear"]
        self.assertEqual(len(ev), 1)
        self.assertGreater(ev[0]["px_j"], ev[0]["px_p"])
        self.assertLess(ev[0]["rsi_j"], ev[0]["rsi_p"])

    def test_indicators(self):
        c = [float(x) for x in range(1, 60)]
        self.assertEqual(DV.rsi(c)[-1], 100.0)                     # only gains
        line, sig, hist = DV.macd(c)
        self.assertIsNone(line[10])
        self.assertGreater(line[-1], 0)


if __name__ == "__main__":
    unittest.main()
