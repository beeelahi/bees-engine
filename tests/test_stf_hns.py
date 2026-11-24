"""STF HNS: the short-timeframe watcher finds a forming right shoulder, then the complete pattern, follows it through
the break to the target in its log (each stage once), settles failures, keeps continuation patterns (a top after a
fall), and its backtest trades a break with no look-ahead."""
import os
import tempfile
import unittest

from tests import _paths  # noqa: F401
import stf_hns as S
from tests.test_hs import ohlc, path

# a proper top: a real rise into it (more than one height below the neckline), shoulders at 104, flat neckline 85,
# head 120 (shoulders 54% of the height above the neckline, head 46% above the shoulders)
TOP = [30, 60, 104, 85, 120, 85, 104, 90]


def series(c):
    o, h, l, c = ohlc(c)
    return [k * 900_000 for k in range(len(c))], o, h, l, c


class FormingTest(unittest.TestCase):
    def test_right_shoulder_forming_then_complete(self):
        # rise, LS 100, neck 85, head 120, neck 85, rally to 98 (inside the shoulder zone, below the head)
        c = path([30, 60, 104, 85, 120, 85, 103]) + [100, 99, 98]
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
        c = path([30, 60, 104, 85, 120, 85, 124])                 # rallied above the head: not a shoulder
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
        rows = [r + (1000.0 + (500.0 if 46 <= k <= 50 else 0.0),) for k, r in enumerate(zip(t, o, h, l, c))]   # busier around the left shoulder
        br = [b for b in S.replay_breaks(rows, "BTC", "15m") if b["kind"] == "top"]
        self.assertEqual(len(br), 1)
        f = br[0]["f"]
        self.assertEqual(set(f), set(S.FEATURES))
        self.assertLess(f["vol_rs_vs_ls"], 1)                     # quieter right shoulder, as the textbook likes it
        self.assertAlmostEqual(f["neck_tilt"], 0, places=2)        # flat neckline
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


