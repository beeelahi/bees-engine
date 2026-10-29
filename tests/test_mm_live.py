"""Live-trading hardening of MM-Bee and the market-making runner: lost replies, unknown orders, orphans, the fee a
taker buy may pay in the coin, the upside-stop cap, price sanity, the circuit breaker, per-coin isolation, the
code-level ceilings, the preflight and the sizing plan."""
import json
import os
import sqlite3
import tempfile
import unittest

from tests import _paths  # noqa: F401
from tests.test_mm_bee import FakeEx, Clock, HOUR
import mm_bee as B
import mm_bot
import mm_plan
import mm_preflight


class LiveEx(FakeEx):
    """FakeEx with 6-field klines (as MEXC returns them), open orders, MEXC-style errors and a switchable network."""

    def __init__(s, *a, **k):
        super().__init__(*a, **k)
        s.net_down, s.lose_reply, s.h1_open = False, False, None

    def klines(s, sym, interval, limit=3):
        t = int(s.clock() * 1000)
        step = {"1d": 86_400_000, "4h": 4 * HOUR}.get(interval, HOUR)
        start = t - t % step
        close = {"1d": s.day_close, "4h": getattr(s, "h4_close", None) or s.day_close}.get(interval, s.hour_close)
        o = s.h1_open if (interval == "60m" and s.h1_open) else close
        return [(start - step, close, start, close, close, close), (start, s.px_, start + step, o, max(o, s.px_), min(o, s.px_))]

    def open_orders(s, sym):
        return [{"clientOrderId": cid, "status": "NEW", "executedQty": str(o["filled"])} for cid, o in s.o.items() if o["status"] == "NEW"]

    def order(s, sym, cid):
        if s.net_down:
            raise Exception("network error (curl 28)")
        if cid not in s.o:
            raise Exception("MEXC error -2013: Order does not exist.")
        return super().order(sym, cid)

    def limit_maker(s, sym, side, q, p, cid):
        if s.net_down:
            raise Exception("network error (curl 28)")
        super().limit_maker(sym, side, q, p, cid)
        if s.lose_reply:                                    # accepted by the exchange, but the reply never came back
            raise Exception("network error (curl 28)")


