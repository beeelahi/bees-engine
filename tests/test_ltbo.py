"""Long-term breakouts: recorded once with their graded checks, then followed (weekly close, since / best)."""
import json
import sqlite3
import unittest

from tests import _paths  # noqa: F401
import ltbo as L


class LtboTest(unittest.TestCase):
    def test_record_grade_follow(self):
        con = sqlite3.connect(":memory:")
        con.executescript("""CREATE TABLE daily(symbol, date, open, high, low, close, volume_usdt);
            CREATE TABLE latest(symbol, d_bull_score, d_sma200, d_rs30, d_rsi14, w_rsi14, d_dist_ath);
            CREATE TABLE highlight_log(symbol TEXT, tf TEXT, period_start TEXT, kind TEXT, direction TEXT, detail TEXT,
                first_seen_utc TEXT, PRIMARY KEY(symbol, tf, period_start, kind));
            INSERT INTO latest VALUES('ABCUSDT', 8, 12.0, 3.0, 62, 58, -40), ('BTCUSDT', 6, 5.0, 0, 55, 55, -10);""")
        from datetime import date, timedelta
        d0 = date(2026, 7, 1)
        rows, p = [], 10.0
        for k in range(120):                      # a slow range, then a breakout on 2026-10-01 (a Thursday)
            day = (d0 + timedelta(days=k)).isoformat()
            p = 10 + (k % 7) * 0.05 if day < "2026-10-01" else 10.8 + 0.05 * (k - 92)
            rows.append(("ABCUSDT", day, p, p * 1.01, p * 0.99, p, 1e6))
        con.executemany("INSERT INTO daily VALUES(?,?,?,?,?,?,?)", rows)
        chan_rows = [{"symbol": "ABCUSDT", "tf": "1d", "status": "breakout", "days": 400, "event_t": "2026-10-01", "level": 10.4,
                      "slope_pct_day": -0.02, "slope": -0.0002, "t0": "2025-08-01", "lo1": 8.0, "hi1": 10.4, "touches": [3, 3], "width_pct": 30}]
        self.assertEqual(L.record(con, chan_rows, "2026-10-01T00:30:00Z"), 1)
        self.assertEqual(L.record(con, chan_rows, "2026-10-01T00:45:00Z"), 0)
        ck = json.loads(con.execute("SELECT checks FROM ltbo").fetchone()[0])
        self.assertTrue(ck["flat_or_falling"] and ck["not_extended"])
        out = L.export(con, "2026-10-28")
        x = out["rows"][0]
        self.assertEqual(x["weekly"], "held")                     # Sunday 2026-10-04 closed above the line
        self.assertGreater(x["max_pct"], 0)
        self.assertEqual(x["status"], "above the line")


if __name__ == "__main__":
    unittest.main()
