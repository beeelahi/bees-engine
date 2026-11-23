"""STF HNS: the short-timeframe watcher finds a forming right shoulder, then the complete pattern, follows it through
the break to the target in its log (each stage once), settles failures, keeps continuation patterns (a top after a
fall), and its backtest trades a break with no look-ahead."""
import os
import tempfile
import unittest

from tests import _paths  # noqa: F401
import stf_hns as S
from tests.test_hs import ohlc, path

# a proper top: a real rise into it (more than one height below the neckline), shoulders at 100, neckline 85, head 120
TOP = [40, 70, 100, 85, 120, 85, 100, 90]


def series(c):
    o, h, l, c = ohlc(c)
    return [k * 900_000 for k in range(len(c))], o, h, l, c


class FormingTest(unittest.TestCase):
    def test_right_shoulder_forming_then_complete(self):
        # rise, LS 100, neck 85, head 120, neck 85, rally to 98 (inside the shoulder zone, below the head)
        c = path([40, 70, 100, 85, 120, 85, 99]) + [96, 95, 94]
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
        c = path([40, 70, 100, 85, 120, 85, 124])                 # rallied above the head: not a shoulder
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
    def test_replay_trades_the_break_alert(self):
        S.REPLAY_W = 100
        t, o, h, l, c = series([110.0] * 20 + path(TOP) + [84, 80, 75, 70, 62, 55, 50, 45, 40])
        rows = list(zip(t, o, h, l, c))
        br = [b for b in S.replay_breaks(rows, "BTC", "15m") if b["kind"] == "top"]
        self.assertEqual(len(br), 1)
        tr = S.trade(rows, br[0], "stop_shoulder")
        self.assertEqual(tr["exit"], "target")
        self.assertGreater(tr["r"], 0)


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


class GoldCaseTest(unittest.TestCase):
    """Bilal's trade: XAUUSDT 15m, 20 Nov 2026 (real candles, tests/data_xau_15m_20nov.txt). The top must be found,
    broken at 15:15 UTC, and reach its target the same evening (it did: low 3,509 at 18:45)."""
    def test_found_broken_target(self):
        from datetime import datetime, timezone
        rows = []
        with open(os.path.join(os.path.dirname(__file__), "data_xau_15m_20nov.txt")) as f:
            for ln in f:
                d, tm, o, h, l, c = ln.split()
                ms = int(datetime.strptime(f"2026-{d} {tm}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)
                rows.append((ms, float(o), float(h), float(l), float(c)))
        t, o, h, l, c = S._series(rows)
        f = lambda k: datetime.fromtimestamp(t[k] / 1000, timezone.utc).strftime("%m-%d %H:%M")   # noqa: E731
        tops = [p for P in S.P_SIZES for p in S.HS.find(h, l, c, P, max_span=S.SPAN, continuation=True,
                                                         head_min=S.HEAD_MIN, trough_max=S.TROUGH_MAX) if p["kind"] == "top"]
        hit = [p for p in tops if f(p["hd"]) == "11-20 10:30"]
        self.assertTrue(hit, "the 20 Nov gold top was not found")
        p = hit[0]
        self.assertEqual((f(p["lsh"]), f(p["rsh"])), ("11-20 08:30", "11-20 15:00"))
        # the daily limits would have missed it
        self.assertFalse([q for q in S.HS.find(h, l, c, p["P"], max_span=S.SPAN, continuation=True) if q["hd"] == p["hd"]])
        why, k = S.exit_reason(p, h, l, c, len(c) - 1)
        self.assertEqual(why, "target")
        self.assertLessEqual(f(k), "11-20 18:45")
        # replay the watcher candle by candle, as the 5-minute timer would have seen it: the break must be reported on
        # the 15:15 candle itself (the right shoulder is only confirmed at 16:15), and the target reached after it
        St, seen = {"since": None, "patterns": {}, "events": []}, []
        start = next(k for k in range(len(t)) if f(k) == "11-20 12:00")
        for n in range(start, len(t)):
            tt, oo, hh, ll, cc = t[:n + 1], o[:n + 1], h[:n + 1], l[:n + 1], c[:n + 1]
            for e in S.update_log(St, "XAU", "15m", tt, oo, hh, ll, cc, S.scan(tt, oo, hh, ll, cc), "x"):
                if e["kind"] == "top" and (":" + str(t[p["hd"]])) in e["key"]:
                    seen.append((e["status"], f(t.index(e["bar_ms"])), e["key"].split("#")[1] if "#" in e["key"] else "1"))
        # first attempt: a lower right shoulder at 13:00 broke at 13:15 (no "forming" first: the shoulder had not turned
        # before the break) and failed at 14:00, a fake-out reported as such;
        # second attempt on the same head: right shoulder 15:00, break on the 15:15 candle, target reached by 18:45
        # the 13:00 "shoulder" (left shoulder to it: 18 candles) is too narrow to count; the real pattern is the only one
        self.assertFalse([x for x in seen if x[2] != "1"])
        second = [x[:2] for x in seen]
        self.assertIn(("broken", "11-20 15:15"), second)
        self.assertEqual(second[-1][0], "target")
        self.assertLessEqual(second[-1][1], "11-20 18:45")
        # the backtest replays exactly these alerts: the fake-out is a loss either way; the real break reaches its
        # target with the stop beyond the right shoulder but is stopped first with the tight neckline stop
        S.REPLAY_W = 60
        br = [b for b in S.replay_breaks(rows, "XAU", "15m") if b["kind"] == "top" and f(b["n"]).startswith("11-20 1")]
        self.assertEqual([f(b["n"]) for b in br], ["11-20 15:15"])
        self.assertEqual(S.trade(rows, br[0], "stop_shoulder")["exit"], "target")
        self.assertEqual(S.trade(rows, br[0], "stop_neck")["exit"], "stop")
