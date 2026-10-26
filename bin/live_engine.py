"""
Live execution engine: mirrors ONE paper bot on Binance spot with real money (Python 3.9 stdlib only).

Modes (~/BinanceData/live.json, "mode"):
  off      nothing happens
  shadow   computes exactly what it would do and logs it; never sends an order and needs no keys
  testnet  real orders on Binance's spot testnet (fake money), keys in ~/BinanceData/.binance_testnet
  live     real orders with real money, keys in ~/BinanceData/.binance_live

What it does each run (after the hourly sync, so the paper bot is current):
  1. Safety: kill switches (STOP_TRADING = no new entries; FLATTEN_NOW = sell every bot position and stop),
     daily-loss and drawdown limits, reconciliation of its own positions against the exchange balances.
  2. Exits: paper position closed or flagged to exit -> cancel protection, market-sell the bot's quantity.
     Price already through the stop but the stop-limit did not fill (gap) -> market-sell.
  3. Stop management: after the partial take-profit fills, the stop on the rest moves to break-even
     (exactly like the paper rules).
  4. Entries: each new paper order for the next open -> market buy sized like the paper bot (same % risk of
     the LIVE allocation), capped by max_order_usdt, max_positions and free USDT, then protective orders on
     the exchange straight away: an OCO on one third (take-profit at +2R / stop) and a stop-limit on the rest.
It only ever sells the quantity it bought itself: other holdings of the same coin are never touched.
Every action is written to data/live.db and sent to Telegram (if set up).
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CFG = os.path.join(ROOT, "live.json")
LDB = os.path.join(ROOT, "data", "live.db")
BOTJ = os.path.join(ROOT, "web_export", "bot.json")
STOP_FILE = os.path.join(ROOT, "STOP_TRADING")
FLAT_FILE = os.path.join(ROOT, "FLATTEN_NOW")

DEFAULTS = {"mode": "off", "bot": "leaders", "capital_usdt": 1500.0, "max_positions": 4, "max_order_usdt": 600.0,
            "max_daily_loss_pct": 5.0, "max_drawdown_pct": 15.0, "stop_limit_buffer_pct": 2.0, "max_entry_drift_pct": 3.0}
HARD_CAP_USDT = 2000.0       # code-level ceiling for the first live weeks (approved cap $1k-2k); raise only by code change

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions(id TEXT PRIMARY KEY, mode TEXT, bot TEXT, symbol TEXT, signal_date TEXT,
    status TEXT, qty REAL, qty_open REAL, entry REAL, cost_usdt REAL, stop0 REAL, stop REAL, target REAL,
    oco_id TEXT, stop_id TEXT, partial_done INTEGER DEFAULT 0, opened_utc TEXT, closed_utc TEXT,
    exit_px REAL, pnl_usdt REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS actions(utc TEXT, mode TEXT, symbol TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS equity(utc TEXT, mode TEXT, equity REAL, realized REAL, unrealized REAL);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_cfg():
    c = dict(DEFAULTS)
    if os.path.exists(CFG):
        with open(CFG) as f:
            c.update(json.load(f))
    if c["mode"] not in ("off", "shadow", "testnet", "live"):
        raise RuntimeError("live.json: mode must be off, shadow, testnet or live")
    c["capital_usdt"] = min(float(c["capital_usdt"]), HARD_CAP_USDT)
    c["max_order_usdt"] = min(float(c["max_order_usdt"]), c["capital_usdt"])
    return c


class Engine:
    def __init__(self, cfg, ex, con, log=print, notify=None):
        self.c, self.ex, self.con, self.log = cfg, ex, con, log
        self.notify = notify or (lambda t: None)
        self.mode = cfg["mode"]
        self.shadow = self.mode == "shadow"
        con.executescript(SCHEMA)
        self.msgs = []

    # ------------------------------------------------------------ bookkeeping
    def act(self, sym, action, detail):
        d = json.dumps(detail, default=str)
        self.con.execute("INSERT INTO actions VALUES(?,?,?,?,?)", (now(), self.mode, sym, action, d))
        self.con.commit()
        self.log(f"live[{self.mode}] {sym} {action} {d}")
        self.msgs.append(f"{'[SHADOW] ' if self.shadow else ''}{sym.replace('USDT', '')}: {action} {self._short(detail)}")

    @staticmethod
    def _short(d):
        keep = ("qty", "price", "stop", "target", "usdt", "pnl_usdt", "reason")
        return ", ".join(f"{k} {d[k]}" for k in keep if k in d)

    def positions(self, status="open"):
        cols = [c[1] for c in self.con.execute("PRAGMA table_info(positions)")]
        return [dict(zip(cols, r)) for r in self.con.execute(
            "SELECT * FROM positions WHERE mode=? AND bot=? AND status=?", (self.mode, self.c["bot"], status))]

    def realized(self):
        return self.con.execute("SELECT COALESCE(SUM(pnl_usdt),0) FROM positions WHERE mode=? AND bot=? AND status='closed'",
                                (self.mode, self.c["bot"])).fetchone()[0]

    def equity(self):
        unreal = 0.0
        for p in self.positions():
            unreal += p["qty_open"] * self.ex.price(p["symbol"]) - p["cost_usdt"] * (p["qty_open"] / p["qty"])
        r = self.realized()
        return self.c["capital_usdt"] + r + unreal, r, unreal

    # ------------------------------------------------------------ order helpers (shadow-aware)
    def _send(self, fn, *a):
        if self.shadow:
            return {"shadow": True}
        return fn(*a)

    def protect(self, p):
        """Exchange-side protection for an open position: OCO on one third (+2R / stop), stop-limit on the rest."""
        sym, buf = p["symbol"], self.c["stop_limit_buffer_pct"] / 100.0
        third = self.ex.qty(sym, p["qty_open"] / 3.0) if not p["partial_done"] else 0
        rest = self.ex.qty(sym, p["qty_open"] - float(third))
        stop = self.ex.px(sym, p["stop"])
        stop_lim = self.ex.px(sym, p["stop"] * (1 - buf))
        oco_id = stop_id = None
        if third > 0 and not p["partial_done"]:
            oco_id = f"bo{int(time.time())}{sym[:6]}"[:30]
            self._send(self.ex.oco_sell, sym, third, self.ex.px(sym, p["target"], up=True), stop, stop_lim, oco_id)
        if rest > 0:
            stop_id = f"bs{int(time.time())}{sym[:6]}"[:30]
            self._send(self.ex.stop_sell, sym, rest, stop, stop_lim, stop_id)
        self.con.execute("UPDATE positions SET oco_id=?, stop_id=? WHERE id=?", (oco_id, stop_id, p["id"]))
        self.con.commit()
        self.act(sym, "protected", {"stop": float(stop), "target": p["target"], "oco_qty": float(third), "stop_qty": float(rest)})

    def unprotect(self, p):
        for kind, cid in (("list", p["oco_id"]), ("order", p["stop_id"])):
            if not cid:
                continue
            try:
                self._send(self.ex.cancel_list if kind == "list" else self.ex.cancel, p["symbol"], cid)
            except Exception as e:                  # already filled/cancelled: fine, reconciliation follows
                self.log(f"live: cancel {cid}: {e}")

    def close(self, p, reason):
        self.unprotect(p)
        sym = p["symbol"]
        qty = self.ex.qty(sym, p["qty_open"])
        if not self.shadow:
            free = self.ex.balances().get(self.ex.filters(sym)["base"], (0.0, 0.0))[0]
            qty = min(qty, self.ex.qty(sym, free))
        px = self.ex.price(sym)
        if qty > 0:
            r = self._send(self.ex.market_sell, sym, qty, f"bx{int(time.time())}{sym[:6]}"[:30])
            if not self.shadow and r.get("fills"):
                px = sum(float(f["price"]) * float(f["qty"]) for f in r["fills"]) / sum(float(f["qty"]) for f in r["fills"])
        pnl = float(qty) * px - p["cost_usdt"] * (float(qty) / p["qty"]) if p["qty"] else 0.0
        prev = self.con.execute("SELECT COALESCE(pnl_usdt,0) FROM positions WHERE id=?", (p["id"],)).fetchone()[0]
        self.con.execute("UPDATE positions SET status='closed', qty_open=0, closed_utc=?, exit_px=?, pnl_usdt=?, reason=? WHERE id=?",
                         (now(), px, prev + pnl, reason, p["id"]))
        self.con.commit()
        self.act(sym, "sold", {"qty": float(qty), "price": px, "pnl_usdt": round(prev + pnl, 2), "reason": reason})

    # ------------------------------------------------------------ the run
    def run(self, paper):
        c = self.c
        if self.mode == "off":
            return {"mode": "off"}
        if not self.shadow:
            self.ex.sync_time()
        # 0) kill switches and limits
        if os.path.exists(FLAT_FILE):
            for p in self.positions():
                self.close(p, "flatten switch")
            self.act("ALL", "flattened", {"reason": "FLATTEN_NOW file present; trading stopped"})
            return self.report(stopped=True)
        eq, real, unreal = self.equity()
        peak = max([c["capital_usdt"]] + [x for (x,) in self.con.execute("SELECT equity FROM equity WHERE mode=?", (self.mode,))])
        day0 = self.con.execute("SELECT equity FROM equity WHERE mode=? AND utc >= ? ORDER BY utc LIMIT 1",
                                (self.mode, now()[:10])).fetchone()
        day_loss = 100 * (1 - eq / day0[0]) if day0 and day0[0] else 0.0
        dd = 100 * (1 - eq / peak) if peak else 0.0
        self.con.execute("INSERT INTO equity VALUES(?,?,?,?,?)", (now(), self.mode, eq, real, unreal))
        self.con.commit()
        if dd >= c["max_drawdown_pct"]:
            for p in self.positions():
                self.close(p, f"max drawdown {dd:.1f}%")
            open(STOP_FILE, "w").close()
            self.act("ALL", "halted", {"reason": f"drawdown {dd:.1f}% >= {c['max_drawdown_pct']}%: all sold, STOP_TRADING set"})
            return self.report(stopped=True)
        entries_ok = not os.path.exists(STOP_FILE) and day_loss < c["max_daily_loss_pct"]
        if day_loss >= c["max_daily_loss_pct"]:
            self.act("ALL", "paused", {"reason": f"daily loss {day_loss:.1f}% >= {c['max_daily_loss_pct']}%: no new entries today"})

        # 1) reconciliation: read the status of every protective order (balances alone can't tell, because the
        #    owner may hold the same coin), then check the exchange holds at least what the bot thinks it holds
        if not self.shadow:
            for p in self.positions():
                self.refresh_fills(p)
            bal = self.ex.balances()
            for p in self.positions():
                base = self.ex.filters(p["symbol"])["base"]
                if sum(bal.get(base, (0.0, 0.0))) + 1e-9 < p["qty_open"] * 0.999:
                    open(STOP_FILE, "w").close()
                    self.act(p["symbol"], "reconcile problem", {"reason": "exchange holds less than the bot's position; new entries paused"})
        # 2) exits and stop management, driven by the paper bot
        pos_paper = {(x["symbol"], x["signal_date"]): x for x in paper.get("open", [])}
        closed_paper = {(x["symbol"], x["signal_date"]): x for x in paper.get("closed", [])}
        for p in self.positions():
            k = (p["symbol"], p["signal_date"])
            px = self.ex.price(p["symbol"])
            if k in closed_paper:
                self.close(p, "paper exit: " + str(closed_paper[k].get("reason", "")))
            elif k in pos_paper and pos_paper[k].get("exit_next_open"):
                self.close(p, "exit rule (close below 10-day low)")
            elif px <= p["stop"] * (1 - c["stop_limit_buffer_pct"] / 100.0):
                self.close(p, "price gapped through the stop")
            elif k in pos_paper:
                pp = pos_paper[k]
                if pp.get("partials") and not p["partial_done"]:
                    self.con.execute("UPDATE positions SET partial_done=1 WHERE id=?", (p["id"],))
                new_stop = float(pp["stop"])
                if new_stop > p["stop"] * 1.0005:              # stops only move up (break-even after the partial)
                    self.unprotect(p)
                    self.con.execute("UPDATE positions SET stop=? WHERE id=?", (new_stop, p["id"]))
                    self.con.commit()
                    p = dict(p, stop=new_stop, partial_done=1 if pp.get("partials") else p["partial_done"])
                    self.protect(p)
        # 3) entries
        if entries_ok:
            have = {p["symbol"] for p in self.positions()}
            for o in paper.get("orders", []):
                if o.get("side") != "long" or o.get("add") or o["symbol"] in have:
                    continue
                pid = f"{self.mode}:{c['bot']}:{o['symbol']}:{o['signal_date']}"
                if self.con.execute("SELECT 1 FROM positions WHERE id=?", (pid,)).fetchone():
                    continue                                 # this signal was already traded (or closed)
                if len(self.positions()) >= c["max_positions"]:
                    self.act(o["symbol"], "skipped", {"reason": "max positions reached"})
                    continue
                self.enter(o, eq)
        return self.report()

    def refresh_fills(self, p):
        """The exchange holds less than recorded: the OCO take-profit or the stop filled. Record it."""
        try:
            if p["oco_id"] and not p["partial_done"]:
                o = self.ex.order(p["symbol"], p["oco_id"] + "a")
                if o.get("status") == "FILLED":
                    q = float(o["executedQty"]); px = float(o["cummulativeQuoteQty"]) / q
                    pnl = q * px - p["cost_usdt"] * (q / p["qty"])
                    self.con.execute("UPDATE positions SET qty_open=qty_open-?, partial_done=1, pnl_usdt=COALESCE(pnl_usdt,0)+? WHERE id=?",
                                     (q, pnl, p["id"]))
                    self.act(p["symbol"], "partial profit", {"qty": q, "price": px, "pnl_usdt": round(pnl, 2)})
            if p["stop_id"]:
                o = self.ex.order(p["symbol"], p["stop_id"])
                if o.get("status") == "FILLED":
                    q = float(o["executedQty"]); px = float(o["cummulativeQuoteQty"]) / q
                    pnl = q * px - p["cost_usdt"] * (q / p["qty"])
                    self.con.execute("""UPDATE positions SET status='closed', qty_open=0, closed_utc=?, exit_px=?,
                                        pnl_usdt=COALESCE(pnl_usdt,0)+?, reason='stop filled' WHERE id=?""", (now(), px, pnl, p["id"]))
                    self.act(p["symbol"], "stopped out", {"qty": q, "price": px, "pnl_usdt": round(pnl, 2)})
            self.con.commit()
        except Exception as e:
            self.act(p["symbol"], "reconcile problem", {"reason": str(e)[:160]})
            open(STOP_FILE, "w").close()

    def enter(self, o, eq):
        c, sym = self.c, o["symbol"]
        f = self.ex.filters(sym)
        if f["status"] != "TRADING":
            return self.act(sym, "skipped", {"reason": "pair not trading"})
        px = self.ex.price(sym)
        drift = 100 * (px / o["ref_close"] - 1)
        if abs(drift) > c["max_entry_drift_pct"]:
            return self.act(sym, "skipped", {"reason": f"price moved {drift:+.1f}% from the signal close", "price": px})
        stop_dist = o["stop_dist"] * px / o["ref_close"]           # same ATR-based distance, re-anchored to the fill
        risk_usdt = eq * c["risk"]                                  # the bot's own % risk (Leaders 1%, Turbo 3%)
        usdt = min(risk_usdt / stop_dist * px, eq * c["max_pos_frac"], c["max_order_usdt"])
        if not self.shadow:
            usdt = min(usdt, self.ex.balances().get("USDT", (0.0, 0.0))[0] * 0.98)
        if usdt < float(f["min_notional"]) * 1.2 or usdt < 10:
            return self.act(sym, "skipped", {"reason": "position below the exchange minimum", "usdt": round(usdt, 2)})
        cid = f"be{int(time.time())}{sym[:6]}"[:30]
        r = self._send(self.ex.market_buy_quote, sym, usdt, cid)
        if self.shadow:
            qty, avg, cost = usdt / px, px, usdt * 1.001
        else:
            qty = float(r["executedQty"]); cost = float(r["cummulativeQuoteQty"])
            avg = cost / qty if qty else px
            fee_base = sum(float(x["commission"]) for x in r.get("fills", []) if x.get("commissionAsset") == f["base"])
            qty -= fee_base                                          # fee paid in the coin reduces what we hold
        stop = avg - stop_dist
        target = avg + 2 * stop_dist
        pid = f"{self.mode}:{c['bot']}:{sym}:{o['signal_date']}"
        self.con.execute("INSERT OR REPLACE INTO positions(id, mode, bot, symbol, signal_date, status, qty, qty_open, entry, cost_usdt, stop0, stop, target, opened_utc) "
                         "VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?,?)", (pid, self.mode, c["bot"], sym, o["signal_date"], qty, qty, avg, cost, stop, stop, target, now()))
        self.con.commit()
        self.act(sym, "bought", {"qty": round(qty, 8), "price": avg, "usdt": round(cost, 2), "stop": stop, "target": target})
        p = dict(zip([x[1] for x in self.con.execute("PRAGMA table_info(positions)")],
                     self.con.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()))
        self.protect(p)

    def report(self, stopped=False):
        eq, real, unreal = self.equity()
        out = {"mode": self.mode, "bot": self.c["bot"], "equity": round(eq, 2), "realized": round(real, 2),
               "unrealized": round(unreal, 2), "open": len(self.positions()), "stopped": stopped, "messages": self.msgs}
        if self.msgs:
            self.notify("Live bot (" + self.mode + ")\n" + "\n".join("• " + m for m in self.msgs[:15]) +
                        f"\nEquity {out['equity']} USDT (start {self.c['capital_usdt']})")
        return out


def run(log=print, notify=None, transport=None):
    cfg = load_cfg()
    if cfg["mode"] == "off":
        return {"mode": "off"}
    if os.path.exists(os.path.join(ROOT, ".no_publish")) and cfg["mode"] in ("testnet", "live"):
        return {"mode": "off"}               # only the primary machine (the publisher) may place orders
    import live_exchange as X
    cred = ("0" * 64, "0" * 64) if cfg["mode"] == "shadow" else X.read_cred(
        X.CRED if cfg["mode"] == "live" else os.path.join(ROOT, ".binance_testnet"))
    ex = X.Spot("testnet" if cfg["mode"] == "testnet" else "live", cred, transport)
    with open(BOTJ) as f:
        bj = json.load(f)
    bot = next((b for b in bj["bots"] if b["id"] == cfg["bot"]), None)
    if not bot:
        raise RuntimeError(f"live: bot {cfg['bot']} not found")
    bc = bot["strategy"]["config"]
    if float(bc.get("max_gross", 1.0)) > 1.0:
        raise RuntimeError("live: this bot uses leverage; live trading is spot-only for now")
    cfg["risk"], cfg["max_pos_frac"] = float(bc["risk"]), float(bc["max_pos_frac"])
    con = sqlite3.connect(LDB)
    try:
        return Engine(cfg, ex, con, log, notify).run(bot["paper"])
    finally:
        con.close()


def drill():
    """Testnet only: one full cycle with fake money on BTCUSDT - buy, exchange-side protection, check, sell."""
    import live_exchange as X
    import tempfile
    cred = X.read_cred(os.path.join(ROOT, ".binance_testnet"))
    ex = X.Spot("testnet", cred)
    ex.sync_time()
    sym = "BTCUSDT"
    px = ex.price(sym)
    cfg = dict(DEFAULTS, mode="testnet", bot="drill", capital_usdt=1500, max_order_usdt=100, risk=0.01, max_pos_frac=0.2,
               max_entry_drift_pct=1000)
    tmp = tempfile.mkdtemp()
    con = sqlite3.connect(os.path.join(tmp, "drill.db"))
    global STOP_FILE, FLAT_FILE
    STOP_FILE, FLAT_FILE = os.path.join(tmp, "STOP"), os.path.join(tmp, "FLAT")
    order = {"symbol": sym, "side": "long", "signal_date": "drill", "ref_close": px, "stop_dist": px * 0.03, "add": False}
    print(f"1/4 testnet BTC price {px:,.2f}; USDT free {ex.balances().get('USDT', (0, 0))[0]:,.2f}")
    E = Engine(cfg, ex, con)
    E.run({"open": [], "closed": [], "orders": [order]})
    oo = ex.open_orders(sym)
    print(f"2/4 bought and protected: {len(oo)} open orders on the exchange ->", [(o['type'], o['side'], o['origQty'], o.get('stopPrice')) for o in oo])
    ok = len(oo) >= 2
    open(FLAT_FILE, "w").close()
    Engine(cfg, ex, con).run({"open": [], "closed": [], "orders": []})
    left = ex.open_orders(sym)
    print(f"3/4 flattened: {len(left)} open orders left; position status:", [p["status"] for p in Engine(cfg, ex, con).positions("closed")])
    ok = ok and not left
    print("4/4 DRILL PASSED" if ok else "4/4 DRILL FAILED - send me this output")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--testnet-drill" in sys.argv:
        sys.exit(drill())
    print("Usage: python3 ~/BinanceData/bin/live_engine.py --testnet-drill")
