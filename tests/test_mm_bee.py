"""MM-Bee (sell-first ladder from the daily close) against a simulated exchange, and on the paper account."""
import os
import random
import shutil
import sqlite3
import tempfile
import unittest
from decimal import Decimal

from tests import _paths  # noqa: F401
import mm_bee as B

DAY, HOUR = 86_400_000, 3_600_000


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeEx:
    """Exchange stand-in: wallet with free / locked coins, post-only checks, fills on a candle's high / low."""

    def __init__(s, px, clock, coins=600_000.0, usdt=10_000.0, day_close=None, hour_close=None):
        s.px_, s.clock, s.o = px, clock, {}
        s.w = {"ZIG": [coins, 0.0], "USDT": [usdt, 0.0]}
        s.day_close, s.hour_close = day_close or px, hour_close or px

    def info(s, sym):
        return {"base": "ZIG", "quote": "USDT"}

    def qty(s, sym, q): return Decimal(str(round(float(q), 2)))
    def px(s, sym, p, up=False): return Decimal(str(round(float(p), 7)))

    def klines(s, sym, interval, limit=3):
        t = int(s.clock() * 1000)
        step = DAY if interval == "1d" else HOUR
        start = t - t % step
        close = s.day_close if interval == "1d" else s.hour_close
        return [(start - step, close, start), (start, s.px_, start + step)]   # last one is still open

    def balances(s):
        return {a: (f, l) for a, (f, l) in s.w.items()}

    def _lock(s, side, q, p):
        a, amt = ("ZIG", q) if side == "SELL" else ("USDT", q * p)
        if s.w[a][0] + 1e-9 < amt:
            raise Exception("insufficient balance")
        s.w[a][0] -= amt
        s.w[a][1] += amt

    def limit_maker(s, sym, side, q, p, cid):
        p, q = float(p), float(q)
        if (side == "SELL" and p <= s.px_) or (side == "BUY" and p >= s.px_):
            raise Exception("post-only would cross")
        s._lock(side, q, p)
        s.o[cid] = dict(side=side, price=p, qty=q, status="NEW", filled=0.0)

    def limit(s, sym, side, q, p, cid):
        p, q = float(p), float(q)
        s._lock(side, q, p)
        s.o[cid] = dict(side=side, price=p, qty=q, status="NEW", filled=0.0)
        s.match(cid, s.px_, s.px_)

    def cancel(s, sym, cid):
        o = s.o.get(cid)
        if o and o["status"] == "NEW":
            o["status"] = "CANCELED"
            if o["side"] == "SELL":
                s.w["ZIG"][0] += o["qty"]; s.w["ZIG"][1] -= o["qty"]
            else:
                s.w["USDT"][0] += o["qty"] * o["price"]; s.w["USDT"][1] -= o["qty"] * o["price"]

    def order(s, sym, cid):
        o = s.o[cid]
        return {"status": o["status"], "executedQty": str(o["filled"]), "price": str(o["price"])}

    def match(s, cid, h, l):
        o = s.o[cid]
        if o["status"] != "NEW":
            return
        q, p = o["qty"], o["price"]
        if o["side"] == "SELL" and h >= p:
            o.update(status="FILLED", filled=q)
            s.w["ZIG"][1] -= q; s.w["USDT"][0] += q * p
        elif o["side"] == "BUY" and l <= p:
            o.update(status="FILLED", filled=q)
            s.w["USDT"][1] -= q * p; s.w["ZIG"][0] += q

    def bar(s, h, l, c):
        for cid in list(s.o):
            s.match(cid, h, l)
        s.px_ = c

    def resting(s, side):
        return sorted(o["price"] for o in s.o.values() if o["status"] == "NEW" and o["side"] == side)


