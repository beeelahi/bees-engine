"""Indicator math: each function in bin/indicators.py is checked against a slow, independent version written here
from the textbook definition (no shared code), on seeded random candles, plus the edge cases screens rely on
(warm-up start, flat windows, excluded current bar, None gaps). Standard library only."""
import math
import random
import statistics
import unittest

from tests import _paths  # noqa: F401
import indicators as ind


def candles(n=300, seed=7, start=100.0):
    """Random-walk OHLCV with some repeated closes (ties matter for streaks, ranks and money flow)."""
    r = random.Random(seed)
    c, h, l, v = [], [], [], []
    p = start
    for i in range(n):
        p = p if i % 17 == 0 else max(p * math.exp(r.gauss(0, 0.02)), 0.01)
        hi, lo = p * (1 + abs(r.gauss(0, 0.01))), p * (1 - abs(r.gauss(0, 0.01)))
        c.append(p)
        h.append(hi)
        l.append(lo)
        v.append(r.uniform(10, 1000))
    return h, l, c, v


def wilder(xs, n, first):
    """Wilder smoothing written out: mean of xs[first:first+n], then (prev*(n-1)+x)/n."""
    out = [None] * len(xs)
    v = statistics.fmean(xs[first:first + n])
    out[first + n - 1] = v
    for i in range(first + n, len(xs)):
        v = (v * (n - 1) + xs[i]) / n
        out[i] = v
    return out


