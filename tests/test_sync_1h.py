"""1-hour candles: first download, incremental top-up, and the audit against daily candles (fake exchange)."""
import sqlite3
import unittest

from tests import _paths  # noqa: F401
import sync_1h as H

DAY = 86_400_000
T0 = 1_760_000_000_000 // DAY * DAY          # a UTC midnight


def kline(t, p):
    return [t, str(p), str(p + 1), str(p - 1), str(p + 0.5), "10", t + H.H1_MS - 1, "10000", 5, "4", str(4 * p)]


class FakeApi:
    def __init__(self, end):
        self.end, self.calls = end, 0

    def __call__(self, path, q):
        self.calls += 1
        out, t = [], q["startTime"]
        while t + H.H1_MS <= self.end and len(out) < q["limit"]:
            out.append(kline(t, 100.0))
            t += H.H1_MS
        return out


class Sync1hTest(unittest.TestCase):
    def test_download_topup_audit(self):
        con = sqlite3.connect(":memory:")
        con.executescript("""CREATE TABLE pairs(symbol, token, status); CREATE TABLE daily(symbol, date, open, high, low, close, volume_usdt);""")
        con.execute("INSERT INTO pairs VALUES('ABCUSDT','ABC','TRADING')")
        days = [(T0 + k * DAY) for k in range(3)]
        from datetime import datetime, timezone
        ds = [datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d") for t in days]
        for d in ds:
            con.execute("INSERT INTO daily VALUES('ABCUSDT',?,100,101,99,100.5,240000)", (d,))
        today = ds[-1]
        end = T0 + 3 * DAY
        self.addCleanup(setattr, H, "H1_DAYS", H.H1_DAYS)
        H.H1_DAYS = 2                       # keep the fake history small
        api = FakeApi(end)
        r = H.run(con, api, today, "s1", now_ms=end)
        self.assertEqual(r["rows"], 72)
        self.assertEqual(r["complete"], 1)
        self.assertEqual(r["audit_bad"], 0)
        self.assertGreater(r["audit_checked"], 0)
        api.end = end + 2 * H.H1_MS
        r = H.run(con, api, today, "s2", now_ms=end + 2 * H.H1_MS)
        self.assertEqual(r["added"], 2)


if __name__ == "__main__":
    unittest.main()
