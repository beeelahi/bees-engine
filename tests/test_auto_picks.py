"""Automatic shortlist picks: entry and exit rules, the hash-chained ledger, and the public copy."""
import json
import os
import sqlite3
import tempfile
import unittest

from tests import _paths  # noqa: F401
import auto_picks as A
import public_sl as P

STATS = {"columns": ["signal", "tf", "direction", "variant", "n", "edge_30d", "edge_30d_early", "edge_30d_late", "n_early", "n_late"],
         "rows": [["good_up", "1d", "bullish", "all", 500, 3.0, 2.0, 4.0, 100, 400],
                  ["flaky_up", "1d", "bullish", "all", 500, 3.0, -1.0, 4.0, 100, 400],
                  ["good_down", "1d", "bearish", "all", 500, -3.0, -2.0, -4.0, 100, 400]]}


def write(d, last, screener, signals):
    json.dump({"last_date": last, "signal_stats": STATS}, open(os.path.join(d, "overview.json"), "w"))
    json.dump({"columns": ["symbol", "last_close", "d_bull_score", "d_atr14_pct", "d_rs30"], "rows": screener},
              open(os.path.join(d, "screener.json"), "w"))
    json.dump({"columns": ["tf", "period_start", "symbol", "signal", "direction", "provisional", "confirmed"], "rows": signals},
              open(os.path.join(d, "signals.json"), "w"))


class AutoPicksTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.state, self.ledger = os.path.join(self.d, "ap.json"), os.path.join(self.d, "ap.log")
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE daily (symbol TEXT, date TEXT, low REAL, close REAL)")

    def run_day(self, last, screener, signals):
        write(self.d, last, screener, signals)
        return A.run(self.con, self.d, lambda *a: None, self.state, self.ledger, now=last + "T00:30:00Z")

    def test_lifecycle(self):
        scr = [["AAAUSDT", 10.0, 8, 5.0, 1.0], ["BBBUSDT", 20.0, 9, 5.0, 2.0], ["CCCUSDT", 5.0, 6, 5.0, 3.0],
               ["DDDUSDT", 2.0, 9, 5.0, 0.0]]
        sig = [["1d", "2026-10-10", "AAAUSDT", "good_up", "bullish", 0, 1], ["1d", "2026-10-09", "BBBUSDT", "good_up", "bullish", 0, 0],
               ["1d", "2026-10-10", "CCCUSDT", "good_up", "bullish", 0, 0],          # bull score too low
               ["1d", "2026-10-10", "DDDUSDT", "flaky_up", "bullish", 0, 0],         # signal not proven
               ["1d", "2026-10-01", "DDDUSDT", "good_up", "bullish", 0, 0]]          # too old
        self.assertTrue(self.run_day("2026-10-10", scr, sig))
        st = json.load(open(self.state))
        self.assertEqual([p["symbol"] for p in st["rows"]], ["AAAUSDT", "BBBUSDT"])
        self.assertAlmostEqual(st["rows"][0]["stop"], 9.0)
        self.assertFalse(self.run_day("2026-10-10", scr, sig))                    # same close: nothing new
        # next day: AAA trades below its stop, BBB gets a proven bearish signal
        self.con.executemany("INSERT INTO daily VALUES (?,?,?,?)", [("AAAUSDT", "2026-10-11", 8.5, 9.5), ("BBBUSDT", "2026-10-11", 19, 21)])
        sig.append(["1d", "2026-10-11", "BBBUSDT", "good_down", "bearish", 0, 0])
        scr[1][1] = 21.0
        self.assertTrue(self.run_day("2026-10-11", scr, sig))
        st = json.load(open(self.state))
        a, b = st["rows"][0], st["rows"][1]
        self.assertEqual((a["status"], a["exit"], a["close_reason"]), ("closed", 9.0, "Stop hit"))
        self.assertEqual((b["status"], b["exit"], b["close_reason"]), ("closed", 21.0, "Signal exit"))
        head, n = A.verify(self.ledger)
        self.assertEqual((head, n), (st["head"], 4))
        # public copy: auto rows keep their entry signals and standard reasons, plus the ledger head
        dst = os.path.join(self.d, "out.json")
        self.assertTrue(P.sync(lambda *a: None, os.path.join(self.d, "none.json"), dst, self.state))
        out = json.load(open(dst))
        self.assertEqual(out["auto"]["ledger_head"], head)
        self.assertEqual({r["source"] for r in out["rows"]}, {"auto"})
        self.assertEqual(out["rows"][-1]["why"], ["1d:good_up"])

    def test_tampering_is_detected(self):
        self.run_day("2026-10-10", [["AAAUSDT", 10.0, 8, 5.0, 1.0]], [["1d", "2026-10-10", "AAAUSDT", "good_up", "bullish", 0, 0]])
        text = open(self.ledger).read().replace('"entry":10.0', '"entry":1.0')
        open(self.ledger, "w").write(text)
        with self.assertRaises(ValueError):
            self.run_day("2026-10-11", [["AAAUSDT", 10.0, 8, 5.0, 1.0]], [])


if __name__ == "__main__":
    unittest.main()