class IndicatorTest(unittest.TestCase):
    def assertSeries(self, got, want, places=9):
        self.assertEqual(len(got), len(want))
        for i, (a, b) in enumerate(zip(got, want)):
            if b is None:
                self.assertIsNone(a, f"index {i}")
            else:
                self.assertIsNotNone(a, f"index {i}")
                self.assertAlmostEqual(a, b, places=places, msg=f"index {i}")

    def setUp(self):
        self.h, self.l, self.c, self.v = candles()

    # ------------------------------------------------------------ moving averages
    def test_sma_and_none_prefix(self):
        for n in (1, 5, 20):
            want = [None] * (n - 1) + [statistics.fmean(self.c[i - n + 1:i + 1]) for i in range(n - 1, len(self.c))]
            self.assertSeries(ind.sma(self.c, n), want)
        xs = [None] * 4 + self.c[:30]
        self.assertSeries(ind.sma_opt(xs, 5), [None] * 4 + ind.sma(self.c[:30], 5))
        self.assertEqual(ind.sma([1.0, 2.0], 3), [None, None])

    def test_ema_seed_and_recursion(self):
        n, a = 10, 2 / 11
        e = ind.ema(self.c, n)
        self.assertTrue(all(x is None for x in e[:n - 1]))
        self.assertAlmostEqual(e[n - 1], statistics.fmean(self.c[:n]))
        for i in range(n, len(self.c)):
            self.assertAlmostEqual(e[i], a * self.c[i] + (1 - a) * e[i - 1])
        self.assertEqual(ind.ema(self.c[:5], 10), [None] * 5)
        self.assertSeries(ind.ema([3.0] * 50, 12)[11:], [3.0] * 39)

    def test_warm_up_makes_the_start_irrelevant(self):
        """The *_warm indices promise that, from there on, starting the history later changes the value by
        less than WARM_TOL of the starting gap. Checked by cutting the history at several points."""
        cases = [
            (lambda c: ind.ema(c, 20), ind.ema_warm(20)),
            (lambda c: ind.rsi(c, 14), ind.rsi_warm(14)),
            (lambda c: ind.macd(c, 12, 26, 9)[0], ind.macd_warm(12, 26, 9)),
        ]
        h, l, c, _ = candles(900, seed=11)
        cases.append((lambda c_: ind.atr([x * 1.01 for x in c_], [x * 0.99 for x in c_], c_, 14), ind.atr_warm(14)))
        for fn, warm in cases:
            full = fn(c)
            for cut in (0, 37, 150):
                part = fn(c[cut:])
                i = cut + warm
                scale = max(abs(full[i]), 1.0)
                self.assertLess(abs(full[i] - part[warm]) / scale, 0.01, f"warm {warm} cut {cut}")

    # ------------------------------------------------------------ momentum
    def test_rsi_matches_wilder_definition(self):
        n = 14
        c = self.c
        g = [0.0] + [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
        lo = [0.0] + [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
        ag, al = wilder(g, n, 1), wilder(lo, n, 1)
        want = [None] * n + [100 - 100 / (1 + ag[i] / al[i]) for i in range(n, len(c))]
        self.assertSeries(ind.rsi(c, n), want)
        up = [float(i) for i in range(1, 40)]
        self.assertTrue(all(x == 100.0 for x in ind.rsi(up, 14)[14:]))
        self.assertTrue(all(x == 0.0 for x in ind.rsi(up[::-1], 14)[14:]))
        self.assertEqual(ind.rsi(up[:14], 14), [None] * 14)

    def test_macd_is_fast_minus_slow_with_ema_signal(self):
        line, sig, hist = ind.macd(self.c, 12, 26, 9)
        ef, es = ind.ema(self.c, 12), ind.ema(self.c, 26)
        self.assertTrue(all(x is None for x in line[:25]))
        for i in range(25, len(self.c)):
            self.assertAlmostEqual(line[i], ef[i] - es[i])
        self.assertTrue(all(x is None for x in sig[:33]))
        self.assertAlmostEqual(sig[33], statistics.fmean(line[25:34]))
        for i in range(34, len(self.c)):
            self.assertAlmostEqual(sig[i], 0.2 * line[i] + 0.8 * sig[i - 1])
            self.assertAlmostEqual(hist[i], line[i] - sig[i])

    def test_roc_and_streak(self):
        self.assertSeries(ind.roc([100.0, 110.0, 99.0, 0.0, 5.0], 1)[:3], [None, 10.0, -10.0])
        self.assertIsNone(ind.roc([1.0, 0.0, 5.0], 1)[2])          # no division by a zero close
        self.assertEqual(ind.streak([1, 2, 3, 3, 2, 1, 2]), [0, 1, 2, 0, -1, -2, 1])

    def test_stochastics(self):
        k, ks, ds = 14, 3, 3
        raw = [None] * len(self.c)
        for i in range(k - 1, len(self.c)):
            hh, ll = max(self.h[i - k + 1:i + 1]), min(self.l[i - k + 1:i + 1])
            raw[i] = 0.0 if hh == ll else 100 * (self.c[i] - ll) / (hh - ll)
        kk = [None] * len(raw)
        for i in range(k - 1 + ks - 1, len(raw)):
            kk[i] = statistics.fmean(raw[i - ks + 1:i + 1])
        dd = [None] * len(raw)
        for i in range(k - 1 + ks - 1 + ds - 1, len(raw)):
            dd[i] = statistics.fmean(kk[i - ds + 1:i + 1])
        gk, gd = ind.stoch(self.h, self.l, self.c, k, ks, ds)
        self.assertSeries(gk, kk)
        self.assertSeries(gd, dd)
        sk, sd = ind.stoch_rsi(self.c)
        self.assertTrue(all(-1e-9 <= x <= 100 + 1e-9 for x in sk if x is not None))   # running-sum rounding only
        self.assertEqual(next(i for i, x in enumerate(sk) if x is not None), 14 + 13 + 2)

    # ------------------------------------------------------------ volatility
    def test_true_range_and_atr(self):
        h, l, c = self.h, self.l, self.c
        tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
        self.assertSeries(ind.true_range(h, l, c), tr)
        self.assertSeries(ind.atr(h, l, c, 14), wilder(tr, 14, 1))      # TA-Lib: seeded from TR[1..14]
        self.assertTrue(all(x > 0 for x in ind.atr(h, l, c)[14:]))

    def test_bollinger_uses_population_stdev(self):
        mid, up, lo = ind.bbands(self.c, 20, 2.0)
        for i in range(19, len(self.c)):
            w = self.c[i - 19:i + 1]
            m, sd = statistics.fmean(w), statistics.pstdev(w)
            self.assertAlmostEqual(mid[i], m)
            self.assertAlmostEqual(up[i], m + 2 * sd)
            self.assertAlmostEqual(lo[i], m - 2 * sd)
        self.assertIsNone(up[18])

    def test_realized_vol_is_annualised_sample_stdev(self):
        n, ppy = 20, 365
        got = ind.realized_vol(self.c, n, ppy)
        lr = [math.log(self.c[i] / self.c[i - 1]) for i in range(1, len(self.c))]
        for i in range(n, len(self.c)):
            self.assertAlmostEqual(got[i], statistics.stdev(lr[i - n:i]) * math.sqrt(ppy) * 100)
        self.assertTrue(all(x is None for x in got[:n]))

    # ------------------------------------------------------------ trend
    def test_adx_shape_and_range(self):
        pdi, mdi, adx = ind.adx(self.h, self.l, self.c, 14)
        self.assertTrue(all(x is None for x in pdi[:14]))
        self.assertTrue(all(x is None for x in adx[:27]))
        self.assertIsNotNone(adx[27])
        for a, b, x in zip(pdi[27:], mdi[27:], adx[27:]):
            self.assertTrue(0 <= a <= 100 and 0 <= b <= 100 and 0 <= x <= 100)
        up = [float(i) for i in range(1, 80)]                    # a clean uptrend: +DI wins, ADX near 100
        p, m, a = ind.adx([x + 0.5 for x in up], [x - 0.5 for x in up], up, 14)
        self.assertGreater(p[-1], m[-1])
        self.assertGreater(a[-1], 90)
        self.assertEqual(ind.adx(up[:28], up[:28], up[:28], 14)[2], [None] * 28)

    def test_supertrend_flips_with_the_trend(self):
        up = [100 + i for i in range(60)]
        down = [159 - i * 2 for i in range(60)]
        c = up + down
        h, lo = [x + 1 for x in c], [x - 1 for x in c]
        line, d = ind.supertrend(h, lo, c, 10, 3.0)
        self.assertEqual(d[59], 1)
        self.assertLess(line[59], c[59])
        self.assertEqual(d[-1], -1)
        self.assertGreater(line[-1], c[-1])

    def test_ichimoku_is_displaced(self):
        h, l = self.h, self.l
        tk, kj, sa, sb = ind.ichimoku(h, l)
        i = 120
        self.assertAlmostEqual(tk[i], (max(h[i - 8:i + 1]) + min(l[i - 8:i + 1])) / 2)
        self.assertAlmostEqual(kj[i], (max(h[i - 25:i + 1]) + min(l[i - 25:i + 1])) / 2)
        self.assertAlmostEqual(sa[i], (tk[i - 26] + kj[i - 26]) / 2)
        j = i - 26
        self.assertAlmostEqual(sb[i], (max(h[j - 51:j + 1]) + min(l[j - 51:j + 1])) / 2)

    # ------------------------------------------------------------ volume
    def test_money_flow_and_cmf_and_obv(self):
        h, l, c, v = self.h, self.l, self.c, self.v
        n = 14
        tp = [(a + b + x) / 3 for a, b, x in zip(h, l, c)]
        got = ind.mfi(h, l, c, v, n)
        for i in range(n, len(c)):
            pos = sum(tp[j] * v[j] for j in range(i - n + 1, i + 1) if tp[j] > tp[j - 1])
            neg = sum(tp[j] * v[j] for j in range(i - n + 1, i + 1) if tp[j] < tp[j - 1])
            self.assertAlmostEqual(got[i], 100 * pos / (pos + neg))
        m = 20
        got = ind.cmf(h, l, c, v, m)
        for i in range(m - 1, len(c)):
            w = range(i - m + 1, i + 1)
            mfv = sum(((c[j] - l[j]) - (h[j] - c[j])) / (h[j] - l[j]) * v[j] for j in w)
            self.assertAlmostEqual(got[i], mfv / sum(v[j] for j in w))
        self.assertEqual(ind.obv([1, 2, 2, 1, 3], [5, 7, 9, 4, 6]), [0.0, 7.0, 7.0, 3.0, 9.0])

    def test_relative_volume_uses_previous_bars_only(self):
        n = 20
        base, rv, z = ind.rvol(self.v, n)
        for i in range(n, len(self.v)):
            w = self.v[i - n:i]
            mu = statistics.fmean(w)
            self.assertAlmostEqual(base[i], mu)
            self.assertAlmostEqual(rv[i], self.v[i] / mu)
            self.assertAlmostEqual(z[i], (self.v[i] - mu) / statistics.pstdev(w))
        b, r, zz = ind.rvol([0.0] * 30, 20)
        self.assertEqual((r[25], zz[25]), (None, None))           # no division by a silent market

    # ------------------------------------------------------------ position and rolling windows
    def test_rolling_windows_match_brute_force(self):
        r = random.Random(3)
        xs = [float(r.randint(0, 9)) for _ in range(250)]          # many ties
        for n in (1, 2, 7, 30):
            self.assertSeries(ind.rolling_max(xs, n), [None] * (n - 1) + [max(xs[i - n + 1:i + 1]) for i in range(n - 1, len(xs))])
            self.assertSeries(ind.rolling_min(xs, n), [None] * (n - 1) + [min(xs[i - n + 1:i + 1]) for i in range(n - 1, len(xs))])
            prev = [None] * n + [max(xs[i - n:i]) for i in range(n, len(xs))]
            self.assertSeries(ind.rolling_max(xs, n, include_current=False), prev)

    def test_donchian_excludes_the_current_bar(self):
        h = [1.0, 2.0, 3.0, 10.0]
        hi, lo = ind.donchian(h, [x - 1 for x in h], 3)
        self.assertEqual(hi, [None, None, None, 3.0])               # bar 3 at 10 is a breakout above 3
        self.assertEqual(lo, [None, None, None, 0.0])
        self.assertEqual(ind.window_high_low(h, h, 2), ([None, 2.0, 3.0, 10.0], [None, 1.0, 2.0, 3.0]))

    def test_percentile_ranks_agree(self):
        """pct_rank (plain) and rolling_pct_rank (fast, sorted window) must give the same numbers, gaps included."""
        r = random.Random(5)
        xs = [None if r.random() < 0.05 else float(r.randint(0, 20)) for _ in range(400)]
        for n in (5, 30):
            self.assertSeries(ind.rolling_pct_rank(xs, n), ind.pct_rank(xs, n))
        self.assertEqual(ind.pct_rank([1.0, 2.0, 2.0, 0.5], 4)[3], 25.0)

    def test_running_high(self):
        hi, since = ind.running_high([1.0, 3.0, 2.0, 3.0, 1.0])
        self.assertEqual(hi, [1.0, 3.0, 3.0, 3.0, 3.0])
        self.assertEqual(since, [0, 0, 1, 0, 1])                   # an equal high counts as a new high


if __name__ == "__main__":
    unittest.main()