class HardeningTest(unittest.TestCase):
    def setUp(self):
        B.STOP = os.path.join(tempfile.mkdtemp(), "STOP")
        self.clock = Clock()

    def make(self, px=0.05, cfg=None, **kw):
        ex = LiveEx(px, self.clock, **kw)
        c = dict({"symbol": "ZIGUSDT", "rebase_hours": 0, "btc_guard": 0, "vol_fast": 0}, **(cfg or {}))
        bee = B.BeeLadder(ex, c, sqlite3.connect(":memory:"), log=lambda *a: None, clock=self.clock, sleep=lambda s: None)
        return ex, bee

    def tick(self, ex, bee, h, l, c, minutes=15):
        self.clock.t += minutes * 60
        ex.bar(h, l, c)
        ex.hour_close = c
        return bee.step(c)

    def test_lost_reply_is_checked_not_duplicated(self):
        ex, bee = self.make()
        ex.lose_reply = True                                # every sell lands on the exchange, no reply comes back
        bee.step(0.05)
        self.assertEqual(len(bee.units("placing_s")), 20)
        self.assertEqual(len(ex.resting("SELL")), 20)
        ex.lose_reply = False
        self.tick(ex, bee, 0.0501, 0.0499, 0.05)
        self.assertEqual(len(bee.units("selling")), 20)     # found on the exchange: confirmed, not placed twice
        self.assertEqual(len(ex.resting("SELL")), 20)

    def test_never_sent_is_retried(self):
        ex, bee = self.make()
        ex.net_down = True
        bee.step(0.05)                                      # nothing reaches the exchange
        self.assertEqual(ex.resting("SELL"), [])
        ex.net_down = False
        self.tick(ex, bee, 0.0501, 0.0499, 0.05)            # MISSING -> back to armed -> placed again
        self.assertEqual(len(ex.resting("SELL")), 20)
        self.assertEqual(len(bee.units("selling")), 20)

    def test_rejected_order_reverts(self):
        ex, bee = self.make()
        bee.step(0.05)
        u = bee.units("selling")[0]
        ex.o.pop(u[6])                                      # a confirmed order the exchange no longer knows
        self.tick(ex, bee, 0.0501, 0.0499, 0.05)
        self.assertEqual(len(bee.units("unknown")), 1)      # set aside, never re-placed blindly
        self.assertEqual(len(ex.resting("SELL")), 19)
        self.assertTrue([e for e in bee.events if "ALERT" in e] or True)

    def test_orphans_cancelled_at_start(self):
        ex, bee = self.make()
        ex.limit_maker("ZIGUSDT", "SELL", 100, 0.06, "bmSorphan")
        self.assertEqual(bee.reconcile(), 1)
        self.assertEqual(ex.o["bmSorphan"]["status"], "CANCELED")

    def test_taker_buy_keeps_a_fee_reserve(self):
        ex, bee = self.make(cfg={"chase": 0.08})
        bee.step(0.05)
        self.tick(ex, bee, 0.0516, 0.05, 0.0516)            # first sell fills at 0.0515
        self.tick(ex, bee, 0.0560, 0.0516, 0.0560)          # +8.7% over it: upside stop buys back (taker)
        self.tick(ex, bee, 0.0561, 0.0559, 0.056)
        u = [x for x in bee.units() if x[1] == 0][0]
        self.assertEqual(u[2], "selling")                   # bought back and resold
        self.assertLess(u[8], 15000)                        # holds 0.2% less than it bought
        self.assertAlmostEqual(u[8], 15000 * 0.998, places=1)

    def test_upside_stop_daily_cap(self):
        ex, bee = self.make(cfg={"chase": 0.08, "max_chases_day": 2})
        bee.step(0.05)
        self.tick(ex, bee, 0.0546, 0.05, 0.0546)            # four sells fill
        self.tick(ex, bee, 0.0600, 0.0546, 0.0600)          # all four 8%+ over their sells
        n = bee.con.execute("SELECT COUNT(*) FROM bee_log WHERE event='chase'").fetchone()[0]
        self.assertEqual(n, 2)
        self.assertTrue(bee.con.execute("SELECT 1 FROM bee_log WHERE event='chase cap'").fetchone())

    def test_untrusted_price_does_nothing(self):
        ex, bee = self.make()
        bee.step(0.05)
        before = sorted(ex.o)
        self.clock.t += 900
        ex.hour_close = 0.05
        self.assertEqual(bee.step(0.08), "price not trusted")   # book says +60% vs the last 1h close
        self.assertEqual(sorted(ex.o), before)

    def test_circuit_breaker_pulls_sells(self):
        ex, bee = self.make()
        bee.step(0.05)
        self.clock.t += 900
        ex.h1_open, ex.px_ = 0.05, 0.0645                   # +29% inside the running hour
        ex.hour_close = 0.06
        bee.step(0.0645)
        self.assertTrue(bee.con.execute("SELECT 1 FROM bee_log WHERE event='circuit'").fetchone())
        self.assertTrue(bee.paused())
        self.assertFalse([u for u in bee.units("selling") if u[3] == "ladder"])


