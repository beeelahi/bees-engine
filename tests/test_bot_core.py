"""Bot core (bin/bot_core.py), the code shared by the backtest and the live paper trader: calendar maths, excluded
tokens, series breaks, the no-look-ahead rolling windows, agreement with the indicator library, and the features and
score that the Top-50 and the bots read. Tests only: nothing in the trading logic changes."""
import math
import random
import unittest
from datetime import date, timedelta

from tests import _paths  # noqa: F401
import bot_core as B
import indicators as I


def walk(n, seed=7, start=100.0):
    rnd = random.Random(seed)
    c, x = [], start
    for _ in range(n):
        x *= math.exp(rnd.gauss(0, 0.03))
        c.append(x)
    o = [c[0]] + c[:-1]
    h = [max(a, b) * (1 + rnd.random() * 0.02) for a, b in zip(o, c)]
    l_ = [min(a, b) * (1 - rnd.random() * 0.02) for a, b in zip(o, c)]
    v = [1e6 * (1 + rnd.random()) for _ in range(n)]
    return o, h, l_, c, v


def close(a, b, tol=1e-9):
    return a is None and b is None or (a is not None and b is not None and abs(a - b) <= tol * max(1.0, abs(b)))


class CalendarTest(unittest.TestCase):
    def test_day_num_counts_real_days(self):
        d0 = date(1999, 12, 25)
        base = B.day_num(d0.isoformat())
        for k in range(0, 12000, 37):                       # across leap years, 2000 (leap) and month ends
            d = d0 + timedelta(days=k)
            self.assertEqual(B.day_num(d.isoformat()) - base, k, d.isoformat())

    def test_day_num_leap_days(self):
        self.assertEqual(B.day_num("2024-03-01") - B.day_num("2024-02-28"), 2)
        self.assertEqual(B.day_num("2023-03-01") - B.day_num("2023-02-28"), 1)
        self.assertEqual(B.day_num("2100-03-01") - B.day_num("2100-02-28"), 1)    # not a leap year


class ExcludedTokensTest(unittest.TestCase):
    def test_stables_wrapped_and_leveraged(self):
        out = B.excluded_tokens({"BTC", "USDC", "WBTC", "BTCUP", "BTCDOWN", "ETH", "ETHBULL", "ETHBEAR", "FDUSD"})
        self.assertEqual(out, {"USDC", "WBTC", "BTCUP", "BTCDOWN", "ETHBULL", "ETHBEAR", "FDUSD"})

    def test_names_ending_in_up_or_down_without_a_parent_stay(self):
        # JUP and SYRUP end in "UP" but there is no "J" or "SYR" coin: they are real coins, not leveraged tokens
        self.assertEqual(B.excluded_tokens({"JUP", "SYRUP", "UP", "BEAR", "SUNDOWN"}), set())

    def test_leveraged_token_needs_its_parent_in_the_same_set(self):
        self.assertEqual(B.excluded_tokens({"ADAUP"}), set())
        self.assertEqual(B.excluded_tokens({"ADAUP", "ADA"}), {"ADAUP"})


class SegmentsTest(unittest.TestCase):
    def test_continuous_series_is_one_segment(self):
        self.assertEqual(B.segments(list(range(100, 130))), [(0, 30)])

    def test_gap_of_seven_days_restarts_but_six_does_not(self):
        dn = list(range(0, 10)) + list(range(15, 20))       # gap of 6 (9 -> 15)
        self.assertEqual(B.segments(dn), [(0, 15)])
        dn = list(range(0, 10)) + list(range(16, 20))       # gap of 7 (9 -> 16)
        self.assertEqual(B.segments(dn), [(0, 10), (10, 14)])

    def test_price_jump_across_a_missing_day_restarts(self):
        dn = [0, 1, 2, 4, 5]
        c = [1.0, 1.0, 1.0, 25.0, 25.0]                     # 25x across a missing day: redenomination / ticker reuse
        self.assertEqual(B.segments(dn, c), [(0, 3), (3, 5)])
        self.assertEqual(B.segments(dn, [1.0, 1.0, 1.0, 19.0, 19.0]), [(0, 5)])     # under 20x: same coin
        self.assertEqual(B.segments(dn, [25.0, 25.0, 25.0, 1.0, 1.0]), [(0, 3), (3, 5)])  # a 25x fall counts too

    def test_price_jump_on_consecutive_days_is_a_real_move(self):
        self.assertEqual(B.segments([0, 1, 2, 3], [1.0, 1.0, 30.0, 30.0]), [(0, 4)])


