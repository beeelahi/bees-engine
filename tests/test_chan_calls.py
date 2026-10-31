"""Channel calls (rules v2): a channel entry counts only when proven signals agree, the price really touched the
line, the stop is within 8% and there is room for the 3R target; each new call is posted to the highlights feed once."""
import json
import os
import sqlite3
import tempfile
import unittest

from tests import _paths  # noqa: F401
import chan_calls as K

STATS = {"columns": ["signal", "tf", "direction", "variant", "n", "edge_30d", "edge_30d_early", "edge_30d_late", "n_early", "n_late"],
         "rows": [["good_up", "1d", "bullish", "all", 500, 3.0, 2.0, 4.0, 100, 400],
                  ["wk_up", "1w", "bullish", "all", 300, 2.0, 1.0, 3.0, 100, 200],
                  ["bad_up", "1d", "bullish", "all", 500, 3.0, -2.0, 4.0, 100, 400],
                  ["good_down", "1d", "bearish", "all", 500, -3.0, -2.0, -4.0, 100, 400]]}


def chan_row(sym, status, lo=100.0, hi=140.0, close=101.0, rel=2.0, touch=True, high5=None):
    return {"symbol": sym, "tf": "1d", "status": status, "lo1": lo, "hi1": hi, "close": close, "quality": 9.0, "liq_rank": 5,
            "touch3": {"lo": touch, "mid": touch, "hi": touch}, "high5": high5 if high5 is not None else close * 1.02, "low5": close * 0.98,
            "t0": "2025-01-01", "days": 400, "touches": [3, 3], "mid_touches": 2, "width_pct": 40, "slope_pct_day": 0.01, "pos": 0.1,
            "n": 0, "hist": {"rel30": rel, "beat30": 59, "n": 900, "group": "late_long", "move30": -3}}


class CallsTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript("""
            CREATE TABLE signals(tf, period_start, symbol, signal, direction, provisional, warm, confirmed);
            CREATE TABLE latest(symbol, d_bull_score, d_rsi14, d_rs30);
            CREATE TABLE highlight_log(symbol TEXT, tf TEXT, period_start TEXT, kind TEXT, direction TEXT, detail TEXT,
                first_seen_utc TEXT, PRIMARY KEY(symbol, tf, period_start, kind));""")
        self.con.executemany("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?)", [
            ("1d", "2026-10-24", "AAAUSDT", "good_up", "bullish", 0, 1, 1), ("1w", "2026-10-19", "AAAUSDT", "wk_up", "bullish", 0, 1, 0),
            ("1d", "2026-10-25", "BBBUSDT", "bad_up", "bullish", 0, 1, 0),                    # not proven
            ("1d", "2026-10-20", "CCCUSDT", "good_up", "bullish", 0, 1, 0),                   # too old
            ("1d", "2026-10-25", "DDDUSDT", "good_down", "bearish", 0, 1, 0),
            ("1d", "2026-10-25", "EEEUSDT", "good_up", "bullish", 0, 1, 0),
            ("1d", "2026-10-25", "GGGUSDT", "good_up", "bullish", 0, 1, 0), ("1d", "2026-10-25", "HHHUSDT", "good_down", "bearish", 0, 1, 0),
            ("1d", "2026-10-25", "JJJUSDT", "good_up", "bullish", 0, 1, 0)])
        self.con.executemany("INSERT INTO latest VALUES(?,?,?,?)", [("AAAUSDT", 7, 55, 3), ("BBBUSDT", 8, 60, 1), ("CCCUSDT", 8, 60, 1),
                                                                    ("DDDUSDT", 3, 40, -5), ("EEEUSDT", 3, 45, 0), ("GGGUSDT", 8, 50, 1),
                                                                    ("HHHUSDT", 6, 50, 1), ("JJJUSDT", 8, 50, 1)])
        self.rows = [chan_row("AAAUSDT", "near_bottom"), chan_row("BBBUSDT", "bottom_bounce"), chan_row("CCCUSDT", "mid_retest"),
                     chan_row("DDDUSDT", "mid_loss", close=118.0, rel=-1.0, high5=121.0), chan_row("EEEUSDT", "near_bottom"),
                     chan_row("FFFUSDT", "inside"),
                     chan_row("GGGUSDT", "near_bottom", touch=False),                 # near the bottom, but never reached the line
                     chan_row("HHHUSDT", "mid_loss", close=118.0, high5=130.0),        # stop above the 5-day high is 10% away
                     chan_row("JJJUSDT", "near_bottom", close=104.0)]                  # stop 7.7% away, but the midline is only 2R up

    def test_build_and_log(self):
        C = K.build(self.con, self.rows, STATS, "2026-10-25")
        got = {x["symbol"]: x for x in C["rows"]}
        self.assertEqual(set(got), {"AAAUSDT", "DDDUSDT"})          # EEE weak bull score, GGG no touch, HHH stop too far, JJJ no room
        a = got["AAAUSDT"]
        self.assertEqual((a["side"], a["signals"], a["rules"]), ("long", ["1d:good_up", "1w:wk_up"], "v2"))
        self.assertAlmostEqual(a["stop"], 96.0)                      # 10% of the width under the bottom line
        self.assertAlmostEqual(a["t1"], 116.0)                       # take profit at 3R
        self.assertEqual(a["line_target"], 120.0)                    # the midline is 3.8R away: room for it
        self.assertLessEqual(a["risk_pct"], 8.0)
        self.assertGreaterEqual((a["t1"] - a["close"]) / (a["close"] - a["stop"]), K.MIN_RR - 1e-9)
        d = got["DDDUSDT"]
        self.assertEqual(d["side"], "short")
        self.assertAlmostEqual(d["stop"], 121.0 * 1.005)             # just above the 5-day high
        self.assertAlmostEqual(d["t1"], 118.0 - 3 * (121.0 * 1.005 - 118.0))
        self.assertEqual(K.log(self.con, C, "2026-10-26T00:30:00Z"), 2)
        self.assertEqual(K.log(self.con, C, "2026-10-26T00:45:00Z"), 0)
        C2 = K.build(self.con, self.rows, STATS, "2026-10-26")
        self.assertEqual(K.log(self.con, C2, "2026-10-27T00:30:00Z"), 0)   # not re-posted within 7 days

    def test_divergence_grades_a(self):
        from tests.test_diverge import BULL, series
        o, h, l_, c = series(BULL)
        self.con.execute("CREATE TABLE daily(symbol, date, open, high, low, close)")
        self.con.executemany("INSERT INTO daily VALUES(?,?,?,?,?,?)", [("AAAUSDT", f"2026-{1 + k // 28:02d}-{1 + k % 28:02d}", o[k], h[k], l_[k], c[k]) for k in range(len(c))])
        last = len(c) - 1
        d = K.recent_divergences(self.con, ["AAAUSDT", "DDDUSDT"], days=last)
        self.assertEqual(d["AAAUSDT"]["side"], "bull")
        self.assertNotIn("DDDUSDT", d)
        C = K.build(self.con, self.rows, STATS, "2026-10-25")
        self.assertLessEqual(len(c) - 1 - (c.index(min(c[60:])) + 3), 10)     # the swing low was confirmed (3 candles later) within the last 10
        got = {x["symbol"]: x for x in C["rows"]}
        self.assertEqual((got["AAAUSDT"]["grade"], got["DDDUSDT"]["grade"]), ("A", "B"))   # a bullish divergence backs the long
        self.assertEqual(got["AAAUSDT"]["divergence"]["side"], "bull")

    def test_lab_stats(self):
        p = os.path.join(tempfile.mkdtemp(), "calls_lab.json")
        self.assertIsNone(K.lab_stats(p))
        rows = [dict(K.LAB_ROWS["long"], side="long", n=106, win=33.0, avg_r=0.29, pf=1.42, years={"2022": [8, 0.02, 38]}, worst_year=0.02),
                dict(K.LAB_ROWS["long"], side="long", min_rr=2.0, n=999, avg_r=9.0)]          # a different rule: not picked
        with open(p, "w") as f:
            json.dump({"rules": rows, "baseline": {"up30": 35.8}}, f)
        st = K.lab_stats(p)
        self.assertEqual((st["long"]["n"], st["long"]["avg_r"]), (106, 0.29))
        self.assertNotIn("short", st)
        self.assertEqual(st["baseline"]["up30"], 35.8)


if __name__ == "__main__":
    unittest.main()
