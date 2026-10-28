"""The live sell-high / rebuy-lower grid against a simulated MEXC exchange on a deterministic random walk."""
import os
import random
import sqlite3
import tempfile
import unittest
from decimal import Decimal

from tests import _paths  # noqa: F401
import mm_grid_live as GL


def walk(n=4000, start=0.05, seed=7):
    rnd, p, bars = random.Random(seed), start, []
    for _ in range(n):
        o = p
        p = max(0.01, p * (1 + rnd.gauss(0, 0.012)))
        hi, lo = max(o, p) * (1 + abs(rnd.gauss(0, 0.004))), min(o, p) * (1 - abs(rnd.gauss(0, 0.004)))
        bars.append((o, hi, lo, p))
    return bars


class FakeMexc:
    def __init__(s, px):
        s.o, s.px_, s.coins, s.usdt = {}, px, 1_000_000.0, 10_000.0

    def qty(s, sym, q): return Decimal(str(round(float(q), 3)))
    def px(s, sym, p, up=False): return Decimal(str(round(float(p), 7)))

    def limit_maker(s, sym, side, q, p, cid):
        p, q = float(p), float(q)
        if (side == "SELL" and p <= s.px_) or (side == "BUY" and p >= s.px_):
            raise Exception("post-only would cross")
        s.o[cid] = dict(side=side, price=p, qty=q, status="NEW", filled=0.0)

    def limit(s, sym, side, q, p, cid):
        s.o[cid] = dict(side=side, price=float(p), qty=float(q), status="NEW", filled=0.0)
        s.match(cid, s.px_, s.px_)

    def balances(s):
        held = sum(o["price"] * o["qty"] for o in s.o.values() if o["status"] == "NEW" and o["side"] == "BUY")
        return {"USDT": (s.usdt - held, held)}

    def cancel(s, sym, cid):
        if cid in s.o and s.o[cid]["status"] == "NEW":
            s.o[cid]["status"] = "CANCELED"

    def order(s, sym, cid):
        o = s.o[cid]
        return {"status": o["status"], "executedQty": str(o["filled"]), "price": str(o["price"])}

    def match(s, cid, h, l):
        o = s.o[cid]
        if o["status"] != "NEW":
            return
        if o["side"] == "SELL" and h >= o["price"] * 1.001:
            o.update(status="FILLED", filled=o["qty"]); s.coins -= o["qty"]; s.usdt += o["qty"] * o["price"]
        elif o["side"] == "BUY" and l <= o["price"] * 0.999:
            o.update(status="FILLED", filled=o["qty"]); s.coins += o["qty"]; s.usdt -= o["qty"] * o["price"]

    def advance(s, o_, h, l, c):
        for cid in list(s.o):
            s.match(cid, h, l)
        s.px_ = c


class GridLiveTest(unittest.TestCase):
    def setUp(self):
        GL.STOP = os.path.join(tempfile.mkdtemp(), "MM_STOP")
        self.cfg = {"symbol": "ABCUSDT", "step": 0.015, "levels": 20, "size": 15000, "floor": 700000,
                    "start_coins": 1_000_000, "band": [0.02, 0.2], "sell_orders_live": 10, "recenter_steps": 3,
                    "buy_rungs": 15, "buy_orders_live": 15, "max_usdt": 10000}

    def run_grid(self, bars, cfg):
        ex = FakeMexc(bars[0][3])
        g = GL.GridLive(ex, cfg, sqlite3.connect(":memory:"), log=lambda *a: None)
        for b in bars:
            ex.advance(*b)
            g.step(b[3])
            self.assertGreaterEqual(ex.coins, cfg["floor"] - 1e-6, "inventory went below the floor")
            self.assertGreaterEqual(ex.usdt, -1e-6, "USDT balance went negative")
        return ex, g

    def test_limits_hold_and_it_earns(self):
        ex, g = self.run_grid(walk(), self.cfg)
        _, rounds, profit = g.meta()
        self.assertGreater(rounds, 50)
        self.assertGreater(profit, 0)

    def test_kill_switch_cancels_everything(self):
        ex, g = self.run_grid(walk(800), self.cfg)
        open(GL.STOP, "w").close()
        g.step(ex.px_)
        self.assertEqual(sum(1 for o in ex.o.values() if o["status"] == "NEW"), 0)

    def test_rungs_switched_on_later(self):
        bars = walk(3000, seed=3)
        ex = FakeMexc(bars[0][3])
        con = sqlite3.connect(":memory:")
        g = GL.GridLive(ex, dict(self.cfg, buy_rungs=0), con, log=lambda *a: None)
        for b in bars[:1000]:
            ex.advance(*b); g.step(b[3])
        g2 = GL.GridLive(ex, self.cfg, con, log=lambda *a: None)
        for b in bars[1000:]:
            ex.advance(*b); g2.step(b[3])
            self.assertGreaterEqual(ex.coins, self.cfg["floor"] - 1e-6)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM grid_buy").fetchone()[0], 15)

    def test_nearest_levels_stay_resting(self):
        """Regression (ZIG paper, Oct 2026): after levels 1-3 sold and were rebought, the slots stayed with far
        levels 11-13 and price trading back through levels 1-3 earned nothing. The nearest levels must rest."""
        a = 0.04
        ex = FakeMexc(a)
        g = GL.GridLive(ex, dict(self.cfg, buy_rungs=0), sqlite3.connect(":memory:"), log=lambda *x: None)
        g.step(a)
        up = a * 1.015 ** 3 * 1.002
        for px, hi, lo in ((up, up, a), (a * 0.995, up, a * 0.995), (a * 0.995, a, a * 0.99)):
            ex.advance(px, hi, lo, px); g.step(px)
        for _ in range(3):                      # rebuys are filled by now; give it a couple of cycles
            ex.advance(a, a, a, a); g.step(a)
        resting = sorted(lvl for lvl, st, *_ in g.rows("selling"))
        self.assertEqual(resting, list(range(1, 11)), "the ten nearest sell levels should be resting")
        _, r0, _ = g.meta()
        ex.advance(up, up, a, up); g.step(up)
        ex.advance(a * 0.99, up, a * 0.99, a * 0.99); g.step(a * 0.99)
        ex.advance(a * 0.99, a, a * 0.985, a * 0.99); g.step(a * 0.99)
        self.assertGreaterEqual(g.meta()[1] - r0, 3, "price went through levels 1-3 again: three more rounds")

    def test_nearest_rungs_stay_resting(self):
        ex, g = self.run_grid(walk(2500, seed=11), dict(self.cfg, buy_orders_live=4))
        mid = ex.px_
        live = sorted((r[2] for r in g.brows("buying")), reverse=True)
        armed = [r[2] for r in g.brows("armed") if r[2] < mid]
        if live and armed:
            self.assertGreaterEqual(min(live), max(armed) - 1e-12, "an armed rung nearer the market sits idle")


if __name__ == "__main__":
    unittest.main()