class BeeTest(unittest.TestCase):
    def setUp(self):
        B.STOP = os.path.join(tempfile.mkdtemp(), "STOP")
        self.clock = Clock()

    def make(self, px=0.05, **kw):
        ex = FakeEx(px, self.clock, **kw)
        bee = B.BeeLadder(ex, {"symbol": "ZIGUSDT"}, sqlite3.connect(":memory:"), log=lambda *a: None, clock=self.clock)
        return ex, bee

    def tick(self, ex, bee, h, l, c, minutes=15):
        self.clock.t += minutes * 60
        ex.bar(h, l, c)
        return bee.step(c)

    def test_ladder_is_bilals_example(self):
        """Close 0.05: sells at 0.0515, 0.0525, 0.0535 ... (3% above, then 2% of the close apart), 20 x 15,000 ZIG."""
        ex, bee = self.make(0.05)
        bee.step(0.05)
        sells = ex.resting("SELL")
        self.assertEqual(len(sells), 20)
        self.assertEqual(sells[:3], [0.0515, 0.0525, 0.0535])
        self.assertAlmostEqual(sells[-1], 0.05 * (1.03 + 0.02 * 19), places=9)
        self.assertEqual(ex.w["ZIG"][1], 300_000)                 # 300,000 ZIG on the book, the rest untouched
        self.assertEqual(ex.resting("BUY"), [])                   # sell first: no buy before a sell

    def test_prev_gap_option(self):
        self.assertAlmostEqual(B.ladder_prices(0.05, dict(B.DEFAULTS, gap_from="prev"))[2], 0.05 * 1.03 * 1.02 ** 2)

    def test_up_seven_percent_then_round_trip(self):
        ex, bee = self.make(0.05)
        bee.step(0.05)
        self.tick(ex, bee, 0.0535, 0.05, 0.0535)                  # +7%: three sells fill
        m = bee.meta()
        self.assertAlmostEqual(m["proceeds_usdt"], 15000 * (0.0515 + 0.0525 + 0.0535), places=6)
        buys = ex.resting("BUY")
        self.assertEqual(len(buys), 3)
        for s, b in zip([0.0515, 0.0525, 0.0535], buys):
            self.assertAlmostEqual(b, round(s * 0.969, 7), places=9)   # 3.1% under each sell
        self.tick(ex, bee, 0.0535, 0.0498, 0.0498)                 # back down: all three buy back
        m = bee.meta()
        self.assertEqual(m["rounds"], 3)
        self.assertAlmostEqual(m["profit_usdt"], 15000 * 0.031 * (0.0515 + 0.0525 + 0.0535), places=4)
        resells = [p for p in ex.resting("SELL") if p < 0.0545]     # the ladder's unsold part starts at 0.0545
        self.assertEqual(len(resells), 3)                           # each bought unit sells again 3% higher
        self.assertAlmostEqual(resells[0], round(round(0.0515 * 0.969, 7) * 1.03, 7), places=9)
        self.assertEqual(ex.w["ZIG"][0] + ex.w["ZIG"][1], 600_000)  # every coin back

    def test_all_sold_waits_for_buys(self):
        ex, bee = self.make(0.05)
        bee.step(0.05)
        self.tick(ex, bee, 0.10, 0.09, 0.10)                       # runs through the whole ladder
        self.assertEqual(len(bee.units("buying")), 20)
        self.assertEqual(ex.resting("SELL"), [])
        for _ in range(8):                                         # hours pass, no buys: it simply waits
            self.tick(ex, bee, 0.10, 0.099, 0.10, minutes=60)
        self.assertEqual(len(bee.units("buying")), 20)
        self.assertEqual(bee.meta()["ladders"], 1)

    def test_idle_12h_revises_base(self):
        ex, bee = self.make(0.05)
        bee.step(0.05)
        ex.hour_close = 0.045
        for _ in range(11):
            self.tick(ex, bee, 0.0451, 0.0449, 0.045, minutes=60)
        self.assertAlmostEqual(bee.meta()["base"], 0.05)
        r = self.tick(ex, bee, 0.0451, 0.0449, 0.045, minutes=60)
        self.assertEqual(r, "revised")
        self.assertAlmostEqual(bee.meta()["base"], 0.045)
        bee.step(0.045)
        sells = ex.resting("SELL")
        self.assertEqual(len(sells), 20)
        self.assertAlmostEqual(sells[0], 0.045 * 1.03, places=7)

    def test_sold_out_and_idle_opens_one_new_ladder(self):
        ex, bee = self.make(0.05)
        bee.step(0.05)
        self.tick(ex, bee, 0.10, 0.09, 0.10)                       # all 300,000 sold
        ex.hour_close = 0.10
        for _ in range(13):
            self.tick(ex, bee, 0.10, 0.099, 0.10, minutes=60)
        self.assertEqual(bee.meta()["ladders"], 2)
        self.assertAlmostEqual(bee.meta()["base"], 0.10)
        self.assertEqual(len(ex.resting("SELL")), 20)
        self.assertAlmostEqual(ex.resting("SELL")[0], 0.103, places=7)
        self.tick(ex, bee, 0.20, 0.19, 0.20)                       # sold out again
        for _ in range(26):
            self.tick(ex, bee, 0.20, 0.199, 0.20, minutes=60)
        self.assertEqual(bee.meta()["ladders"], 2)                 # never more than one extra ladder
        self.assertGreaterEqual(ex.w["ZIG"][0], -1e-9)

    def test_random_walk_limits_hold(self):
        ex, bee = self.make(0.05)
        rnd, px = random.Random(5), 0.05
        bee.step(px)
        for i in range(6000):
            o = px
            px = max(0.02, px * (1 + rnd.gauss(0, 0.006)))
            h, l = max(o, px) * 1.002, min(o, px) * 0.998
            if i % 4 == 0:
                ex.hour_close = px
            self.tick(ex, bee, h, l, px)
            for a, (f, lk) in ex.w.items():
                self.assertGreaterEqual(f, -1e-6, a)
                self.assertGreaterEqual(lk, -1e-6, a)
            m = bee.meta()
            self.assertLessEqual(m["spent_usdt"] + sum(u[8] * u[5] for u in bee.units("buying")) - m["proceeds_usdt"], 10_000 + 1e-6)
            self.assertLessEqual(sum(1 for u in bee.units()), 40)
        m = bee.meta()
        self.assertGreater(m["rounds"], 0)
        self.assertGreater(m["profit_usdt"], 0)

    def test_kill_switch(self):
        ex, bee = self.make(0.05)
        bee.step(0.05)
        open(B.STOP, "w").close()
        bee.step(0.05)
        self.assertEqual(ex.resting("SELL") + ex.resting("BUY"), [])

    def test_not_enough_coins(self):
        ex, bee = self.make(0.05, coins=100_000)
        self.assertEqual(bee.step(0.05), "not enough coins")
        self.assertEqual(ex.resting("SELL"), [])