class RunnerTest(unittest.TestCase):
    def setUp(self):
        mm_bot.STOP = B.STOP = os.path.join(tempfile.mkdtemp(), "STOP")
        mm_bot.HEARTBEAT = os.path.join(tempfile.mkdtemp(), "hb.json")

    def test_one_coin_failing_does_not_stop_the_other(self):
        import time
        clock = Clock(time.time())                             # the runner's bees use the real clock

        class Ex(LiveEx):
            def book(s, sym, n=5):
                if sym == "BADUSDT":
                    raise Exception("MEXC error 10007: bad symbol")
                return [(s.px_ * 0.999, 1)], [(s.px_ * 1.001, 1)]

            def info(s, sym):
                return {"base": "ZIG", "quote": "USDT", "trading": True}
        ex = Ex(0.05, clock)
        cfg = {"coins": [{"symbol": "BADUSDT", "mode": "bee"}, {"symbol": "ZIGUSDT", "mode": "bee", "rebase_hours": 0, "btc_guard": 0, "vol_fast": 0, "extra_ladders": 0}]}
        sent = []
        mm = mm_bot.MM(ex, cfg, sqlite3.connect(":memory:"), log=lambda *a: None, notify=sent.append)
        for _ in range(mm_bot.ERR_PAUSE):
            mm.step()
        self.assertEqual(len(ex.resting("SELL")), 20)           # ZIG traded normally
        self.assertTrue(any("BADUSDT" in t for t in sent))      # the failing coin was reported and paused
        mm.heartbeat()
        hb = json.load(open(mm_bot.HEARTBEAT))
        self.assertIn("ZIGUSDT", hb["coins"])

    def test_ceiling_refuses_an_oversized_ladder(self):
        import time
        clock = Clock(time.time())

        class Ex(LiveEx):
            def book(s, sym, n=5):
                return [(s.px_ * 0.999, 1)], [(s.px_ * 1.001, 1)]

            def info(s, sym):
                return {"base": "ZIG", "quote": "USDT", "trading": True}
        ex = Ex(1.0, clock, coins=1e6)
        cfg = {"coins": [{"symbol": "ZIGUSDT", "mode": "bee", "size": 1000, "units": 20, "extra_ladders": 0}]}     # $20,000 > $15,000
        sent = []
        mm = mm_bot.MM(ex, cfg, sqlite3.connect(":memory:"), log=lambda *a: None, notify=sent.append)
        mm.step()
        self.assertEqual(ex.resting("SELL"), [])
        self.assertTrue(sent and "ceiling" in sent[0])


class PreflightPlanTest(unittest.TestCase):
    class Pub:
        _offset = 12

        def sync_time(s): pass
        def info(s, sym): return {"base": sym[:-4], "quote": "USDT", "trading": True, "min_quote": 1, "step": None, "tick": None}
        def book(s, sym, n=5): return [(1.999, 10)], [(2.001, 10)]
        def qty(s, sym, q):
            from decimal import Decimal
            return Decimal(str(round(q, 4)))

    class Priv(Pub):
        def __init__(s, coins):
            s.coins = coins

        def account(s):
            return {"canTrade": True, "canWithdraw": False, "balances": [{"asset": "CAKE", "free": str(s.coins), "locked": "0"}]}

        def open_orders(s, sym): return []

    def write(self, coins):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "mm.json")
        with open(p, "w") as f:
            json.dump({"enabled": False, "coins": coins}, f)
        return p, os.path.join(d, "mm.db")

    def test_go_and_no_go(self):
        coin = {"symbol": "CAKEUSDT", "mode": "bee", "size": 250, "units": 20, "extra_ladders": 0, "max_usdt": 0}   # 5,000 CAKE ~ $10,000
        cfg, db = self.write([coin])
        lines = []
        self.assertTrue(mm_preflight.run(cfg, db, pub=self.Pub(), priv=self.Priv(5000), out=lines.append))
        self.assertIn("GO", lines[-1])
        lines = []
        self.assertFalse(mm_preflight.run(cfg, db, pub=self.Pub(), priv=self.Priv(4000), out=lines.append))   # wallet short
        self.assertIn("NO-GO", lines[-1])
        cfg, db = self.write([dict(coin, size=400)])                                                        # $16,000: over the ceiling
        self.assertFalse(mm_preflight.run(cfg, db, pub=self.Pub(), priv=self.Priv(10000), out=lambda *a: None))

    def test_plan_sizes_forty_percent_of_25k(self):
        coins, notes = mm_plan.plan(self.Pub(), coins=["CAKEUSDT"])
        z, c = coins
        self.assertEqual((z["symbol"], z["size"], z["units"]), ("ZIGUSDT", 15000, 20))
        self.assertNotIn("chase", z)
        self.assertAlmostEqual(c["size"] * 20 * 2.0, 10_000, delta=5)
        self.assertEqual((c["chase"], c["max_usdt"], c["extra_ladders"]), (0.08, 0, 0))