class NoLookAheadTest(unittest.TestCase):
    def test_prev_max_and_min_exclude_today(self):
        rnd = random.Random(3)
        x = [rnd.random() for _ in range(300)]
        for n in (1, 5, 20, 55):
            hi, lo = B._prev_max(x, n), B._prev_min(x, n)
            for i in range(len(x)):
                if i < n:
                    self.assertIsNone(hi[i])
                    self.assertIsNone(lo[i])
                else:
                    self.assertEqual(hi[i], max(x[i - n:i]))
                    self.assertEqual(lo[i], min(x[i - n:i]))

    def test_prev_max_matches_indicator_library(self):
        x = walk(400)[3]
        self.assertEqual(B._prev_max(x, 20), I.rolling_max(x, 20, include_current=False))

    def test_features_for_a_day_do_not_change_when_later_days_are_added(self):
        o, h, l_, c, v = walk(320, seed=11)
        d0 = date(2022, 1, 1)
        rows = [((d0 + timedelta(days=i)).isoformat(), o[i], h[i], l_[i], c[i], v[i]) for i in range(len(c))]
        full, cut = B.build_features(rows), B.build_features(rows[:250])
        for k in B.FEATURE_SPEC:
            for i in range(250):
                self.assertTrue(close(full[k][i], cut[k][i]), (k, i))


class AgreesWithIndicatorsTest(unittest.TestCase):
    """bot_core keeps its own copies of the indicator maths; they must give the same numbers as bin/indicators.py."""

    def setUp(self):
        self.o, self.h, self.l, self.c, self.v = walk(500, seed=5)

    def test_sma_ema(self):
        for n in (5, 20, 50, 200):
            self.assertTrue(all(close(a, b) for a, b in zip(B._sma(self.c, n), I.sma(self.c, n))))
            self.assertTrue(all(close(a, b) for a, b in zip(B._ema(self.c, n), I.ema(self.c, n))))

    def test_rsi(self):
        for n in (2, 14):
            self.assertTrue(all(close(a, b) for a, b in zip(B._rsi(self.c, n), I.rsi(self.c, n))))

    def test_atr(self):
        self.assertTrue(all(close(a, b) for a, b in zip(B._atr(self.h, self.l, self.c, 20), I.atr(self.h, self.l, self.c, 20))))

    def test_macd_histogram(self):
        hist = I.macd(self.c, 12, 26, 9)[2]
        self.assertTrue(all(close(a, b) for a, b in zip(B._macd_hist(self.c), hist)))

    def test_short_inputs_give_no_values(self):
        self.assertEqual(B._rsi([1.0, 2.0], 14), [None, None])
        self.assertEqual(B._atr([1.0], [1.0], [1.0], 14), [None])
        self.assertEqual(B._ema([1.0] * 5, 20), [None] * 5)
        self.assertEqual(B._macd_hist([1.0] * 10), [None] * 10)


class FeaturesAndScoreTest(unittest.TestCase):
    def rows(self, c, start=date(2021, 1, 1), skip=()):
        out, d = [], start
        for i, x in enumerate(c):
            if i in skip:
                d += timedelta(days=10)
            out.append((d.isoformat(), x, x * 1.01, x * 0.99, x, 1e6))
            d += timedelta(days=1)
        return out

    def test_indicators_restart_after_a_break(self):
        c = [100.0 + i for i in range(300)]
        F = B.build_features(self.rows(c, skip={250}))
        self.assertEqual(F["seg_bar"][249], 249)
        self.assertEqual(F["seg_bar"][250], 0)
        self.assertIsNotNone(F["sma200"][249])
        self.assertIsNone(F["sma200"][250])                 # a fresh series has no 200-day average yet
        self.assertIsNone(F["ret20"][260])
        self.assertIsNotNone(F["ret20"][270])

    def test_returns(self):
        c = [100.0 * 1.01 ** i for i in range(120)]
        F = B.build_features(self.rows(c))
        self.assertAlmostEqual(F["ret20"][100], 1.01 ** 20 - 1)
        self.assertAlmostEqual(F["ret90"][100], 1.01 ** 90 - 1)
        self.assertIsNone(F["ret90"][89])

    def test_missing_volume_counts_as_zero(self):
        r = self.rows([1.0] * 3)
        r[1] = r[1][:5] + (None,)
        self.assertEqual(B.build_features(r)["v"], [1e6, 0.0, 1e6])

    def test_score_parts_steady_uptrend(self):
        rnd = random.Random(1)
        c = [100.0 * 1.004 ** i * (1 + rnd.uniform(-0.01, 0.01)) for i in range(260)]
        F = B.build_features(self.rows(c))
        for i in range(len(c)):                            # volume rising so the volume condition can pass
            F["vavg20i"][i] = 2.0 if F["vavg20i"][i] is not None else None
            F["vavg60i"][i] = 1.0 if F["vavg60i"][i] is not None else None
        p = B.score_parts(F, 259, btc90=-0.5)
        self.assertEqual(len(p), len(B.SCORE_PARTS))
        self.assertEqual(p[:5], [True] * 5)
        self.assertTrue(p[6] and p[7] and p[9])

    def test_score_parts_too_little_history_is_all_false(self):
        F = B.build_features(self.rows([100.0 + i for i in range(10)]))
        self.assertEqual(B.score_parts(F, 9, btc90=0.0), [False] * 10)

    def test_score_parts_without_btc_comparison(self):
        F = B.build_features(self.rows([100.0 * 1.01 ** i for i in range(120)]))
        self.assertFalse(B.score_parts(F, 119, btc90=None)[4])


if __name__ == "__main__":
    unittest.main()