class GoldCaseTest(unittest.TestCase):
    """Bilal's trade: XAUUSDT 15m, 20 Nov 2026 (real candles, tests/data_xau_15m_20nov.txt), a valid top by his rules:
    left shoulder 3,571.9 (06:00 UTC), head 3,600.8 (10:30), right shoulder 3,591.9 (15:00), neckline rising from the
    07:30 low (3,547.2) through the 14:30 low (3,555.7). The 08:30 bump is not the shoulder: with it the textbook
    neckline would slope down about 37 degrees, which his rule forbids."""
    def load(self):
        from datetime import datetime, timezone
        rows = []
        with open(os.path.join(os.path.dirname(__file__), "data_xau_15m_20nov.txt")) as f:
            for ln in f:
                d, tm, o, h, l, c = ln.split()
                ms = int(datetime.strptime(f"2026-{d} {tm}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)
                rows.append((ms, float(o), float(h), float(l), float(c)))
        f = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%m-%d %H:%M")   # noqa: E731
        return rows, f

    def test_found_with_the_allowed_neckline(self):
        rows, f = self.load()
        t, o, h, l, c = S._series(rows)
        tops = [p for P in S.P_SIZES for p in S.find(h, l, c, P) if p["kind"] == "top" and f(t[p["hd"]]) == "11-20 10:30"]
        self.assertTrue(tops, "the 20 Nov gold top was not found")
        p = tops[0]
        self.assertEqual((f(t[p["lsh"]]), f(t[p["rsh"]]), f(t[p["t1"]]), f(t[p["t2"]])), ("11-20 06:00", "11-20 15:00", "11-20 07:30", "11-20 14:30"))
        self.assertGreater(S.HS.neck(p, p["rsh"]), S.HS.neck(p, p["lsh"]))     # the neckline rises: allowed for a top

    def test_replay_one_record_forming_broken_target(self):
        rows, f = self.load()
        t, o, h, l, c = S._series(rows)
        St, seen = {"since": None, "patterns": {}, "events": []}, []
        for n in range(60, len(t)):
            tt, oo, hh, ll, cc = t[:n + 1], o[:n + 1], h[:n + 1], l[:n + 1], c[:n + 1]
            seen += [(e["status"], f(e["bar_ms"]), e["key"]) for e in S.update_log(St, "XAU", "15m", tt, oo, hh, ll, cc, S.scan(tt, oo, hh, ll, cc), "x")]
        # forming while the right shoulder built, the break reported once the shoulder was confirmed (16:15; the close
        # had gone through at 15:15), target reached at 18:45 -- one record, nothing else in these two days
        self.assertEqual([x[:2] for x in seen], [("forming", "11-20 14:45"), ("broken", "11-20 16:15"), ("target", "11-20 18:45")])
        self.assertEqual(len({x[2] for x in seen}), 1)


class NecklineRuleTest(unittest.TestCase):
    def tilt(self, kind, d):
        """A clean pattern whose neckline moves d heights from the left to the right shoulder (+ = up for a top):
        shoulders 19 and head 35 above the neckline wherever they are, 13 candles per leg."""
        k = d * 35 / 52                                   # neckline slope per candle (shoulders are 52 candles apart)
        nk = lambda x: 85 + k * (x - 52)                  # noqa: E731
        pts = [nk(0) - 55, nk(13) - 25, nk(26) + 19, nk(39), nk(52) + 35, nk(65), nk(78) + 19, nk(91) - 10]
        if kind == "inverse":
            pts = [200 - x for x in pts]
        return ohlc(path(pts, 13))

    def found(self, kind, d):
        o, h, l, c = self.tilt(kind, d)
        return [p for P in S.P_SIZES for p in S.find(h, l, c, P) if p["kind"] == kind]

    def test_top_allowed_and_refused(self):
        self.assertTrue(self.found("top", 0.0))           # flat
        self.assertTrue(self.found("top", 0.3))           # slopes up (shoulders at matching heights above the neckline)
        self.assertFalse(self.found("top", -0.3))         # slopes down well past 5 degrees
        self.assertTrue(self.found("top", -0.08))         # 4.6 degrees down: allowed
        self.assertFalse(self.found("top", -0.10))        # 5.7 degrees down: refused

    def test_inverse_mirror(self):
        # in the inverse pattern, d > 0 here means the neckline slopes DOWN (the points are mirrored)
        self.assertTrue(self.found("inverse", 0.0))
        self.assertTrue(self.found("inverse", 0.3))
        self.assertFalse(self.found("inverse", -0.3))

class StateFileTest(unittest.TestCase):
    """The forward log is the live record: a damaged file is kept aside (never overwritten) and the run says so."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.saved = (S.STATE,)
        S.STATE = os.path.join(self.d, "stf_hns_state.json")

    def tearDown(self):
        (S.STATE,) = self.saved

    def test_missing_file_is_a_quiet_fresh_start(self):
        st, problem = S.load_state(log=lambda *a: None)
        self.assertIsNone(st["since"])
        self.assertIsNone(problem)

    def test_damaged_file_is_kept_and_reported(self):
        with open(S.STATE, "w") as f:
            f.write('{"since": "2026-11-20T00:00:00Z", "patterns": {"XAU:15m')    # a cut-off write
        logged = []
        st, problem = S.load_state(log=logged.append, now_s="2026-11-24T05:00:00Z")
        self.assertIsNone(st["since"])
        self.assertIn("unreadable", problem)
        kept = os.path.join(self.d, "stf_hns_state.json.unreadable-2026-11-24T050000Z")
        self.assertTrue(os.path.exists(kept))
        self.assertFalse(os.path.exists(S.STATE))
        with open(kept) as f:
            self.assertIn("XAU:15m", f.read())                   # the original bytes, untouched
        self.assertTrue(logged)

    def test_wrong_shape_is_treated_as_damaged(self):
        with open(S.STATE, "w") as f:
            f.write("[]")
        st, problem = S.load_state(log=lambda *a: None, now_s="2026-11-24T05:00:00Z")
        self.assertEqual(st["patterns"], {})
        self.assertIsNotNone(problem)

    def test_good_file_round_trips(self):
        S.save_json(S.STATE, {"since": "x", "patterns": {"k": {"open": True}}, "events": [1]})
        st, problem = S.load_state(log=lambda *a: None)
        self.assertEqual(st["since"], "x")
        self.assertIsNone(problem)
        self.assertFalse(os.path.exists(S.STATE + ".tmp"))


if __name__ == "__main__":
    unittest.main()
