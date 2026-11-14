"""Indicator math (bin/indicators.py) against hand-worked values and slow, independent reference versions."""
import math
import random
import unittest

from tests import _paths  # noqa: F401
import indicators as I


def bars(n=400, seed=11):
    rnd, p, h, l, c, v = random.Random(seed), 100.0, [], [], [], []
    for _ in range(n):
        o = p
        p = max(1.0, p * (1 + rnd.gauss(0, 0.02)))
        h.append(max(o, p) * (1 + abs(rnd.gauss(0, 0.005))))
        l.append(min(o, p) * (1 - abs(rnd.gauss(0, 0.005))))
        c.append(p)
        v.append(rnd.uniform(100, 1000))
    return h, l, c, v


def close(a, b, tol=1e-9):
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


class Base(unittest.TestCase):
    def same(self, got, want, tol=1e-9):
        self.assertEqual(len(got), len(want))
        for i, (a, b) in enumerate(zip(got, want)):
            self.assertTrue(close(a, b, tol), f"index {i}: {a} != {b}")


class MovingAverages(Base):
    def test_sma_by_hand(self):
        self.same(I.sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_sma_matches_naive(self):
        _, _, c, _ = bars()
        self.same(I.sma(c, 20), [None if i < 19 else sum(c[i - 19:i + 1]) / 20 for i in range(len(c))])

    def test_sma_opt_skips_none_prefix(self):
        self.same(I.sma_opt([None, None, 1, 2, 3, 4], 2), [None, None, None, 1.5, 2.5, 3.5])

    def test_ema_by_hand(self):
        # n=3: alpha 0.5, seeded with mean(1,2,3)=2; then 0.5*4+0.5*2=3, 0.5*5+0.5*3=4
        self.same(I.ema([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])
        self.same(I.ema([1, 2], 3), [None, None])

    def test_rma_by_hand(self):
        # n=2: seed mean(2,4)=3; (3*1+6)/2=4.5; (4.5+0)/2=2.25
        self.same(I.rma([2, 4, 6, 0], 2), [None, 3.0, 4.5, 2.25])

    def test_ema_warm_means_history_no_longer_matters(self):
        _, _, c, _ = bars(600)
        n, w = 20, I.ema_warm(20)
        full, late = I.ema(c, n), I.ema(c[100:], n)
        self.assertLess(abs(full[100 + w] - late[w]) / full[100 + w], I.WARM_TOL)


class Momentum(Base):
    def test_rsi_extremes(self):
        up, down = list(range(1, 40)), list(range(40, 1, -1))
        self.assertEqual(I.rsi(up, 14)[-1], 100.0)
        self.assertEqual(I.rsi(down, 14)[-1], 0.0)
        self.assertEqual(I.rsi(up[:14], 14), [None] * 14)

    def test_rsi_matches_wilder_reference(self):
        _, _, c, _ = bars()
        n, out = 14, [None] * len(c)
        d = [c[i] - c[i - 1] for i in range(1, len(c))]
        ag = sum(max(x, 0) for x in d[:n]) / n
        al = sum(max(-x, 0) for x in d[:n]) / n
        out[n] = 100 - 100 / (1 + ag / al)
        for i in range(n + 1, len(c)):
            x = d[i - 1]
            ag, al = (ag * (n - 1) + max(x, 0)) / n, (al * (n - 1) + max(-x, 0)) / n
            out[i] = 100 - 100 / (1 + ag / al)
        self.same(I.rsi(c, n), out)
        self.assertTrue(all(0 <= x <= 100 for x in out if x is not None))

    def test_macd_line_is_fast_minus_slow_and_hist_is_line_minus_signal(self):
        _, _, c, _ = bars()
        line, sig, hist = I.macd(c, 12, 26, 9)
        ef, es = I.ema(c, 12), I.ema(c, 26)
        self.assertIsNone(line[24])
        self.assertTrue(close(line[25], ef[25] - es[25]))
        self.assertIsNone(sig[32])
        self.assertTrue(close(sig[33], sum(line[25:34]) / 9))
        for i in range(33, len(c)):
            self.assertTrue(close(hist[i], line[i] - sig[i]))

    def test_roc(self):
        self.same(I.roc([100, 110, 0, 121], 1), [None, 10.0, -100.0, None])

    def test_stoch_bounds_and_by_hand(self):
        h, l, c, _ = bars()
        k, d = I.stoch(h, l, c)
        self.assertTrue(all(-1e-9 <= x <= 100 + 1e-9 for x in k + d if x is not None))
        raw = [(c[i] - min(l[i - 13:i + 1])) / (max(h[i - 13:i + 1]) - min(l[i - 13:i + 1])) * 100 for i in range(13, 16)]
        self.assertTrue(close(k[15], sum(raw) / 3))

    def test_stoch_rsi_bounds(self):
        _, _, c, _ = bars()
        k, d = I.stoch_rsi(c)
        vals = [x for x in k + d if x is not None]
        self.assertTrue(vals and all(-1e-9 <= x <= 100 + 1e-9 for x in vals))   # running-sum SMA leaves ~1e-14 float dust


class Volatility(Base):
    def test_true_range_uses_previous_close(self):
        self.same(I.true_range([10, 12, 9], [8, 11, 7], [9, 11.5, 8]), [2, 3, 4.5])

    def test_atr_seed_and_smoothing(self):
        h, l, c, _ = bars()
        tr, a = I.true_range(h, l, c), I.atr(h, l, c, 14)
        self.assertIsNone(a[13])
        self.assertTrue(close(a[14], sum(tr[1:15]) / 14))
        self.assertTrue(close(a[15], (a[14] * 13 + tr[15]) / 14))

    def test_bbands_population_sd(self):
        mid, up, lo = I.bbands([2, 4, 4, 4, 5, 5, 7, 9], 8, 2.0)
        self.assertEqual((mid[-1], up[-1], lo[-1]), (5.0, 9.0, 1.0))   # mean 5, population sd 2

    def test_realized_vol_and_donchian_shapes(self):
        h, l, c, _ = bars()
        hi, lo = I.donchian(h, l, 20)                                  # the previous 20 bars, not the current one
        self.assertEqual(len(hi), len(c))
        self.assertIsNone(hi[19])
        self.assertTrue(close(hi[50], max(h[30:50])) and close(lo[50], min(l[30:50])))
        rv = I.realized_vol(c, 30, 365)
        self.assertEqual(len(rv), len(c))
        self.assertTrue(all(x > 0 for x in rv if x is not None))


class Trend(Base):
    def test_adx_bounds(self):
        h, l, c, _ = bars()
        out = I.adx(h, l, c, 14)
        vals = [x for x in (out[0] if isinstance(out, tuple) else out) if x is not None]
        self.assertTrue(vals and all(0 <= x <= 100 for x in vals))

    def test_supertrend_runs_full_length(self):
        h, l, c, _ = bars()
        out = I.supertrend(h, l, c)
        for series in (out if isinstance(out, tuple) else (out,)):
            self.assertEqual(len(series), len(c))


class Volume(Base):
    def test_obv_by_hand(self):
        self.same(I.obv([1, 2, 2, 1, 3], [5, 10, 20, 30, 40]), [0, 10, 10, -20, 20])

    def test_mfi_and_cmf_bounds(self):
        h, l, c, v = bars()
        self.assertTrue(all(0 <= x <= 100 for x in I.mfi(h, l, c, v) if x is not None))
        self.assertTrue(all(-1 <= x <= 1 for x in I.cmf(h, l, c, v) if x is not None))

    def test_rvol_uses_previous_bars_only(self):
        base, rv, z = I.rvol([10, 10, 10, 40], 3)
        self.assertEqual((base[3], rv[3], z[3]), (10.0, 4.0, None))     # flat window: no z-score
        base, rv, z = I.rvol([10, 20, 30, 40], 3)
        self.assertTrue(close(z[3], (40 - 20) / math.sqrt(200 / 3)))


class Position(Base):
    def test_rolling_max_min_match_brute_force(self):
        _, _, c, _ = bars()
        for n in (1, 5, 20):
            self.same(I.rolling_max(c, n), [None if i < n - 1 else max(c[i - n + 1:i + 1]) for i in range(len(c))])
            self.same(I.rolling_min(c, n), [None if i < n - 1 else min(c[i - n + 1:i + 1]) for i in range(len(c))])
            self.same(I.rolling_max(c, n, include_current=False),
                      [None if i < n else max(c[i - n:i]) for i in range(len(c))])

    def test_rolling_pct_rank_matches_slow_version(self):
        _, _, c, _ = bars()
        xs = [None, None] + c[:200] + [None] + c[200:]
        self.same(I.rolling_pct_rank(xs, 30), I.pct_rank(xs, 30))

    def test_running_high_and_bars_since(self):
        hi, since = I.running_high([3, 5, 4, 5, 2])
        self.assertEqual((hi, since), ([3, 5, 5, 5, 5], [0, 0, 1, 0, 1]))

    def test_streak(self):
        self.assertEqual(I.streak([1, 2, 3, 3, 2, 1, 2]), [0, 1, 2, 0, -1, -2, 1])


if __name__ == "__main__":
    unittest.main()
