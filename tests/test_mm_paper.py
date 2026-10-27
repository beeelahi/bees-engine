"""Paper trading for the market maker: the simulated MEXC account, its fill model, and the live grid code on top."""
import os
import random
import shutil
import sqlite3
import tempfile
import unittest
from decimal import Decimal

from tests import _paths  # noqa: F401
import mexc_exchange as X
import mm_paper as P

SYM = "COINUSDT"


class FakePub:
    """Public-only stand-in: exchangeInfo, book and trades set by the test."""

    def __init__(self):
        self.bk, self.tr, self._offset = ([(0.999, 500.0)], [(1.001, 500.0)]), [], 0

    def info(self, s):
        return {"base": "COIN", "quote": "USDT", "step": Decimal("0.01"), "tick": Decimal("0.0001"), "min_quote": Decimal("1"), "trading": True}

    def qty(self, s, q):
        return (Decimal(str(q)) / Decimal("0.01")).to_integral_value(rounding="ROUND_DOWN") * Decimal("0.01")

    def px(self, s, p, up=False):
        return (Decimal(str(p)) / Decimal("0.0001")).to_integral_value(rounding="ROUND_UP" if up else "ROUND_DOWN") * Decimal("0.0001")

    def book(self, s, limit=20):
        return self.bk

    def trades(self, s, limit=1000):
        return list(self.tr)

    def sync_time(self):
        pass


class Clock:
    def __init__(self):
        self.t = 1_000_000

    def __call__(self):
        return self.t


def make(coins=1000.0, usdt=1000.0):
    pub, clock = FakePub(), Clock()
    feed = P.Feed(pub)
    feed.now_ms = clock
    ex = P.PaperEx(feed, sqlite3.connect(":memory:"), clock=clock)
    ex.fund("COIN", coins)
    ex.fund("USDT", usdt)
    feed.refresh(SYM)
    return pub, feed, ex, clock


class PaperExTest(unittest.TestCase):
    def test_post_only_and_balance_checks(self):
        pub, feed, ex, _ = make()
        with self.assertRaises(X.MexcError):
            ex.limit_maker(SYM, "BUY", Decimal("10"), Decimal("1.001"), "bmB1")      # would cross the ask
        with self.assertRaises(X.MexcError):
            ex.limit_maker(SYM, "SELL", Decimal("5000"), Decimal("1.1"), "bmS1")      # more coins than held
        with self.assertRaises(X.MexcError):
            ex.limit_maker(SYM, "BUY", Decimal("0.5"), Decimal("0.9"), "bmB2")        # below the minimum value
        ex.limit_maker(SYM, "SELL", Decimal("100"), Decimal("1.05"), "bmS2")
        self.assertEqual(ex.balances()["COIN"], (900.0, 100.0))
        ex.cancel(SYM, "bmS2")
        self.assertEqual(ex.balances()["COIN"], (1000.0, 0.0))
        self.assertEqual(ex.order(SYM, "bmS2")["status"], "CANCELED")
        with self.assertRaises(X.MexcError):
            ex.cancel(SYM, "bmS2")

    def test_fills_through_and_queue(self):
        pub, feed, ex, clock = make()
        pub.bk = ([(0.999, 500.0)], [(1.001, 500.0), (1.05, 30.0)])
        feed.refresh(SYM)
        clock.t = 2_000_000
        ex.limit_maker(SYM, "SELL", Decimal("100"), Decimal("1.05"), "bmS1")        # 30 coins ahead of us at 1.05
        pub.tr = [(1_500_000, 1.2, 50.0, "BUY")]                                    # printed BEFORE we placed: ignored
        feed.refresh(SYM); ex.tick(SYM)
        self.assertEqual(ex.order(SYM, "bmS1")["status"], "NEW")
        pub.tr += [(2_000_100, 1.05, 20.0, "BUY"), (2_000_200, 1.05, 40.0, "SELL")]   # 20 eats queue; a taker SELL doesn't hit asks
        feed.refresh(SYM); ex.tick(SYM)
        self.assertEqual(ex.order(SYM, "bmS1")["executedQty"], "0")
        pub.tr += [(2_000_300, 1.05, 25.0, "BUY")]                                   # 10 queue left, then 15 for us
        feed.refresh(SYM); ex.tick(SYM)
        o = ex.order(SYM, "bmS1")
        self.assertEqual((o["status"], o["executedQty"]), ("PARTIALLY_FILLED", "15"))
        feed.refresh(SYM); ex.tick(SYM)                                               # same trades again: not counted twice
        self.assertEqual(ex.order(SYM, "bmS1")["executedQty"], "15")
        pub.tr += [(2_000_400, 1.06, 1.0, "BUY")]                                    # traded through: the rest fills
        feed.refresh(SYM); ex.tick(SYM)
        self.assertEqual(ex.order(SYM, "bmS1")["status"], "FILLED")
        b = ex.balances()
        self.assertAlmostEqual(b["COIN"][0] + b["COIN"][1], 900.0)
        self.assertAlmostEqual(b["USDT"][0], 1000.0 + 105.0)                         # maker fee 0

    def test_plain_limit_that_crosses_is_a_taker(self):
        pub, feed, ex, _ = make()
        ex.limit(SYM, "BUY", Decimal("100"), Decimal("1.002"), "bmB1")               # ask is 1.001: crosses
        self.assertEqual(ex.order(SYM, "bmB1")["status"], "FILLED")
        b = ex.balances()
        self.assertAlmostEqual(b["USDT"][0], 1000.0 - 100.2 - 100.2 * 0.0005)
        self.assertAlmostEqual(b["COIN"][0], 1100.0)

    def test_book_crossing_fills(self):
        pub, feed, ex, _ = make()
        ex.limit_maker(SYM, "BUY", Decimal("100"), Decimal("0.99"), "bmB1")
        pub.bk = ([(0.97, 500.0)], [(0.98, 500.0)])                                  # market fell through our bid
        feed.refresh(SYM); ex.tick(SYM)
        self.assertEqual(ex.order(SYM, "bmB1")["status"], "FILLED")

    def test_public_only_client_refuses_signed_calls(self):
        m = X.Mexc(cred=False, transport=lambda *a: (_ for _ in ()).throw(AssertionError("no network in tests")))
        with self.assertRaises(X.MexcError):
            m.account()
        with self.assertRaises(X.MexcError):
            m.limit_maker(SYM, "BUY", 1, 1, "bmX")


