"""Live bot engine (Binance spot) hardening: the defects found in the independent review of 30 Oct 2026, each
reproduced against a stricter fake exchange (order lookup by client id, coins locked by resting orders, the owner's
own holding in the same wallet, Binance-style errors)."""
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal

from tests import _paths  # noqa: F401
import live_engine as L
from live_exchange import ExchangeError


class Ex:
    def __init__(s, owner_eth=0.5):
        s.p = {"ETHUSDT": 2000.0, "BTCUSDT": 60000.0}
        s.free = {"USDT": 5000.0, "ETH": owner_eth, "BTC": 0.0}
        s.locked = {"USDT": 0.0, "ETH": 0.0, "BTC": 0.0}
        s.o, s.calls, s.fail = {}, [], {}

    def _maybe_fail(s, what):
        f = s.fail.get(what)
        if f:
            if f.get("times", 1) <= 0:
                return None
            f["times"] = f.get("times", 1) - 1
            return f
        return None

    def sync_time(s): return 0
    def filters(s, sym): return {"status": "TRADING", "step": Decimal("0.0001"), "min_qty": Decimal("0.0001"), "tick": Decimal("0.01"),
                                 "min_notional": Decimal("5"), "oco": True, "base": sym[:-4], "quote": "USDT"}
    def price(s, sym): return s.p[sym]
    def qty(s, sym, q): return Decimal(str(q)).quantize(Decimal("0.0001"), rounding="ROUND_DOWN")
    def px(s, sym, p, up=False): return Decimal(str(p)).quantize(Decimal("0.01"))
    def balances(s): return {k: (s.free[k], s.locked[k]) for k in s.free}

    def order(s, sym, cid):
        if cid not in s.o:
            raise ExchangeError(-2013, "Order does not exist.", 400)
        o = s.o[cid]
        return {"status": o["status"], "executedQty": str(o["exq"]), "cummulativeQuoteQty": str(o["exq"] * o["px"])}

    def market_buy_quote(s, sym, usdt, cid):
        s.calls.append(("buy", sym, usdt))
        f = s._maybe_fail("buy")
        if f and f.get("before"):
            raise ExchangeError("network", "curl exit 28")
        base = sym[:-4]
        q = round(usdt / s.p[sym], 4) if not (f and f.get("zero")) else 0.0
        s.free["USDT"] -= q * s.p[sym]
        s.free[base] += q
        s.o[cid] = {"status": "FILLED" if q else "EXPIRED", "exq": q, "px": s.p[sym], "side": "BUY", "kind": "mkt"}
        if f and f.get("lost"):
            raise ExchangeError("network", "curl exit 28")
        return {"status": s.o[cid]["status"], "executedQty": str(q), "cummulativeQuoteQty": str(q * s.p[sym]),
                "fills": [{"price": str(s.p[sym]), "qty": str(q), "commission": "0", "commissionAsset": "BNB"}] if q else []}

    def market_sell(s, sym, q, cid):
        q = float(q)
        base = sym[:-4]
        s.calls.append(("sell", sym, q))
        assert s.free[base] >= q - 1e-9, "selling coins that are locked or absent"
        f = s._maybe_fail("sell")
        s.free[base] -= q
        s.free["USDT"] += q * s.p[sym]
        s.o[cid] = {"status": "FILLED", "exq": q, "px": s.p[sym], "side": "SELL", "kind": "mkt"}
        if f and f.get("lost"):
            raise ExchangeError("network", "curl exit 28")
        return {"status": "FILLED", "executedQty": str(q), "cummulativeQuoteQty": str(q * s.p[sym]), "fills": [{"price": str(s.p[sym]), "qty": str(q)}]}

    def _lock(s, sym, q):
        base = sym[:-4]
        if s.free[base] + 1e-12 < q:
            raise ExchangeError(-2010, "Account has insufficient balance for requested action.", 400)
        s.free[base] -= q
        s.locked[base] += q

    def stop_sell(s, sym, q, stop, lim, cid):
        s.calls.append(("stop", sym, float(q), float(stop)))
        if s._maybe_fail("stop"):
            raise ExchangeError(-2010, "Order would immediately trigger.", 400)
        s._lock(sym, float(q))
        s.o[cid] = {"status": "NEW", "exq": 0.0, "px": float(stop), "q": float(q), "stop": float(stop), "kind": "stop", "sym": sym}

    def oco_sell(s, sym, q, take, stop, lim, lid):
        s.calls.append(("oco", sym, float(q), float(take), float(stop)))
        if s._maybe_fail("oco"):
            raise ExchangeError(-2010, "Order would immediately trigger.", 400)
        s._lock(sym, float(q))
        s.o[lid] = {"status": "NEW", "kind": "oco", "q": float(q), "sym": sym, "exq": 0.0, "px": 0.0}
        s.o[lid + "a"] = {"status": "NEW", "exq": 0.0, "px": float(take), "q": float(q), "take": float(take), "kind": "tp", "list": lid, "sym": sym}
        s.o[lid + "b"] = {"status": "NEW", "exq": 0.0, "px": float(stop), "q": float(q), "stop": float(stop), "kind": "stop", "list": lid, "sym": sym}

    def _release(s, o, sym):
        if o["status"] == "NEW":
            s.locked[sym[:-4]] -= o["q"] - o["exq"]
            s.free[sym[:-4]] += o["q"] - o["exq"]

    def cancel(s, sym, cid):
        s.calls.append(("cancel", cid))
        if s._maybe_fail("cancel"):
            raise ExchangeError("network", "curl exit 28")
        o = s.o.get(cid)
        if not o or o["status"] != "NEW":
            raise ExchangeError(-2011, "Unknown order sent.", 400)
        s._release(o, sym)
        o["status"] = "CANCELED"

    def cancel_list(s, sym, lid):
        s.calls.append(("cancel_list", lid))
        if s._maybe_fail("cancel"):
            raise ExchangeError("network", "curl exit 28")
        a, b = s.o.get(lid + "a"), s.o.get(lid + "b")
        if not a or a["status"] != "NEW":
            raise ExchangeError(-2011, "Unknown order list.", 400)
        s._release(a, sym)
        a["status"] = b["status"] = "CANCELED"
        s.o[lid]["status"] = "CANCELED"

    def tick(s, sym, price, part=1.0):
        """Move the price; take-profit / stop legs that it reaches fill (by `part` of their quantity)."""
        s.p[sym] = price
        base = sym[:-4]
        for cid, o in list(s.o.items()):
            if o.get("sym") != sym or o["status"] != "NEW" or o["kind"] not in ("tp", "stop"):
                continue
            hit = (o["kind"] == "tp" and price >= o["take"]) or (o["kind"] == "stop" and price <= o["stop"])
            if not hit:
                continue
            q = o["q"] * part
            o["exq"] = q
            s.locked[base] -= q
            s.free["USDT"] += q * o["px"]
            if part >= 1.0:
                o["status"] = "FILLED"
                if o.get("list"):                       # the other leg of the OCO expires (the legs share one lock)
                    other = s.o[o["list"] + ("b" if cid.endswith("a") else "a")]
                    other["status"] = "EXPIRED"
            else:
                o["status"] = "PARTIALLY_FILLED"


