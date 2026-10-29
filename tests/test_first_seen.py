"""first_seen: an entry keeps the day it was first listed while it stays listed; a long gap makes it new again."""
import sqlite3
import unittest

from tests import _paths  # noqa: F401
import first_seen as FS


class FirstSeenTest(unittest.TestCase):
    def test_keeps_first_date_and_resets_after_gap(self):
        con, k = sqlite3.connect(":memory:"), (lambda r: r["s"])
        self.assertEqual(FS.stamp(con, "x", [{"s": "A"}], k, "2026-10-01")[0]["added"], "2026-10-01")
        self.assertEqual(FS.stamp(con, "x", [{"s": "A"}], k, "2026-10-03")[0]["added"], "2026-10-01")
        self.assertEqual(FS.stamp(con, "x", [{"s": "A"}], k, "2026-10-06")[0]["added"], "2026-10-01")   # 3-day gap: same entry
        self.assertEqual(FS.stamp(con, "x", [{"s": "A"}], k, "2026-10-20")[0]["added"], "2026-10-20")   # gone 14 days: new
        self.assertEqual(FS.stamp(con, "y", [{"s": "A"}], k, "2026-10-20")[0]["added"], "2026-10-20")   # sections separate

    def test_seed_for_new_entries_only(self):
        con, k = sqlite3.connect(":memory:"), (lambda r: r["s"])
        r = FS.stamp(con, "x", [{"s": "A"}, {"s": "B"}], k, "2026-10-10", seed=lambda r: "2026-10-04" if r["s"] == "A" else "2026-11-01")
        self.assertEqual([x["added"] for x in r], ["2026-10-04", "2026-10-10"])                          # future seed ignored

    def test_timestamps_and_bad_stored_dates(self):
        con, k = sqlite3.connect(":memory:"), (lambda r: r["s"])
        self.assertEqual(FS.as_date(1793232000), "2026-10-29")
        self.assertEqual(FS.as_date("1793232000000"), "2026-10-29")
        self.assertEqual(FS.as_date("2026-10-28T04:00:00Z"), "2026-10-28")
        self.assertIsNone(FS.as_date("junk"))
        con.execute("CREATE TABLE first_seen(section TEXT, key TEXT, first_date TEXT, last_date TEXT, PRIMARY KEY(section, key))")
        con.execute("INSERT INTO first_seen VALUES('x','A','1793232000','2026-10-28')")
        r = FS.stamp(con, "x", [{"s": "A"}], k, "2026-10-29", seed=lambda r: 1793145600)
        self.assertEqual(r[0]["added"], "2026-10-28")

    def test_feed_date(self):
        con = sqlite3.connect(":memory:")
        self.assertIsNone(FS.feed_date(con, "hs_event", "ZIGUSDT"))
        con.execute("CREATE TABLE highlight_log(kind TEXT, symbol TEXT, direction TEXT, period_start TEXT)")
        con.executemany("INSERT INTO highlight_log VALUES(?,?,?,?)", [("hs_event", "ZIGUSDT", "bullish", "2026-10-02"), ("hs_event", "ZIGUSDT", "bullish", "2026-10-08")])
        self.assertEqual(FS.feed_date(con, "hs_event", "ZIGUSDT", "bullish", today="2026-10-10"), "2026-10-02")
        self.assertEqual(FS.feed_date(con, "hs_event", "ZIGUSDT", "bullish", today="2026-10-10", latest=True), "2026-10-08")
        self.assertIsNone(FS.feed_date(con, "hs_event", "ZIGUSDT", "bearish", today="2026-10-10"))


if __name__ == "__main__":
    unittest.main()
