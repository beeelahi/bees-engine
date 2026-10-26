"""Channel calls: a channel entry counts only when proven signals in the same direction and the bull score agree;
each new call is posted to the highlights feed once."""
import sqlite3
import unittest

from tests import _paths  # noqa: F401
import chan_calls as K

STATS = {"columns": ["signal", "tf", "direction", "variant", "n", "edge_30d", "edge_30d_early", "edge_30d_late", "n_early", "n_late"],
         "rows": [["good_up", "1d", "bullish", "all", 500, 3.0, 2.0, 4.0, 100, 400],
                  ["wk_up", "1w", "bullish", "all", 300, 2.0, 1.0, 3.0, 100, 200],
                  ["bad_up", "1d", "bullish", "all", 500, 3.0, -2.0, 4.0, 100, 400],
                  ["good_down", "1d", "bearish", "all", 500, -3.0, -2.0, -4.0, 100, 400]]}


def chan_row(sym, status, lo=100.0, hi=140.0, close=104.0, rel=2.0):
    return {"symbol": sym, "tf": "1d", "status": status, "lo1": lo, "hi1": hi, "close": close, "quality": 9.0, "liq_rank": 5,
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
            ("1d", "2026-10-25", "EEEUSDT", "good_up", "bullish", 0, 1, 0)])
        self.con.executemany("INSERT INTO latest VALUES(?,?,?,?)", [("AAAUSDT", 7, 55, 3), ("BBBUSDT", 8, 60, 1), ("CCCUSDT", 8, 60, 1),
                                                                    ("DDDUSDT", 3, 40, -5), ("EEEUSDT", 3, 45, 0)])
        self.rows = [chan_row("AAAUSDT", "near_bottom"), chan_row("BBBUSDT", "bottom_bounce"), chan_row("CCCUSDT", "mid_retest"),
                     chan_row("DDDUSDT", "mid_loss", close=118.0, rel=-1.0), chan_row("EEEUSDT", "near_bottom"),
                     chan_row("FFFUSDT", "inside")]

    def test_build_and_log(self):
        C = K.build(self.con, self.rows, STATS, "2026-10-25")
        got = {x["symbol"]: x for x in C["rows"]}
        self.assertEqual(set(got), {"AAAUSDT", "DDDUSDT"})          # EEE: bull score 3 is too weak for a long
        a = got["AAAUSDT"]
        self.assertEqual((a["side"], a["grade"], a["signals"]), ("long", "A", ["1d:good_up", "1w:wk_up"]))
        self.assertAlmostEqual(a["stop"], 96.0)
        self.assertEqual(a["t1"], 120.0)                             # midline first, then the top
        self.assertEqual(a["t2"], 140.0)
        d = got["DDDUSDT"]
        self.assertEqual((d["side"], d["grade"]), ("short", "B"))
        self.assertAlmostEqual(d["stop"], 124.0)                     # back above the midline + 10% of the width
        self.assertEqual(K.log(self.con, C, "2026-10-26T00:30:00Z"), 2)
        self.assertEqual(K.log(self.con, C, "2026-10-26T00:45:00Z"), 0)
        C2 = K.build(self.con, self.rows, STATS, "2026-10-26")
        self.assertEqual(K.log(self.con, C2, "2026-10-27T00:30:00Z"), 0)   # not re-posted within 7 days


if __name__ == "__main__":
    unittest.main()
