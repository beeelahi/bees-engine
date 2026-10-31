"""Live Paper U: each shortlisted coin paper-trades the setting the study ranked best for it. The settings must be
exactly the ones the study measured, a running coin keeps its setting, and the runs work end to end (canned data)."""
import itertools
import math
import json
import os
import random
import sys
import tempfile
import time
import unittest
from decimal import Decimal

from tests import _paths  # noqa: F401
sys.path.insert(0, os.path.join(_paths.BIN, "..", "research"))
import mm_bee_tune as TUNE  # noqa: E402
import mm_paper as MP  # noqa: E402
import mm_site  # noqa: E402


class Pub:
    def __init__(s, px=2.0):
        s.bk, s.tr, s._offset, s.now = ([(px * 0.9995, 1e7)], [(px * 1.0005, 1e7)]), [], 0, lambda: int(time.time() * 1000)
        s.hist = []

    def info(s, sym): return {"base": sym[:-4], "quote": "USDT", "step": Decimal("0.01"), "tick": Decimal("0.0001"), "min_quote": Decimal("1"), "trading": True}
    def qty(s, sym, q): return (Decimal(str(q)) / Decimal("0.01")).to_integral_value(rounding="ROUND_DOWN") * Decimal("0.01")
    def px(s, sym, p, up=False): return (Decimal(str(p)) / Decimal("0.0001")).to_integral_value(rounding="ROUND_UP" if up else "ROUND_DOWN") * Decimal("0.0001")
    def book(s, sym, limit=20): return s.bk
    def trades(s, sym, limit=1000): return list(s.tr)
    def klines(s, sym, interval, limit=3):           # real candles from the price history (open time, close, close time, ...)
        ms = {"60m": 3_600_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]
        bars = {}
        for t, p in s.hist:
            k = t - t % ms
            bars.setdefault(k, [k, p, k + ms, p, p, p])
            b = bars[k]
            b[1], b[3], b[4], b[5] = p, b[3], max(b[4], p), min(b[5], p)
        return [tuple(b) for _, b in sorted(bars.items())][-limit:]

    def sync_time(s): pass


class LivePaperU(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.saved = MP.DATA, MP.mm_bot.STOP, MP.mm_bee.STOP
        MP.DATA = self.d
        MP.mm_bot.STOP = MP.mm_bee.STOP = os.path.join(self.d, "STOP")
        self.pub = Pub()
        self.feed = MP.Feed(self.pub)

    def tearDown(self):
        MP.DATA, MP.mm_bot.STOP, MP.mm_bee.STOP = self.saved

    def test_settings_are_the_studys(self):
        for fam, ch, tr in itertools.product(TUNE.FAMILIES, TUNE.CHASE, TUNE.TREND):
            self.assertEqual(MP.u_settings(TUNE.name(fam, ch, tr)), dict(TUNE.settings(fam, ch, tr), chase=ch), TUNE.name(fam, ch, tr))
        with self.assertRaises(ValueError):
            MP.u_settings("pro+moon")

    def test_plan_size_and_kept_setting(self):
        p = os.path.join(self.d, "mm_bee_tune.json")
        with open(p, "w") as f:
            json.dump({"coins": {"AAAUSDT": {"best": "pro+chase15+trend"}, "BTCUSDT": {"best": "v2+chase15"}}}, f)
        self.assertEqual(MP.u_plan(p), {"AAAUSDT": "pro+chase15+trend", "BTCUSDT": "v2+chase15"})
        self.assertEqual(MP.u_plan(os.path.join(self.d, "missing.json")), {})
        self.feed.refresh("AAAUSDT")
        nm, c = MP.u_coin("AAAUSDT", "pro+chase15+trend", self.feed, data=self.d)
        self.assertEqual(nm, "pro+chase15+trend")
        self.assertAlmostEqual(c["paper_coins"] * 2.0, 10000, delta=1)          # 40% of $25K
        self.assertEqual((c["units"], c["chase"], c["first"], c["regimes"]["up"]["first"]), (20, 0.15, 0.04, 0.08))
        self.assertEqual(c["paper_usdt"], 0.0)
        self.assertNotEqual(c["btc_guard"], 0)
        self.assertEqual(MP.u_coin("BTCUSDT", "v2+chase15", self.feed, data=self.d)[1]["btc_guard"], 0)
        v = MP.Variant("u_aaausdt", dict(c, drip_max=0, btc_guard=0), self.feed, None, log=lambda *a: None)
        v.ex.set_meta("u_setting", nm)
        v.con.commit()
        v.con.close()
        self.assertEqual(MP.u_coin("AAAUSDT", "v2", self.feed, data=self.d)[0], "pro+chase15+trend")   # the study changed; the run did not

    def test_runs_end_to_end_and_publishes(self):
        clock = [int(time.time() * 1000) - 8 * 86_400_000]
        self.feed.now_ms = self.pub.now = lambda: clock[0]
        self.feed.refresh("AAAUSDT")
        nm, c = MP.u_coin("AAAUSDT", "pro+chase8+trend", self.feed, data=self.d)
        v = MP.Variant("u_aaausdt", dict(c, drip_max=0, btc_guard=0, vol_fast=0), self.feed, None, log=lambda *a: None)
        v.ex.set_meta("u_setting", nm)
        rnd = random.Random(3)
        for i in range(7 * 24 * 4):                                     # a week: trend mode needs 30 four-hour candles
            clock[0] += 900_000
            px = 2.0 * (1 + 0.15 * math.sin(i / 12))                     # swings of +-15%
            self.pub.bk = ([(px * 0.9995, 1e7)], [(px * 1.0005, 1e7)])
            self.pub.tr = [(clock[0] - 1000, px * (1 + rnd.choice((-1, 1)) * 0.004), 1e6, rnd.choice(("BUY", "SELL")))]
            self.pub.hist.append((clock[0], px))
            self.feed.refresh("AAAUSDT")
            v.step()
            if i % 4 == 0:
                v.ex.set_meta("snap_t", 0)
        st = v.ex.meta("start")
        self.assertEqual(st["settings"]["chase"], 0.08)
        self.assertIn("regimes", st["settings"])
        self.assertTrue(v.con.execute("SELECT COUNT(*) FROM bee_log WHERE event='regime'").fetchone()[0])   # trend mode read the 4h candles
        self.assertTrue(v.con.execute("SELECT COUNT(*) FROM bee_log WHERE note LIKE 'up %'").fetchone()[0])  # and saw the rally
        v.con.close()
        with open(os.path.join(self.d, "mm_bee_tune.json"), "w") as f:
            json.dump({"coins": {"AAAUSDT": {"best": "v2", "start_utc": "x", "results": {"pro+chase8+trend": {"real": {"vs_hold": 1}, "mirror": {"vs_hold": 2}}, "v2": {}}}}}, f)
        out = mm_site.build(self.d)
        u = out["variants"]["u_aaausdt"]
        self.assertEqual(u["setup"], "pro+chase8+trend")
        self.assertGreater(len(u["snaps"]), 10)
        self.assertNotIn("fill_list", u)
        self.assertGreater(u["fills"], 0)                                   # it traded
        self.assertIn("pro+chase8+trend", out["tune"]["coins"]["AAAUSDT"]["results"])     # its own backtest is published


if __name__ == "__main__":
    unittest.main()