class PaperGridTest(unittest.TestCase):
    """The live grid code trades against the paper account on a random walk of public trades."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (P.DATA, P.mm_bot.STOP, P.mm_grid_live.STOP)
        P.DATA = self.tmp
        P.mm_bot.STOP = P.mm_grid_live.STOP = os.path.join(self.tmp, "STOP")

    def tearDown(self):
        P.DATA, P.mm_bot.STOP, P.mm_grid_live.STOP = self.old
        shutil.rmtree(self.tmp)

    def test_grid_runs_and_wallet_stays_consistent(self):
        coin = {"symbol": SYM, "mode": "grid", "step": 0.015, "levels": 8, "size": 50, "floor": 600, "start_coins": 1000,
                "band": [0.5, 2.0], "max_usdt": 500, "buy_rungs": 4, "buy_orders_live": 4, "sell_orders_live": 6}
        self.assertEqual(P.grid_ok(coin), [])
        self.assertEqual([n for n, _ in P.variants(coin)], ["base", "tight", "wide"])
        self.assertEqual(P.variants(coin)[1][1]["step"], 0.01)
        pub, clock = FakePub(), Clock()
        feed = P.Feed(pub)
        feed.now_ms = clock
        rnd, px, logs = random.Random(3), 1.0, []
        pub.bk = ([(px * 0.999, 1e6)], [(px * 1.001, 1e6)])
        feed.refresh(SYM)
        v = P.Variant("base", coin, feed, None, log=logs.append)
        for i in range(1500):
            clock.t += 15_000
            px = max(0.6, min(1.8, px * (1 + rnd.gauss(0, 0.006))))
            pub.bk = ([(px * 0.999, 1e6)], [(px * 1.001, 1e6)])
            pub.tr = [(clock.t - 5_000, px * (1 + rnd.choice((-1, 1)) * 0.002), 1e5, rnd.choice(("BUY", "SELL")))]
            feed.refresh(SYM)
            v.step()
        bal = v.ex.balances()
        for a, (f, l) in bal.items():
            self.assertGreaterEqual(f, -1e-6, a)
            self.assertGreaterEqual(l, -1e-6, a)
        fills = v.con.execute("SELECT side, SUM(qty), SUM(qty*price), SUM(fee_usdt) FROM p_fills GROUP BY side").fetchall()
        f = {s: (q, val, fee) for s, q, val, fee in fills}
        bq, bv, bf = f.get("BUY", (0, 0, 0))
        sq, sv, sf = f.get("SELL", (0, 0, 0))
        self.assertAlmostEqual(sum(bal["COIN"]), 1000 + bq - sq, places=6)
        self.assertAlmostEqual(sum(bal["USDT"]), 500 - bv + sv - bf - sf, places=6)
        rounds = v.con.execute("SELECT rounds FROM grid_meta").fetchone()[0]
        self.assertGreater(rounds, 0)
        self.assertGreaterEqual(sum(bal["COIN"]), coin["floor"] - 1e-6 - 4 * 50)          # rungs may add, sells never below floor
        v.con.close()
        txt = P.report(data=self.tmp)
        self.assertIn("-- base:", txt)
        self.assertIn("completed rounds", txt)


if __name__ == "__main__":
    unittest.main()
