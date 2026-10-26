import sys, sqlite3, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin"))
from decimal import Decimal
import mm_bot as M
c = {"symbol": "XYZUSDT", "band": [0.040, 0.055], "spread_bps": 80, "levels": 2, "level_step_bps": 40, "order_usdt": 50,
     "target_coin_pct": 60, "max_skew_bps": 60, "skew_range_pct": 25, "max_coin": 200000, "min_coin": 20000, "max_usdt": 3000, "max_daily_usdt": 5000}
q = M.plan_quotes(c, 0.048, 75000, 2400)   # coin value 3600 / 6000 = 60% -> no skew
print("balanced:", [(s, round(p, 6)) for s, p, _ in q])
assert all((s == "BUY" and p < 0.048) or (s == "SELL" and p > 0.048) for s, p, _ in q)
qh = M.plan_quotes(c, 0.048, 110000, 1000)  # 84% coin -> lean lower
print("too much coin:", [(s, round(p, 6)) for s, p, _ in qh])
assert max(p for s, p, _ in qh if s == "SELL") < max(p for s, p, _ in q if s == "SELL")
print("outside band:", M.plan_quotes(c, 0.060, 75000, 2400))
assert M.plan_quotes(c, 0.060, 75000, 2400) == []
print("min coin respected:", [s for s, _, _ in M.plan_quotes(c, 0.048, 20500, 2400)])

class FakeMexc:
    def __init__(s): s.orders = {}; s.bal = {"XYZ": (75000, 0), "USDT": (2400, 0)}; s.mid = 0.048; s.trades = []
    def sync_time(s): pass
    def info(s, sym): return {"base": "XYZ", "quote": "USDT", "step": Decimal("1"), "tick": Decimal("0.000001"), "min_quote": Decimal("1"), "trading": True}
    def book(s, sym, n=5): return [(s.mid * 0.999, 1000)], [(s.mid * 1.001, 1000)]
    def balances(s): return s.bal
    def qty(s, sym, q): return Decimal(int(q))
    def px(s, sym, p, up=False): return Decimal(str(round(p, 6)))
    def open_orders(s, sym): return [dict(o, clientOrderId=k) for k, o in s.orders.items()]
    def limit_maker(s, sym, side, qty, price, cid):
        bb, ba = s.book(sym)
        if (side == "BUY" and float(price) >= ba[0][0]) or (side == "SELL" and float(price) <= bb[0][0]): raise Exception("would cross")
        s.orders[cid] = {"side": side, "price": str(price), "qty": str(qty)}
    def cancel(s, sym, cid): s.orders.pop(cid, None)
    def my_trades(s, sym, since): return s.trades
d = tempfile.mkdtemp(); M.STOP = os.path.join(d, "MM_STOP")
ex = FakeMexc(); m = M.MM(ex, {"coins": [c]}, sqlite3.connect(":memory:"), log=lambda *a: None)
m.step(); print("orders after step 1:", sorted((o["side"], o["price"]) for o in ex.orders.values()))
n1 = len(ex.orders); m.step(); assert len(ex.orders) == n1, "no churn when nothing moved"
ex.mid = 0.0485; m.step(); print("after +1% move:", sorted((o["side"], o["price"]) for o in ex.orders.values()))
ex.trades = [{"id": 1, "isBuyer": True, "price": "0.0477", "qty": "1048", "quoteQty": "50"}]; m.step(); m.step()
print("fills recorded:", m.con.execute("SELECT COUNT(*), SUM(quote) FROM fills").fetchone())
open(M.STOP, "w").close(); m.step(); print("after MM_STOP:", len(ex.orders))
assert len(ex.orders) == 0
print("MM TESTS PASSED")
