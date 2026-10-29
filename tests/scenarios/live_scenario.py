import sys, os, sqlite3, json, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin"))
from decimal import Decimal
import live_engine as L

class FakeEx:
    """Minimal Binance spot simulator: market orders fill at the current price, stop/OCO orders trigger on tick()."""
    def __init__(self):
        self.p = {"ETHUSDT": 2000.0}; self.bal = {"USDT": 5000.0, "ETH": 0.5}   # 0.5 ETH = the owner's own holding
        self.orders = {}; self.calls = []
    def sync_time(self): return 0
    def filters(self, s): return {"status": "TRADING", "step": Decimal("0.0001"), "min_qty": Decimal("0.0001"), "tick": Decimal("0.01"),
                                   "min_notional": Decimal("5"), "oco": True, "base": s[:-4], "quote": "USDT"}
    def price(self, s): return self.p[s]
    def qty(self, s, q): return Decimal(str(q)).quantize(Decimal("0.0001"), rounding="ROUND_DOWN")
    def px(self, s, p, up=False): return Decimal(str(p)).quantize(Decimal("0.01"))
    def balances(self): return {k: (v, 0.0) for k, v in self.bal.items()}
    def market_buy_quote(self, s, usdt, cid):
        self.calls.append(("buy", s, usdt)); q = round(usdt / self.p[s], 4); self.bal["USDT"] -= q * self.p[s]; self.bal["ETH"] += q
        return {"executedQty": str(q), "cummulativeQuoteQty": str(q * self.p[s]), "fills": [{"price": str(self.p[s]), "qty": str(q), "commission": "0", "commissionAsset": "BNB"}]}
    def market_sell(self, s, q, cid):
        q = float(q); self.calls.append(("sell", s, q)); assert self.bal["ETH"] >= q - 1e-9, "selling more than held!"
        self.bal["ETH"] -= q; self.bal["USDT"] += q * self.p[s]
        return {"status": "FILLED", "executedQty": str(q), "cummulativeQuoteQty": str(q * self.p[s]), "fills": [{"price": str(self.p[s]), "qty": str(q)}]}
    def stop_sell(self, s, q, stop, lim, cid): self.calls.append(("stop", s, float(q), float(stop))); self.orders[cid] = dict(kind="stop", q=float(q), stop=float(stop), status="NEW")
    def oco_sell(self, s, q, take, stop, lim, lid): self.calls.append(("oco", s, float(q), float(take), float(stop))); self.orders[lid] = dict(kind="oco", q=float(q), take=float(take), stop=float(stop), status="NEW")
    def cancel(self, s, cid): self.orders[cid]["status"] = "CANCELED"; self.calls.append(("cancel", cid))
    def cancel_list(self, s, lid): self.orders[lid]["status"] = "CANCELED"; self.calls.append(("cancel_list", lid))
    def order(self, s, cid):
        from live_exchange import ExchangeError
        leg = None
        if cid in self.orders:
            o = self.orders[cid]
        elif cid[-1:] in ("a", "b") and cid[:-1] in self.orders:
            o, leg = self.orders[cid[:-1]], cid[-1]
        else:
            raise ExchangeError(-2013, "Order does not exist.", 400)
        st = o["status"]
        if o["kind"] == "oco":
            if st == "FILLED_TP":
                return {"status": "FILLED", "executedQty": str(o["q"]), "cummulativeQuoteQty": str(o["q"] * o["take"])} if leg == "a" else {"status": "EXPIRED", "executedQty": "0"}
            if st == "FILLED":
                return {"status": "FILLED", "executedQty": str(o["q"]), "cummulativeQuoteQty": str(o["q"] * o["stop"])} if leg == "b" else {"status": "EXPIRED", "executedQty": "0"}
        if st == "FILLED":
            return {"status": "FILLED", "executedQty": str(o["q"]), "cummulativeQuoteQty": str(o["q"] * o["stop"])}
        return {"status": st if st in ("NEW", "CANCELED") else "EXPIRED", "executedQty": "0"}
    def tick(self, s, price):
        self.p[s] = price
        for k, o in self.orders.items():
            if o["status"] != "NEW": continue
            if o["kind"] == "oco" and price >= o["take"]: o["status"] = "FILLED_TP"; self.bal["ETH"] -= o["q"]; self.bal["USDT"] += o["q"] * o["take"]
            elif price <= o["stop"]: o["status"] = "FILLED"; self.bal["ETH"] -= o["q"]; self.bal["USDT"] += o["q"] * o["stop"]

def fresh(mode="testnet"):
    d = tempfile.mkdtemp(); L.STOP_FILE = os.path.join(d, "STOP"); L.FLAT_FILE = os.path.join(d, "FLAT")
    cfg = dict(L.DEFAULTS, mode=mode, bot="leaders", risk=0.01, max_pos_frac=0.2, capital_usdt=1500, max_order_usdt=600)
    return d, cfg, sqlite3.connect(":memory:")