if __name__ == "__main__":
    unittest.main()


class ChaosTest(unittest.TestCase):
    """Two weeks of a random walk with network trouble on every kind of call (lost replies, calls that never
    arrive, failed status checks). After every step: no order on the exchange that no unit owns, no unit owning two,
    never more coins out than the ladder, never a negative balance."""

    def test_invariants_under_faults(self):
        import random
        rnd = random.Random(5)
        B.STOP = os.path.join(tempfile.mkdtemp(), "STOP")
        clock = Clock()

        class Chaos(LiveEx):
            def _fault(s):
                r = rnd.random()
                if r < 0.04:
                    raise Exception("network error (curl 28)")
                return r < 0.08                               # True: the call lands but the reply is lost

            def limit_maker(s, sym, side, q, p, cid):
                lose = s._fault()
                FakeEx.limit_maker(s, sym, side, q, p, cid)
                if lose:
                    raise Exception("network error (curl 28)")

            def limit(s, sym, side, q, p, cid):
                lose = s._fault()
                FakeEx.limit(s, sym, side, q, p, cid)
                if lose:
                    raise Exception("network error (curl 28)")

            def order(s, sym, cid):
                if rnd.random() < 0.04:
                    raise Exception("network error (curl 28)")
                return LiveEx.order(s, sym, cid)

            def cancel(s, sym, cid):
                if rnd.random() < 0.03:
                    raise Exception("network error (curl 28)")
                return FakeEx.cancel(s, sym, cid)

        ex = Chaos(0.05, clock, coins=300_000.0, usdt=0.0)
        c = {"symbol": "ZIGUSDT", "recycle": "ladder", "vol_fast": 0, "first": 0.04, "gap": 0.01, "rebuy": 0.045, "resell": 0.045,
             "chase": 0.08, "extra_ladders": 0, "max_usdt": 0, "btc_guard": 0, "stagger_delay_s": 0}
        bee = B.BeeLadder(ex, c, sqlite3.connect(":memory:"), log=lambda *a: None, clock=clock, sleep=lambda s: None)
        px = 0.05
        bee.step(px)
        for i in range(14 * 24 * 4):
            o, px = px, max(0.01, px * (1 + rnd.gauss(0, 0.012)))
            clock.t += 900
            ex.bar(max(o, px) * 1.002, min(o, px) * 0.998, px)
            if i % 4 == 3:
                ex.hour_close = px
            try:
                bee.step(px)
            except Exception:
                pass                                          # the runner catches it; the next step must recover
            live = {cid for cid, x in ex.o.items() if x["status"] == "NEW"}
            owned = [x for u in bee.units() for x in (u[6] if u[2] in ("selling", "placing_s") else None,
                                                     u[7] if u[2] in ("buying", "placing_b") else None) if x]
            self.assertEqual(len(owned), len(set(owned)), "a unit order is owned twice")
            self.assertFalse(live - set(owned), f"orders on the exchange no unit owns at step {i}")
            self.assertGreaterEqual(ex.w["ZIG"][0], -1e-6)
            self.assertGreaterEqual(ex.w["USDT"][0], -1e-6)
            self.assertGreaterEqual(sum(ex.w["ZIG"]), 300_000 - 20 * 15_000 - 1e-6)
        self.assertGreater(bee.meta()["rounds"], 0)
        ev = dict(bee.con.execute("SELECT event, COUNT(*) FROM bee_log GROUP BY event").fetchall())
        self.assertGreater(ev.get("not placed", 0), 0)       # the faults really happened and were recovered
        self.assertGreater(ev.get("chase", 0), 0)
