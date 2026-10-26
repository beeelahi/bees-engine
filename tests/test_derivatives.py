"""Funding history backfills from the start, resumes after partial data, and stops on a dead network."""
import sqlite3
import time
import unittest
import urllib.parse as U

from tests import _paths  # noqa: F401
import derivatives as D

H8 = 8 * 3600 * 1000


class FundingTest(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time() * 1000)
        self.listing = {"BTCUSDT": 1568102400000, "ETHUSDT": 1574000000000, "SOLUSDT": 1600000000000}
        self._scope = D.scope
        D.scope = lambda con: list(self.listing)
        D.time.sleep = lambda s: None

    def tearDown(self):
        D.scope = self._scope

    def fetch(self, url):
        path, q = url.split("?")
        q = dict(U.parse_qsl(q))
        if path.endswith("exchangeInfo"):
            return {"symbols": [{"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"} for s in self.listing]}
        if path.endswith("fundingRate"):
            st = max(int(q["startTime"]), self.listing[q["symbol"]])
            t0 = -(-st // H8) * H8
            return [{"fundingTime": t, "fundingRate": "0.0001", "markPrice": "1"} for t in range(t0, self.now, H8)][:1000]
        return []

    def test_full_history_and_resume(self):
        con = sqlite3.connect(":memory:")
        con.executescript(D.SCHEMA)
        recent = [("BTCUSDT", t, 0.0001, 1) for t in range(-(-self.now // H8) * H8 - 500 * H8, self.now, H8)]
        con.executemany("INSERT INTO funding VALUES(?,?,?,?)", recent)
        con.execute("INSERT INTO deriv_state VALUES('BTCUSDT',?,?,1,'x','x')", (recent[-1][1], recent[-1][1]))
        r = D.run(con, self.fetch)
        self.assertEqual(r["errors"], 0)
        for s, t in self.listing.items():
            n = con.execute("SELECT COUNT(*) FROM funding WHERE symbol=?", (s,)).fetchone()[0]
            self.assertEqual(n, len(range(-(-t // H8) * H8, self.now, H8)), s)
        self.assertEqual(D.run(con, self.fetch)["funding_rows"], 0)

    def test_dead_network_stops_early(self):
        calls = []

        def dead(url):
            calls.append(url)
            if "exchangeInfo" in url:
                return self.fetch(url)
            raise ConnectionError("curl exit 6")
        self.listing = {f"C{i}USDT": 1600000000000 for i in range(30)}
        con = sqlite3.connect(":memory:")
        r = D.run(con, dead)
        self.assertLessEqual(r["errors"], D.MAX_CONSEC_ERRORS)


if __name__ == "__main__":
    unittest.main()