order = {"symbol": "ETHUSDT", "side": "long", "signal_date": "2026-10-26", "ref_close": 2000.0, "stop_dist": 60.0, "stop_dist_pct": 3.0, "add": False}
# 1) entry + protection
d, cfg, con = fresh(); ex = FakeEx(); E = L.Engine(cfg, ex, con, log=lambda *_: None)
r = E.run({"open": [], "closed": [], "orders": [order]})
print("entry:", [c for c in ex.calls], "| open", r["open"])
pos = E.positions()[0]; assert abs(pos["stop"] - 1940) < 1e-6 and abs(pos["target"] - 2120) < 1e-6
# usdt sized: risk 1% of 1500 = 15 / 60 * 2000 = 500 <= min(300 (20%), 600) -> 300
assert ex.calls[0][0] == "buy" and abs(ex.calls[0][2] - 300) < 1e-6, ex.calls[0]
# 2) same order seen again next hour: no second buy
E2 = L.Engine(cfg, ex, con, log=lambda *_: None); E2.run({"open": [{"symbol": "ETHUSDT", "signal_date": "2026-10-26", "stop": 1940, "partials": []}], "closed": [], "orders": [order]})
assert sum(1 for c in ex.calls if c[0] == "buy") == 1
# 3) price hits +2R: OCO take-profit fills; paper moves stop to break-even
ex.tick("ETHUSDT", 2125)
E3 = L.Engine(cfg, ex, con, log=lambda *_: None)
E3.run({"open": [{"symbol": "ETHUSDT", "signal_date": "2026-10-26", "stop": 2000.0, "partials": [{"share": 0.333}]}], "closed": [], "orders": []})
pos = E3.positions()[0]; print("after TP:", round(pos["qty_open"], 4), "stop", pos["stop"], "partial", pos["partial_done"], "pnl so far", round(pos["pnl_usdt"] or 0, 2))
assert pos["partial_done"] == 1 and abs(pos["stop"] - 2000) < 1e-6
# 4) exit rule -> sell only the bot's quantity, never the owner's 0.5 ETH
E4 = L.Engine(cfg, ex, con, log=lambda *_: None)
E4.run({"open": [{"symbol": "ETHUSDT", "signal_date": "2026-10-26", "stop": 2000.0, "partials": [{"share": 0.333}], "exit_next_open": True}], "closed": [], "orders": []})
print("final ETH balance", round(ex.bal["ETH"], 6), "USDT", round(ex.bal["USDT"], 2), "| closed pnl", [round(x["pnl_usdt"], 2) for x in E4.positions("closed")])
assert abs(ex.bal["ETH"] - 0.5) < 1e-3, "owner's holding was touched"
# 5) gap through the stop
d, cfg, con = fresh(); ex = FakeEx(); E = L.Engine(cfg, ex, con, log=lambda *_: None); E.run({"open": [], "closed": [], "orders": [order]})
ex.p["ETHUSDT"] = 1800.0     # gapped far below stop and stop-limit (no tick fill)
L.Engine(cfg, ex, con, log=lambda *_: None).run({"open": [{"symbol": "ETHUSDT", "signal_date": "2026-10-26", "stop": 1940, "partials": []}], "closed": [], "orders": []})
print("gap exit:", [x["reason"] for x in L.Engine(cfg, ex, con).positions("closed")])
# 6) flatten switch
d, cfg, con = fresh(); ex = FakeEx(); E = L.Engine(cfg, ex, con, log=lambda *_: None); E.run({"open": [], "closed": [], "orders": [order]})
open(L.FLAT_FILE, "w").close(); r = L.Engine(cfg, ex, con, log=lambda *_: None).run({"open": [], "closed": [], "orders": [order]})
print("flatten:", r["stopped"], r["open"], round(ex.bal["ETH"], 4))
# 7) drawdown halt
d, cfg, con = fresh(); cfg["max_drawdown_pct"] = 2; ex = FakeEx(); E = L.Engine(cfg, ex, con, log=lambda *_: None); E.run({"open": [], "closed": [], "orders": [order]})
ex.p["ETHUSDT"] = 1700.0; r = L.Engine(cfg, ex, con, log=lambda *_: None).run({"open": [{"symbol": "ETHUSDT", "signal_date": "2026-10-26", "stop": 1940, "partials": []}], "closed": [], "orders": []})
print("drawdown:", r["stopped"], os.path.exists(L.STOP_FILE))
# 8) shadow mode never calls order endpoints
d, cfg, con = fresh("shadow"); ex = FakeEx(); r = L.Engine(cfg, ex, con, log=lambda *_: None).run({"open": [], "closed": [], "orders": [order]})
print("shadow calls:", ex.calls, "| logged:", r["messages"][:2])
assert not ex.calls
# 9) price drift guard
d, cfg, con = fresh(); ex = FakeEx(); ex.p["ETHUSDT"] = 2100.0; r = L.Engine(cfg, ex, con, log=lambda *_: None).run({"open": [], "closed": [], "orders": [order]})
print("drift:", r["messages"])
print("ALL TESTS PASSED")