ORDER = {"symbol": "ETHUSDT", "side": "long", "signal_date": "2026-10-26", "ref_close": 2000.0, "stop_dist": 60.0, "add": False}
OPEN = {"symbol": "ETHUSDT", "signal_date": "2026-10-26", "stop": 1940.0, "partials": []}


class LiveEngineTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        L.STOP_FILE, L.FLAT_FILE = os.path.join(d, "STOP"), os.path.join(d, "FLAT")
        self.cfg = dict(L.DEFAULTS, mode="testnet", bot="leaders", risk=0.01, max_pos_frac=0.2, capital_usdt=1500, max_order_usdt=600)
        self.con = sqlite3.connect(":memory:")

    def run_(self, ex, paper, cfg=None):
        return L.Engine(cfg or self.cfg, ex, self.con, log=lambda *a: None).run(paper)

    def E(self, ex):
        return L.Engine(self.cfg, ex, self.con, log=lambda *a: None)

    def test_lost_buy_reply_never_buys_twice(self):
        ex = Ex()
        ex.fail["buy"] = {"lost": True}
        self.run_(ex, {"orders": [ORDER]})
        self.run_(ex, {"orders": [ORDER]})
        self.assertEqual(sum(1 for c in ex.calls if c[0] == "buy"), 1)
        p = self.E(ex).positions()
        self.assertEqual(len(p), 1)                                 # found by its id and booked
        self.assertTrue(any(c[0] == "stop" for c in ex.calls))       # and protected

    def test_buy_that_never_landed_is_retried(self):
        ex = Ex()
        ex.fail["buy"] = {"before": True}
        self.run_(ex, {"orders": [ORDER]})
        self.assertEqual(len(self.E(ex).positions("pending")), 1)
        self.run_(ex, {"orders": [ORDER]})                           # MISSING -> forgotten -> bought now
        self.assertEqual(sum(1 for c in ex.calls if c[0] == "buy"), 2)
        self.assertEqual(len(self.E(ex).positions()), 1)

    def test_reconcile_problem_stops_entries_in_the_same_run(self):
        ex = Ex(owner_eth=0.0)
        self.run_(ex, {"orders": [ORDER]})
        ex.free["ETH"] = ex.locked["ETH"] = 0.0                      # the bot's coins are gone (sold by hand)
        o2 = dict(ORDER, symbol="BTCUSDT", ref_close=60000.0, stop_dist=1800.0, signal_date="2026-10-27")
        self.run_(ex, {"open": [OPEN], "orders": [o2]})
        self.assertTrue(os.path.exists(L.STOP_FILE))
        self.assertFalse(any(c[0] == "buy" and c[1] == "BTCUSDT" for c in ex.calls))

    def test_rejected_protection_is_placed_again(self):
        ex = Ex()
        ex.fail["oco"] = {"times": 1}
        self.run_(ex, {"orders": [ORDER]})
        self.assertIsNone(self.E(ex).positions()[0]["oco_id"])
        self.run_(ex, {"open": [OPEN], "orders": []})
        p = self.E(ex).positions()[0]
        self.assertIsNotNone(p["oco_id"])
        self.assertEqual(ex.o[p["oco_id"] + "a"]["status"], "NEW")
        self.assertEqual(ex.o[p["stop_id"]]["status"], "NEW")

    def test_failed_cancel_keeps_the_position_and_never_sells_locked_or_owner_coins(self):
        ex = Ex()
        self.run_(ex, {"orders": [ORDER]})
        ex.fail["cancel"] = {"times": 2}
        self.run_(ex, {"open": [dict(OPEN, exit_next_open=True)], "orders": []})
        self.assertEqual(len(self.E(ex).positions()), 1)            # not marked closed
        self.assertAlmostEqual(ex.free["ETH"] + ex.locked["ETH"], 0.65, places=4)
        self.run_(ex, {"open": [dict(OPEN, exit_next_open=True)], "orders": []})   # cancels work now
        self.assertEqual(len(self.E(ex).positions()), 0)
        self.assertAlmostEqual(ex.free["ETH"], 0.5, places=4)        # the owner's 0.5 ETH untouched

    def test_live_take_profit_fill_is_booked_even_after_the_paper_partial(self):
        ex = Ex()
        ex.p["ETHUSDT"] = 2050.0
        self.run_(ex, {"orders": [dict(ORDER, stop_dist=40.0)]})
        paper_partial = {"symbol": "ETHUSDT", "signal_date": "2026-10-26", "stop": 2000.0, "partials": [{"share": 0.333}]}
        self.run_(ex, {"open": [paper_partial], "orders": []})
        p = self.E(ex).positions()[0]
        ex.tick("ETHUSDT", p["target"] + 1)                         # the live take-profit fills on the exchange
        self.run_(ex, {"open": [dict(paper_partial, exit_next_open=True)], "orders": []})
        self.assertAlmostEqual(ex.free["ETH"], 0.5, places=4)        # the owner's coins untouched
        self.assertEqual(len(self.E(ex).positions("closed")), 1)

    def test_partial_stop_fill_is_booked(self):
        ex = Ex()
        self.run_(ex, {"orders": [ORDER]})
        before = self.E(ex).positions()[0]["qty_open"]
        ex.tick("ETHUSDT", 1939.0, part=0.5)
        self.run_(ex, {"open": [OPEN], "orders": []})
        after = self.E(ex).positions()[0]["qty_open"]
        self.assertLess(after, before)

    def test_hard_cap_limits_the_usdt_at_work(self):
        ex = Ex()
        ex.free["USDT"] = 50_000
        cfg = dict(self.cfg, max_positions=10, capital_usdt=2000, max_order_usdt=2000, max_pos_frac=0.5)
        orders = [dict(ORDER, symbol=s, ref_close=ex.p[s], stop_dist=ex.p[s] * 0.03, signal_date="2026-10-2" + str(i))
                  for i, s in enumerate(["ETHUSDT", "BTCUSDT"])]
        for k in range(3):
            ex.p["X%dUSDT" % k] = 10.0
            ex.free["X%d" % k] = 0.0
            ex.locked["X%d" % k] = 0.0
            orders.append(dict(ORDER, symbol="X%dUSDT" % k, ref_close=10.0, stop_dist=0.3, signal_date="2026-10-1" + str(k)))
        self.run_(ex, {"orders": orders}, cfg)
        e = L.Engine(cfg, ex, self.con, log=lambda *a: None)
        self.assertLessEqual(e.deployed(), L.HARD_CAP_USDT + 1)
        self.assertLessEqual(len(e.positions()), L.MAX_POSITIONS)

    def test_zero_fill_is_void_and_nothing_crashes(self):
        ex = Ex()
        ex.fail["buy"] = {"zero": True}
        self.run_(ex, {"orders": [ORDER]})
        self.assertEqual(self.E(ex).positions(), [])
        self.run_(ex, {"orders": []})                                 # no ZeroDivisionError

    def test_one_bad_position_does_not_stop_the_others(self):
        ex = Ex()
        o2 = dict(ORDER, symbol="BTCUSDT", ref_close=60000.0, stop_dist=1800.0, signal_date="2026-10-27")
        self.run_(ex, {"orders": [ORDER, o2]})
        real_price = ex.price

        def price(sym):
            if sym == "ETHUSDT":
                raise ExchangeError(-1121, "Invalid symbol.", 400)
            return real_price(sym)
        ex.price = price
        self.run_(ex, {"open": [OPEN], "closed": [{"symbol": "BTCUSDT", "signal_date": "2026-10-27", "reason": "x"}], "orders": []})
        self.assertFalse([p for p in self.E(ex).positions() if p["symbol"] == "BTCUSDT"])   # BTC still exited

    def test_flatten_covers_every_bot_and_stops_entries(self):
        ex = Ex()
        self.run_(ex, {"orders": [ORDER]})
        open(L.FLAT_FILE, "w").close()
        self.run_(ex, {"orders": []}, dict(self.cfg, bot="turbo"))   # a different bot configured now
        self.assertEqual(self.E(ex).positions(), [])
        self.assertTrue(os.path.exists(L.STOP_FILE))

    def test_lost_sell_reply_never_sells_twice(self):
        ex = Ex()
        self.run_(ex, {"orders": [ORDER]})
        ex.fail["sell"] = {"lost": True}
        self.run_(ex, {"open": [dict(OPEN, exit_next_open=True)], "orders": []})
        self.run_(ex, {"open": [dict(OPEN, exit_next_open=True)], "orders": []})
        self.assertEqual(sum(1 for c in ex.calls if c[0] == "sell"), 1)
        self.assertAlmostEqual(ex.free["ETH"], 0.5, places=4)
        self.assertEqual(len(self.E(ex).positions("closed")), 1)


    def test_failed_cancels_never_stack_protection(self):
        ex = Ex()
        self.run_(ex, {"orders": [ORDER]})
        ex.fail["cancel"] = {"times": 2}
        moved = dict(OPEN, stop=1990.0)                              # the paper bot raises the stop
        self.run_(ex, {"open": [moved], "orders": []})
        live = [o for o in ex.o.values() if o["status"] == "NEW" and o["kind"] in ("tp", "stop") and not o.get("list", "").endswith("x")]
        locked = ex.locked["ETH"]
        self.assertLessEqual(locked, 0.15 + 1e-9)                    # never more than the bot's own coins locked
        self.assertTrue(live)
        self.assertAlmostEqual(self.E(ex).positions()[0]["stop"], 1940.0)   # not recorded: the move did not happen
        self.run_(ex, {"open": [moved], "orders": []})               # cancels work now: the move goes through
        p = self.E(ex).positions()[0]
        self.assertAlmostEqual(p["stop"], 1990.0)
        self.assertEqual(ex.o[p["stop_id"]]["stop"], 1990.0)          # and it is on the exchange
        self.assertLessEqual(ex.locked["ETH"], 0.15 + 1e-9)

    def test_lost_sell_is_resolved_even_if_the_exit_reason_goes_away(self):
        ex = Ex()
        self.run_(ex, {"orders": [ORDER]})
        ex.p["ETHUSDT"] = 1890.0                                     # gap below stop and stop-limit
        ex.fail["sell"] = {"lost": True}
        self.run_(ex, {"open": [OPEN], "orders": []})
        ex.p["ETHUSDT"] = 2000.0                                     # price recovers: no exit condition any more
        self.run_(ex, {"open": [dict(OPEN, stop=1990.0)], "orders": []})
        self.assertEqual(self.E(ex).positions(), [])                 # the lost sell was found and booked
        self.assertAlmostEqual(ex.free["ETH"], 0.5, places=4)        # owner's coins neither sold nor locked
        self.assertAlmostEqual(ex.locked["ETH"], 0.0, places=6)

    def test_stop_above_the_price_sells_instead(self):
        ex = Ex()
        self.run_(ex, {"orders": [ORDER]})
        ex.p["ETHUSDT"] = 1995.0                                     # between the old stop and the new one
        self.run_(ex, {"open": [dict(OPEN, stop=2000.0)], "orders": []})
        self.assertEqual(self.E(ex).positions(), [])
        self.assertAlmostEqual(ex.free["ETH"], 0.5, places=4)

    def test_bad_symbol_in_balance_check_does_not_stop_the_run(self):
        ex = Ex()
        o2 = dict(ORDER, symbol="BTCUSDT", ref_close=60000.0, stop_dist=1800.0, signal_date="2026-10-27")
        self.run_(ex, {"orders": [ORDER, o2]})
        real = ex.filters

        def filters(sym):
            if sym == "ETHUSDT":
                raise ExchangeError(-1121, "Invalid symbol.", 400)
            return real(sym)
        ex.filters = filters
        self.run_(ex, {"open": [OPEN], "closed": [{"symbol": "BTCUSDT", "signal_date": "2026-10-27", "reason": "x"}], "orders": []})
        self.assertFalse([p for p in self.E(ex).positions() if p["symbol"] == "BTCUSDT"])


    def test_no_hourly_churn_when_the_stop_covers_everything(self):
        ex = Ex()
        ex.fail["oco"] = {"times": 99}                               # the OCO is always refused
        self.run_(ex, {"orders": [ORDER]})
        for _ in range(3):                                           # at most two more tries to add the OCO back
            self.run_(ex, {"open": [OPEN], "orders": []})
        n = len(ex.calls)
        for _ in range(3):                                           # then no churn at all
            self.run_(ex, {"open": [OPEN], "orders": []})
        self.assertFalse([c for c in ex.calls[n:] if c[0] in ("cancel", "stop", "cancel_list", "oco")])
        p = self.E(ex).positions()[0]
        self.assertAlmostEqual(ex.o[p["stop_id"]]["q"], p["qty_open"], places=4)


if __name__ == "__main__":
    unittest.main()
