"""Moving-average lab: a touch that bounces is HELD, one that closes through is BROKE, the base rate is the same rule
from any close, only complete weeks count, the three picks keep distinct lengths and need the check period to agree,
and the whole run produces the page's data from a small database."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "research"))
import ma_lab as M  # noqa: E402


def flat_ma(n, v):
    return [v] * n


class ReactionTest(unittest.TestCase):
    def setUp(self):
        # average fixed at 100, ATR 2 (so touch band 0.5, hold target 103, break close < 99)
        self.m = flat_ma(12, 100.0)
        self.a = [2.0] * 12

    def run_case(self, lows, highs, closes):
        o = closes[:]
        return M.reaction(o, highs, lows, closes, self.m, self.a, 0, len(closes), H=5)

    def test_bounce_held(self):
        c = [104, 104, 104, 104, 101, 102, 104, 104, 104, 104, 104, 104]
        l = [103, 103, 103, 103, 100.3, 101, 103, 103, 103, 103, 103, 103]      # bar 4 dips to 100.3: a fresh touch
        h = [105, 105, 105, 105, 102, 103.2, 105, 105, 105, 105, 105, 105]     # bar 5 reaches 103.2 >= 103: held
        tests, held, sides, last = self.run_case(l, h, c)
        self.assertEqual((tests, held), (1, 1))
        self.assertEqual(last[2], "held")

    def test_close_through_broke(self):
        c = [104, 104, 104, 104, 101, 98.5, 104, 104, 104, 104, 104, 104]      # bar 5 closes 98.5 < 99: broke first
        l = [103, 103, 103, 103, 100.3, 98, 103, 103, 103, 103, 103, 103]
        h = [105, 105, 105, 105, 102, 104, 105, 105, 105, 105, 105, 105]      # its high also passed 103: broke still wins
        tests, held, sides, last = self.run_case(l, h, c)
        self.assertEqual((tests, held, last[2]), (1, 0, "broke"))

    def test_resistance_mirror(self):
        c = [96, 96, 96, 96, 99, 97, 96, 96, 96, 96, 96, 96]
        h = [97, 97, 97, 97, 99.7, 98, 97, 97, 97, 97, 97, 97]                # bar 4 pokes up to 99.7: fresh touch from below
        l = [95, 95, 95, 95, 98, 96.8, 95, 95, 95, 95, 95, 95]               # bar 5 low 96.8 <= 97: held
        tests, held, sides, last = self.run_case(l, h, c)
        self.assertEqual((tests, held, sides), (1, 1, [-1]))

    def test_base_rate_from_the_low(self):
        c = [100.0] * 30
        h = [100.5] * 30
        l = [99.0] * 30                                                      # lows 1 below the close, ATR 2: target low + 3 = 102
        h[10] = 102.2                                                        # only bars 5..9 (within 5 of bar 10) reach it
        up, dn = M.base_rate(h, l, c, [2.0] * 30, 0, 30, 5)
        self.assertAlmostEqual(up, 5 / 29)
        self.assertAlmostEqual(dn, 1 / 29)                                   # bar 10 itself: its high 102.2, a low 3 below

    def test_spread(self):
        r = [{"ma": m, "passes": True} for m in ("SMA8", "SMA9", "SMA10", "EMA21", "EMA26", "EMA55")]
        self.assertEqual([x["ma"] for x in M.top3(r)], ["SMA8", "EMA21", "EMA55"])


class PickTest(unittest.TestCase):
    def test_distinct_lengths_and_check_agrees(self):
        good = {"tests": 999, "edge": 5, "sharpe_adv": 0.5, "split": 1.0}
        rows = [("EMA20", good, {"edge": 3, "split": 1}), ("SMA20", dict(good, edge=4.9), {"edge": 3, "split": 1}),
                ("EMA9", dict(good, edge=1), {"edge": 1, "split": -1}), ("EMA50", dict(good, edge=2), {"edge": 1, "split": 1}),
                ("SMA200", dict(good, edge=0.5), {"edge": 1, "split": 1}), ("EMA5", dict(good, tests=10), {"edge": 9, "split": 9})]
        best = [x["ma"] for x in M.top3(M.rank_pick(rows, 100))]
        self.assertEqual(best[0], "EMA20")
        self.assertNotIn("SMA20", best)                                     # same length as EMA20 (spread rule)
        self.assertNotIn("EMA5", best)                                      # too few tests
        self.assertEqual(set(best), {"EMA20", "EMA50", "SMA200"})           # EMA9 fails the check period (split)


def make_db(db):
    """Two coins (BTC, one of the picks, and AAA) with 1,500 daily and 3,000 4-hour candles of a slow wave."""
    import math
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE daily(symbol, date, open, high, low, close, volume_usdt);
        CREATE TABLE candles_4h(symbol, open_ms, open, high, low, close, close_ms);
        CREATE TABLE latest(symbol, in_universe); CREATE TABLE pairs(symbol, token);""")
    now =datetime.now(timezone.utc)
    for s, ph in (("BTCUSDT", 0.0), ("AAAUSDT", 1.0)):
        con.execute("INSERT INTO latest VALUES(?,1)", (s,))
        con.execute("INSERT INTO pairs VALUES(?,?)", (s, s[:-4]))
        for k in range(1500, 0, -1):
            day = (now - timedelta(days=k)).strftime("%Y-%m-%d")
            p = 100 * (1.5 + math.sin(k / 40 + ph)) + k * 0.01
            con.execute("INSERT INTO daily VALUES(?,?,?,?,?,?,?)", (s, day, p, p * 1.02, p * 0.98, p * 1.001, 1e6))
        t0 = int((now - timedelta(days=500)).timestamp() * 1000) // 14_400_000 * 14_400_000
        for k in range(3000):
            p = 100 * (1.5 + math.sin(k / 60 + ph))
            con.execute("INSERT INTO candles_4h VALUES(?,?,?,?,?,?,?)", (s, t0 + k * 14_400_000, p, p * 1.01, p * 0.99, p * 1.001,
                                                                       t0 + (k + 1) * 14_400_000 - 1))
    con.commit()
    con.close()


