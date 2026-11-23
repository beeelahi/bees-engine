"""STF HNS: the short-timeframe watcher finds a forming right shoulder, then the complete pattern, follows it through
the break to the target in its log (each stage once), settles failures, keeps continuation patterns (a top after a
fall), and its backtest trades a break with no look-ahead."""
import os
import tempfile
import unittest

from tests import _paths  # noqa: F401
import stf_hns as S
from tests.test_hs import TOP, ohlc, path


def series(c):
    o, h, l, c = ohlc(c)
    return [k * 900_000 for k in range(len(c))], o, h, l, c


class FormingTest(unittest.TestCase):
    def test_right_shoulder_forming_then_complete(self):
        # rise, LS 100, neck 85, head 120, neck 85, rally to 98 (inside the shoulder zone, below the head)
        c = path([60, 80, 100, 85, 120, 85, 98])
        act = S.scan(*series(c))
        tops = [(p, s) for p, s in act if p["kind"] == "top"]
        self.assertTrue(tops)
        self.assertEqual(tops[0][1]["status"], "forming")
        # once the right shoulder is confirmed (P candles of decline) the full pattern takes over
        c2 = path(TOP)
        st = {s["status"] for p, s in S.scan(*series(c2)) if p["kind"] == "top"}
        self.assertIn("near_neckline", st)
        self.assertNotIn("forming", st)

    def test_no_forming_past_the_head(self):
        c = path([60, 80, 100, 85, 120, 85, 124])                 # rallied above the head: not a shoulder
        self.assertFalse([p for p, s in S.scan(*series(c)) if p["kind"] == "top" and s["status"] == "forming"])


class ContinuationTest(unittest.TestCase):
    def test_top_after_a_fall_is_kept_and_tagged(self):
        c = path([150, 110, 90, 100, 85, 120, 85, 100, 90])       # fell into the pattern: a continuation top
        o, h, l, c = ohlc(c)
        pats = [p for p in S.HS.find(h, l, c, 5, continuation=True) if p["kind"] == "top"]
        self.assertTrue(pats)
        self.assertEqual(pats[0]["context"], "continuation")
        self.assertFalse([p for p in S.HS.find(h, l, c, 5) if p["kind"] == "top"])   # the daily page still skips it


class LogTest(unittest.TestCase):
    def test_stages_once_then_target(self):
        St = {"since": None, "patterns": {}, "events": []}
        seq = []
        base = path(TOP)
        for extra in ([], [84, 82], [84, 82, 80], [84, 80, 75, 72, 66, 60, 55, 50, 48]):
            t, o, h, l, c = series(base + extra)
            evs = S.update_log(St, "BTC", "15m", t, o, h, l, c, S.scan(t, o, h, l, c), "2026-11-23T00:00:00Z")
            seq += [e["status"] for e in evs if e["kind"] == "top"]
        self.assertEqual(seq[:2], ["near_neckline", "broken"])
        self.assertEqual(seq[-1], "target")
        self.assertEqual(seq.count("broken"), 1)                  # each stage reported once
        rec = S.record(St)["15m"]
        self.assertEqual((rec["target"], rec["failed"]), (1, 0))

    def test_failure_is_settled(self):
        St = {"since": None, "patterns": {}, "events": []}
        base = path(TOP)
        for extra in ([84, 82], [84, 80, 75, 72, 85, 98, 101]):   # broke, then closed far back above the neckline
            t, o, h, l, c = series(base + extra)
            evs = S.update_log(St, "ETH", "1h", t, o, h, l, c, S.scan(t, o, h, l, c), "x")
        self.assertIn("failed", [e["status"] for e in evs if e["kind"] == "top"])


class BacktestTest(unittest.TestCase):
    def test_break_then_target(self):
        tr = S.backtest_series(*series(path(TOP) + [84, 80, 75, 70, 62, 55, 50, 45, 40]))
        tops = [x for x in tr if x["kind"] == "top"]
        self.assertEqual(len(tops), 1)
        self.assertEqual(tops[0]["exit"], "target")
        self.assertGreater(tops[0]["r"], 0)


class RunTest(unittest.TestCase):
    def test_first_run_sends_nothing_and_writes_the_page_data(self):
        d = tempfile.mkdtemp()
        S.OUT, S.STATE, S.BTSTATS = (os.path.join(d, x) for x in ("o.json", "s.json", "b.json"))
        t, o, h, l, c = series([100 + (k % 37) * 0.5 for k in range(400)])
        rows = list(zip(t, o, h, l, c))
        S.source_for = lambda code, log=print: ("spot", code + "USDT")
        S.klines = lambda *a, **k: rows
        sent = []
        S.send_alerts = lambda evs, log=print: sent.append(len(evs)) or 0
        out = S.run(log=lambda *a: None)
        self.assertEqual(sent, [])                               # the first run only learns the current state
        self.assertEqual(len(out["instruments"]), 5)
        self.assertEqual(set(out["series"]["XAU"]), {"15m", "1h", "4h"})
        self.assertTrue(os.path.exists(S.OUT) and os.path.exists(S.STATE))
        S.run(log=lambda *a: None)
        self.assertEqual(len(sent), 1)                           # later runs do send (here: an empty batch)


if __name__ == "__main__":
    unittest.main()