class BeePaperTest(unittest.TestCase):
    """MM-Bee through mm_bot on the paper account (the same path the server runs)."""

    def test_paper_variant(self):
        import mm_paper as P
        from tests.test_mm_paper import FakePub, Clock as PClock
        tmp = tempfile.mkdtemp()
        old = (P.DATA, P.mm_bot.STOP, P.mm_bee.STOP)
        try:
            P.DATA = tmp
            P.mm_bot.STOP = P.mm_bee.STOP = os.path.join(tmp, "STOP")
            pub, clock = FakePub(), PClock()
            pub.klines = lambda s, i, limit=3: [(0, 1.0, 1)]
            feed = P.Feed(pub)
            feed.now_ms = clock
            pub.bk = ([(0.999, 1e6)], [(1.001, 1e6)])
            feed.refresh("COINUSDT")
            bc = P.bee_coin({"symbol": "COINUSDT"}, {"bee": {"size": 10, "units": 5}})
            self.assertEqual(bc["paper_coins"], 100)
            self.assertIsNone(P.bee_coin({"symbol": "X", "bee": False}, {}))
            v = P.Variant("bee", bc, feed, None, log=lambda *a: None)
            v.step()
            sells = v.con.execute("SELECT price FROM p_orders WHERE side='SELL' AND status='NEW' ORDER BY price").fetchall()
            self.assertEqual([round(x[0], 4) for x in sells], [1.03, 1.05, 1.07, 1.09, 1.11])
            clock.t += 15_000
            pub.tr = [(clock.t - 1000, 1.06, 1e6, "BUY")]                 # a trade through 1.03 and 1.05
            pub.bk = ([(1.059, 1e6)], [(1.061, 1e6)])
            feed.refresh("COINUSDT")
            v.step()
            buys = v.con.execute("SELECT price FROM p_orders WHERE side='BUY' AND status='NEW' ORDER BY price").fetchall()
            self.assertEqual([round(x[0], 5) for x in buys], [0.998, 1.0174])
            v.con.close()
            d = P.data_dump(tmp)
            self.assertEqual(len(d["variants"]["bee"]["bee_units"]), 5)
            self.assertIn("-- bee:", P.report(data=tmp))
            import mm_site, json as J
            site = mm_site.build(tmp)
            b = site["variants"]["bee"]
            self.assertEqual(len(b["units"]), 5)
            self.assertEqual(b["bee"]["ladders"], 1)
            self.assertEqual(len([o for o in b["orders"] if o[0] == "BUY"]), 2)
            out = tempfile.mkdtemp()
            self.assertTrue(mm_site.export(out, data=tmp))
            self.assertFalse(mm_site.export(out, data=tmp))                 # unchanged: not rewritten
            self.assertTrue(J.load(open(os.path.join(out, "mmbee.json")))["paper"])
            shutil.rmtree(out)
        finally:
            P.DATA, P.mm_bot.STOP, P.mm_bee.STOP = old
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