class RunTest(unittest.TestCase):
    def test_end_to_end(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "b.db")
        make_db(db)
        out = os.path.join(d, "ma.json")
        M.MIN_TESTS = {"1w": 1, "1d": 1, "4h": 1}
        M.main(db, out, log=lambda *a: None)
        with open(out) as f:
            r = json.load(f)
        self.assertEqual(set(r["timeframes"]), {"1w", "1d", "4h"})
        for tf in ("1w", "1d", "4h"):
            self.assertEqual(len(r["timeframes"][tf]["table"]), 34)
            self.assertEqual(len(r["timeframes"][tf]["top3"]), 3)
        self.assertEqual(r["picks"], ["BTCUSDT"])                          # the only pick in this database
        self.assertIn("ETH", r["picks_missing"])
        btc = next(c for c in r["coins"] if c["symbol"] == "BTCUSDT")
        self.assertTrue(btc["pick"])
        b = btc["tf"]["1d"]["best"]
        self.assertEqual(len(b), 3)                                         # its own three, with evidence and today's position
        self.assertEqual(len(b[0]), 17)
        self.assertIn("fresh", r)
        cr = b[0][16]
        self.assertTrue(cr is None or (cr[1] in (1, -1) and cr[0] >= 0))
        ns = sorted(int("".join(ch for ch in x[0] if ch.isdigit())) for x in b)
        self.assertTrue(all(ns[k + 1] / ns[k] >= 1.5 for k in range(2)))   # fast / medium / slow
        self.assertIsInstance(b[0][7], bool)
        with open(os.path.join(d, "ma_summary.json")) as f:                # the small companion agrees with the full file
            sm = json.load(f)
        e = sm["coins"]["BTCUSDT"]["tf"]["1d"]
        self.assertEqual(e[2], [x[0] for x in b])
        self.assertEqual(e[0], sum(1 for x in b if x[9] is not None and x[9] > 0))
        self.assertEqual((e[4], e[7]), (b[0][8], b[0][16]))
        self.assertEqual(sm["fresh"], r["fresh"])
        self.assertTrue(os.path.exists(os.path.join(d, "ma_cross_log.json")))   # the forward record sits next to the database
        self.assertEqual(r["cross_log"]["total"], len({(x[0], x[1], x[2], x[6]) for x in r["fresh"]}))
        self.assertEqual(set(r["cross_log"]["stats"]), {"1w", "1d", "4h"})


class CrossRecordTest(unittest.TestCase):
    def test_logged_once_then_measured(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "log.json")
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        row = ["EMA26", 9, 50, 1, 0.1, 1, 1, True, 100.0, 5.0, 1.0, 0.5, None, 10, 5, 4, [0, 1, "2026-11-10", 100.0]]
        out = {"coins": [{"symbol": "XUSDT", "pick": True, "tf": {"1d": {"best": [row], "close": 100.0, "last_bar": "2026-11-10"}}}]}
        r1 = M.cross_record(p, out, now)["rows"]
        self.assertEqual(len(r1), 1)
        self.assertEqual((r1[0]["ret"], r1[0]["side"]), (0.0, True))
        row2 = list(row)
        row2[9], row2[16] = -2.0, [1, -1, "2026-11-11", 95.0]           # a day later it fell back under: a new cross down
        out["coins"][0]["tf"]["1d"].update(best=[row2], close=95.0, last_bar="2026-11-11")
        rec = M.cross_record(p, out, now + timedelta(days=6))
        r2 = rec["rows"]
        self.assertEqual(len(r2), 2)                                            # the first one is not logged twice
        up = next(e for e in r2 if e["dir"] == 1)
        self.assertEqual((up["ret"], up["side"]), (-5.0, False))               # measured, and no longer above
        st = rec["stats"]["1d"]                                                # only the first was shown 5+ days earlier
        self.assertEqual((st["n"], st["median_ret"], st["positive_pct"], st["still_on_side_pct"]), (1, -5.0, 0.0, 0.0))


class WeeksTest(unittest.TestCase):
    def test_only_complete_weeks(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE daily(symbol, date, open, high, low, close)")
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)                 # a Wednesday
        start = datetime(2026, 10, 19)                                       # a Monday: 3 full weeks + this partial one
        for k in range((now.replace(tzinfo=None) - start).days):
            d = (start + timedelta(days=k)).strftime("%Y-%m-%d")
            con.execute("INSERT INTO daily VALUES('X',?,?,?,?,?)", (d, 1 + k, 2 + k, 0.5 + k, 1.5 + k))
        con.execute("DELETE FROM daily WHERE date='2026-10-29'")            # a missing day: that week is incomplete
        w = M.load(con, "X", "1w", now)
        self.assertEqual([x[0] for x in w], ["2026-10-19", "2026-11-02"])
        self.assertEqual(w[0][1:], (1, 8, 0.5, 7.5))                         # Monday's open, week high/low, Sunday's close


if __name__ == "__main__":
    unittest.main()
