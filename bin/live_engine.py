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
#                              it limits the USDT AT WORK (open cost + unsettled buys), not only the starting capital
MAX_POSITIONS = 6            # code-level ceiling on positions at once, whatever live.json says

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions(id TEXT PRIMARY KEY, mode TEXT, bot TEXT, symbol TEXT, signal_date TEXT,
    status TEXT, qty REAL, qty_open REAL, entry REAL, cost_usdt REAL, stop0 REAL, stop REAL, target REAL,
    oco_id TEXT, stop_id TEXT, partial_done INTEGER DEFAULT 0, opened_utc TEXT, closed_utc TEXT,
    exit_px REAL, pnl_usdt REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS actions(utc TEXT, mode TEXT, symbol TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS equity(utc TEXT, mode TEXT, equity REAL, realized REAL, unrealized REAL);
CREATE TABLE IF NOT EXISTS fills_seen(cid TEXT PRIMARY KEY, qty REAL, quote REAL);
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
        have = {r[1] for r in con.execute("PRAGMA table_info(positions)")}
        for col, typ in (("entry_cid", "TEXT"), ("prot_n", "INTEGER"), ("exit_cid", "TEXT")):
            if col not in have:
                con.execute(f"ALTER TABLE positions ADD COLUMN {col} {typ}")
        con.commit()
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

    def positions(self, status="open", bot=None):
        """Positions of this mode. Management (safety, exits, stops, flatten) covers every bot's positions in the
        mode, so a position is never orphaned when live.json switches bot; entries use the configured bot."""
        cols = [c[1] for c in self.con.execute("PRAGMA table_info(positions)")]
        q, a = "SELECT * FROM positions WHERE mode=? AND status=?", [self.mode, status]
        if bot:
            q += " AND bot=?"
            a.append(bot)
        return [dict(zip(cols, r)) for r in self.con.execute(q, a)]

    def realized(self):
        return self.con.execute("SELECT COALESCE(SUM(pnl_usdt),0) FROM positions WHERE mode=? AND status IN ('open','closed')",
                                (self.mode,)).fetchone()[0]

    def deployed(self):
        """USDT at work: cost of what is still held, plus buys sent but not settled."""
        d = sum(p["cost_usdt"] * (p["qty_open"] / p["qty"]) for p in self.positions() if p["qty"])
        return d + sum(p["cost_usdt"] or 0 for p in self.positions("pending"))

    def equity(self):
        unreal = 0.0
        for p in self.positions():
            if p["qty"]:
                try:
                    px = self.ex.price(p["symbol"])
                except Exception:
                    px = p["entry"]                       # a price that cannot be read: valued at entry this run
                unreal += p["qty_open"] * px - p["cost_usdt"] * (p["qty_open"] / p["qty"])
        r = self.con.execute("SELECT COALESCE(SUM(pnl_usdt),0) FROM positions WHERE mode=?", (self.mode,)).fetchone()[0]
        return self.c["capital_usdt"] + r + unreal, r, unreal

    # ------------------------------------------------------------ order helpers (shadow-aware)
    def _send(self, fn, *a):
        if self.shadow:
            return {"shadow": True}
        return fn(*a)

    @staticmethod
    def outcome(e):
        """'missing' (the order does not exist), 'unknown' (it may have landed: network, 5xx, -1007, bad reply) or
        'rejected' (a clear refusal: it did not land)."""
        code, status = getattr(e, "code", None), getattr(e, "status", None)
        if code == -2013 or "does not exist" in str(e).lower() or "unknown order" in str(e).lower():
            return "missing"
        if code in ("network", "bad_json", -1007, -1001, -1003, -1015) or status is None or (status and status >= 500) or status == 429:
            return "unknown"
        return "rejected"

    def order_state(self, sym, cid):
        """(status, executedQty, cummulativeQuoteQty) of one order; ("MISSING", 0, 0) when the exchange does not
        know it; None when it could not be asked."""
        try:
            o = self.ex.order(sym, cid)
            return o.get("status"), float(o.get("executedQty") or 0), float(o.get("cummulativeQuoteQty") or 0)
        except Exception as e:
            return ("MISSING", 0.0, 0.0) if self.outcome(e) == "missing" else None

    @staticmethod
    def cid_for(kind, pid, n=0):
        """Deterministic client order ids (Binance allows 36 chars): the same position always gets the same id, so a
        repeated attempt after a lost reply is recognised instead of placed twice."""
        import hashlib
        return f"b{kind}{hashlib.sha1(pid.encode()).hexdigest()[:20]}{n:02d}"

    def protect(self, p):
        """Exchange-side protection: OCO on one third (+2R / stop) until the take-profit has filled, stop-limit on
        the rest. Each id is saved BEFORE its order is sent; a lost reply is checked on the next run."""
        sym, buf = p["symbol"], self.c["stop_limit_buffer_pct"] / 100.0
        if not self.shadow:
            if self.protection_live(self.row(p["id"])):   # old orders not (yet) cancelled: never stack new ones on
                return self.act(sym, "protection waiting", {"reason": "earlier protective orders still on the book; retried next run"})
            px = self.ex.price(sym)
            if px <= p["stop"] * 1.001:                    # a stop at or above the price would be refused: sell now
                return self.close(p, "price at or below the stop")
        n = 1 + (self.con.execute("SELECT COALESCE(MAX(prot_n),0) FROM positions WHERE id=?", (p["id"],)).fetchone()[0] or 0)
        self.con.execute("UPDATE positions SET prot_n=? WHERE id=?", (n, p["id"]))
        third = self.ex.qty(sym, p["qty_open"] / 3.0) if not p["partial_done"] else 0
        if third > 0 and not self.shadow and self.ex.price(sym) >= p["target"]:
            third = 0                                      # already over the target: the stop covers everything
        rest = self.ex.qty(sym, p["qty_open"] - float(third))
        stop = self.ex.px(sym, p["stop"])
        self.con.execute("UPDATE positions SET oco_id=NULL WHERE id=? AND ?", (p["id"], 1 if third <= 0 else 0))
        stop_lim = self.ex.px(sym, p["stop"] * (1 - buf))
        done = {}
        if third > 0 and not p["partial_done"]:
            oco_id = self.cid_for("o", p["id"], n)
            self.con.execute("UPDATE positions SET oco_id=? WHERE id=?", (oco_id, p["id"]))
            self.con.commit()
            try:
                self._send(self.ex.oco_sell, sym, third, self.ex.px(sym, p["target"], up=True), stop, stop_lim, oco_id)
                done["oco_qty"] = float(third)
            except Exception as e:
                self.protection_failed(p, "oco_id", e)
                if self.outcome(e) == "rejected":          # no OCO: the stop covers the whole position
                    rest = self.ex.qty(sym, p["qty_open"])
        if rest > 0:
            stop_id = self.cid_for("s", p["id"], n)
            self.con.execute("UPDATE positions SET stop_id=? WHERE id=?", (stop_id, p["id"]))
            self.con.commit()
            try:
                self._send(self.ex.stop_sell, sym, rest, stop, stop_lim, stop_id)
                done["stop_qty"] = float(rest)
            except Exception as e:
                self.protection_failed(p, "stop_id", e)
        self.con.commit()
        self.act(sym, "protected", dict({"stop": float(stop), "target": p["target"]}, **done))

    def protection_failed(self, p, col, e):
        if self.outcome(e) == "rejected":              # it did not land: clear the id; ensure_protected retries
            self.con.execute(f"UPDATE positions SET {col}=NULL WHERE id=?", (p["id"],))
        self.con.commit()
        self.act(p["symbol"], "protection problem", {"reason": f"{col[:-3]} order: {str(e)[:120]}"})

    def protection_live(self, p):
        """True when any protective order of this position may still rest on the book (or cannot be checked)."""
        for cid in ((p["oco_id"] + "a") if p["oco_id"] else None, (p["oco_id"] + "b") if p["oco_id"] else None, p["stop_id"]):
            if not cid:
                continue
            st = self.order_state(p["symbol"], cid)
            if st is None or st[0] in ("NEW", "PARTIALLY_FILLED"):
                return True
        return False

    def ensure_protected(self, p):
        """Every run: a position must have a live stop (and its OCO until the take-profit filled). Cancelled,
        expired, never-landed or missing protection is placed again."""
        if self.shadow or p["exit_cid"] or str(p["reason"] or "").startswith("closing"):
            return                                       # an exit is under way: never re-protect it
        need = False
        for col, cid in (("oco_id", (p["oco_id"] + "b") if p["oco_id"] else None), ("stop_id", p["stop_id"])):
            if col == "oco_id" and p["partial_done"]:
                continue
            if not cid:
                need = True
                continue
            st = self.order_state(p["symbol"], cid)
            if st is None:
                return                                   # could not ask: decide nothing this run
            if st[0] in ("NEW", "PARTIALLY_FILLED", "FILLED"):
                continue
            if col == "oco_id":                          # the OCO ended: because its take-profit filled? then the
                a = self.order_state(p["symbol"], p["oco_id"] + "a")    # fill is booked, nothing to re-place
                if a is None or a[1] > 0:
                    return
            need = True                                  # MISSING, CANCELED, EXPIRED, REJECTED: not protecting
        if need and p["qty_open"] > 0:
            self.unprotect(p)
            self.protect(dict(p, **self.row(p["id"])))

    def row(self, pid):
        cols = [c[1] for c in self.con.execute("PRAGMA table_info(positions)")]
        return dict(zip(cols, self.con.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()))

    def unprotect(self, p):
        """Cancel the protective orders; then read them back so a fill that happened meanwhile is booked."""
        for kind, cid in (("list", p["oco_id"]), ("order", p["stop_id"])):
            if not cid:
                continue
            try:
                self._send(self.ex.cancel_list if kind == "list" else self.ex.cancel, p["symbol"], cid)
            except Exception as e:                  # already filled/cancelled: refresh_fills books what traded
                self.log(f"live: cancel {cid}: {e}")
        if not self.shadow:
            self.refresh_fills(self.row(p["id"]))

    def book_sale(self, p, sold, px, reason, final_ok=True):
        p = self.row(p["id"])
        pnl = sold * px - p["cost_usdt"] * (sold / p["qty"]) if p["qty"] else 0.0
        left = max(0.0, p["qty_open"] - sold)
        if left > 0 and float(self.ex.qty(p["symbol"], left)) > 0:
            self.con.execute("UPDATE positions SET qty_open=?, pnl_usdt=COALESCE(pnl_usdt,0)+?, exit_cid=NULL, reason=? WHERE id=?",
                             (left, pnl, "closing: " + reason, p["id"]))
            self.con.commit()
            self.act(p["symbol"], "sold part", {"qty": sold, "price": px, "pnl_usdt": round(pnl, 2), "reason": f"{reason}; {left:g} left, retried next run"})
            return False
        self.con.execute("UPDATE positions SET status='closed', qty_open=0, exit_cid=NULL, closed_utc=?, exit_px=?, pnl_usdt=COALESCE(pnl_usdt,0)+?, reason=? WHERE id=?",
                         (now(), px, pnl, reason, p["id"]))
        self.con.commit()
        self.act(p["symbol"], "sold", {"qty": sold, "price": px, "pnl_usdt": round((p["pnl_usdt"] or 0) + pnl, 2), "reason": reason})
        return True

    def close(self, p, reason):
        """Sell what the bot still holds of this position (never more, never the owner's coins). A sell whose reply
        was lost is looked up by its id before anything else is sold; what does not sell stays open for next run."""
        p = self.row(p["id"])
        sym = p["symbol"]
        if p["exit_cid"] and not self.shadow:            # an earlier sell without a clear answer
            st = self.order_state(sym, p["exit_cid"])
            if st is None:
                return                                    # cannot tell yet: sell nothing more
            if st[0] in ("NEW", "PARTIALLY_FILLED"):
                return
            if st[0] != "MISSING" and st[1] > 0:
                if self.book_sale(p, st[1], st[2] / st[1], reason):
                    self.unprotect(self.row(p["id"]))             # nothing of this position may stay on the book
                    return
            else:
                self.con.execute("UPDATE positions SET exit_cid=NULL WHERE id=?", (p["id"],))
                self.con.commit()
            p = self.row(p["id"])
        self.unprotect(p)
        p = self.row(p["id"])
        if p["status"] != "open":
            return
        if not self.shadow and self.protection_live(p):      # its coins are still locked: selling now would sell
            return self.act(sym, "exit waiting", {"reason": f"{reason}: protective orders could not be cancelled yet; retried next run"})
        qty = self.ex.qty(sym, p["qty_open"])
        if not self.shadow:
            free = self.ex.balances().get(self.ex.filters(sym)["base"], (0.0, 0.0))[0]
            qty = min(qty, self.ex.qty(sym, free))
        px, sold = self.ex.price(sym), float(qty)
        if qty > 0 and not self.shadow:
            cid = self.cid_for("x", p["id"], int(time.time()) % 100)
            self.con.execute("UPDATE positions SET exit_cid=? WHERE id=?", (cid, p["id"]))
            self.con.commit()
            try:
                r = self.ex.market_sell(sym, qty, cid)
            except Exception as e:
                if self.outcome(e) == "rejected":
                    self.con.execute("UPDATE positions SET exit_cid=NULL WHERE id=?", (p["id"],))
                    self.con.commit()
                    return self.act(sym, "sell refused", {"reason": f"{reason}: {str(e)[:120]}"})
                return self.act(sym, "sell unconfirmed", {"reason": f"{reason}: no clear answer; looked up next run, never sold twice"})
            sold = float(r.get("executedQty") or 0)
            if r.get("fills"):
                fq = sum(float(f["qty"]) for f in r["fills"])
                px = sum(float(f["price"]) * float(f["qty"]) for f in r["fills"]) / fq if fq else px
        self.book_sale(p, sold, px, reason)

    def guarded(self, p, what, fn):
        """One position's trouble never stops the others."""
        try:
            fn()
        except Exception as e:
            self.act(p["symbol"] if p else "ALL", "error", {"reason": f"{what}: {str(e)[:160]}"})

    # ------------------------------------------------------------ the run
    def run(self, paper):
        c = self.c
        if self.mode == "off":
            return {"mode": "off"}
        if not self.shadow:
            self.ex.sync_time()
            for p in self.positions("pending"):              # buys sent in an earlier run without a clear answer
                self.guarded(p, "pending buy", lambda p=p: self.settle_pending(p))
            for p in self.positions():                       # sells sent without a clear answer, whatever happens next
                if p["exit_cid"]:
                    self.guarded(p, "pending sell", lambda p=p: self.close(p, str(p["reason"] or "exit")[9:] if str(p["reason"] or "").startswith("closing: ") else "exit"))
        # 0) kill switches and limits
        if os.path.exists(FLAT_FILE):
            for p in self.positions():
                self.guarded(p, "flatten", lambda p=p: self.close(p, "flatten switch"))
            open(STOP_FILE, "w").close()                       # removing FLATTEN_NOW alone does not resume entries
            self.act("ALL", "flattened", {"reason": "FLATTEN_NOW file present; trading stopped (STOP_TRADING set)"})
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
                self.guarded(p, "drawdown exit", lambda p=p: self.close(p, f"max drawdown {dd:.1f}%"))
            open(STOP_FILE, "w").close()
            self.act("ALL", "halted", {"reason": f"drawdown {dd:.1f}% >= {c['max_drawdown_pct']}%: all sold, STOP_TRADING set"})
            return self.report(stopped=True)
        if day_loss >= c["max_daily_loss_pct"]:
            self.act("ALL", "paused", {"reason": f"daily loss {day_loss:.1f}% >= {c['max_daily_loss_pct']}%: no new entries today"})

        # 1) reconciliation: book every protective fill (both OCO legs, partial fills), then check the exchange holds
        #    at least what the bot thinks it holds; re-place missing protection
        if not self.shadow:
            for p in self.positions():
                self.guarded(p, "fills", lambda p=p: self.refresh_fills(p))
            bal = self.ex.balances()
            for p in self.positions():
                self.guarded(p, "balance check", lambda p=p: self.check_balance(p, bal))
        # 2) exits and stop management, driven by the paper bot
        pos_paper = {(x["symbol"], x["signal_date"]): x for x in paper.get("open", [])}
        closed_paper = {(x["symbol"], x["signal_date"]): x for x in paper.get("closed", [])}
        for p in self.positions():
            self.guarded(p, "manage", lambda p=p: self.manage(p, pos_paper, closed_paper))
        if not self.shadow:
            for p in self.positions():
                self.guarded(p, "protection", lambda p=p: self.ensure_protected(self.row(p["id"])))
        # 3) entries (the kill switch is read again: reconciliation above may just have set it)
        entries_ok = not os.path.exists(STOP_FILE) and day_loss < c["max_daily_loss_pct"]
        if entries_ok:
            for o in paper.get("orders", []):
                have = {p["symbol"] for p in self.positions() + self.positions("pending")}
                if o.get("side") != "long" or o.get("add") or o["symbol"] in have:
                    continue
                pid = f"{self.mode}:{c['bot']}:{o['symbol']}:{o['signal_date']}"
                if self.con.execute("SELECT 1 FROM positions WHERE id=?", (pid,)).fetchone():
                    continue                                 # this signal was already traded (or closed, or void)
                if len(self.positions()) + len(self.positions("pending")) >= min(c["max_positions"], MAX_POSITIONS):
                    self.act(o["symbol"], "skipped", {"reason": "max positions reached"})
                    continue
                self.guarded(None, "entry " + o["symbol"], lambda o=o: self.enter(o, eq))
        return self.report()

    def check_balance(self, p, bal):
        base = self.ex.filters(p["symbol"])["base"]
        if sum(bal.get(base, (0.0, 0.0))) + 1e-9 < p["qty_open"] * 0.999:
            open(STOP_FILE, "w").close()
            self.act(p["symbol"], "reconcile problem", {"reason": "exchange holds less than the bot's position; new entries paused"})

    def manage(self, p, pos_paper, closed_paper):
        c = self.c
        p = self.row(p["id"])
        if p["status"] != "open" or p["exit_cid"]:
            return                                       # closed meanwhile, or a sell still being looked up
        if str(p["reason"] or "").startswith("closing: "):
            return self.close(p, p["reason"][9:])         # an exit already decided is finished, never undone
        k = (p["symbol"], p["signal_date"])
        px = self.ex.price(p["symbol"])
        mine = p["bot"] == c["bot"]
        if mine and k in closed_paper:
            self.close(p, "paper exit: " + str(closed_paper[k].get("reason", "")))
        elif mine and k in pos_paper and pos_paper[k].get("exit_next_open"):
            self.close(p, "exit rule (close below 10-day low)")
        elif px <= p["stop"] * (1 - c["stop_limit_buffer_pct"] / 100.0):
            self.close(p, "price gapped through the stop")
        elif mine and k in pos_paper:
            new_stop = float(pos_paper[k]["stop"])
            if new_stop > p["stop"] * 1.0005:              # stops only move up (break-even after the partial)
                self.unprotect(p)
                p = self.row(p["id"])
                if p["status"] != "open":
                    return
                self.con.execute("UPDATE positions SET stop=? WHERE id=?", (new_stop, p["id"]))
                self.con.commit()
                self.protect(dict(p, stop=new_stop))

    def refresh_fills(self, p):
        """Book what the protective orders traded: both OCO legs and the stop, partial fills included. Each order's
        traded quantity is booked once (table fills_seen), so nothing is counted twice or missed."""
        if self.shadow or p["status"] != "open":
            return
        for cid, leg in (((p["oco_id"] or "") + "a", "take-profit"), ((p["oco_id"] or "") + "b", "OCO stop"), (p["stop_id"], "stop")):
            if not cid or cid in ("a", "b"):
                continue
            st = self.order_state(p["symbol"], cid)
            if not st or st[1] <= 0:
                continue
            seen = self.con.execute("SELECT COALESCE(qty,0), COALESCE(quote,0) FROM fills_seen WHERE cid=?", (cid,)).fetchone() or (0.0, 0.0)
            dq, dquote = st[1] - seen[0], st[2] - seen[1]
            if dq <= 1e-12:
                continue
            p = self.row(p["id"])
            dq = min(dq, p["qty_open"])
            fpx = dquote / dq if dq else 0.0
            pnl = dq * fpx - p["cost_usdt"] * (dq / p["qty"])
            left = max(0.0, p["qty_open"] - dq)
            self.con.execute("INSERT OR REPLACE INTO fills_seen VALUES(?,?,?)", (cid, st[1], st[2]))
            self.con.execute("UPDATE positions SET qty_open=?, pnl_usdt=COALESCE(pnl_usdt,0)+?, partial_done=MAX(partial_done, ?) WHERE id=?",
                             (left, pnl, 1 if leg == "take-profit" else 0, p["id"]))
            if float(self.ex.qty(p["symbol"], left)) <= 0:
                self.con.execute("UPDATE positions SET status='closed', qty_open=0, closed_utc=?, exit_px=?, reason=? WHERE id=?",
                                 (now(), fpx, leg + " filled", p["id"]))
            self.con.commit()
            self.act(p["symbol"], "partial profit" if leg == "take-profit" else "stopped out",
                     {"qty": round(dq, 8), "price": fpx, "pnl_usdt": round(pnl, 2), "reason": leg})

    def settle_pending(self, p):
        """A buy sent without a clear answer: ask the exchange by its (deterministic) id."""
        st = self.order_state(p["symbol"], p["entry_cid"])
        if st is None:
            return
        if st[0] == "MISSING":
            self.con.execute("DELETE FROM positions WHERE id=?", (p["id"],))
            self.con.commit()
            self.act(p["symbol"], "entry not placed", {"reason": "the exchange never got the buy; it may be retried"})
            return
        if st[0] in ("NEW", "PARTIALLY_FILLED"):
            return                                       # a market order still working: next run
        self.book_entry(p, st[1], st[2], fee_base=st[1] * 0.001)   # fills unknown here: assume a 0.1% fee in the coin

    def book_entry(self, p, qty, cost, fee_base=0.0):
        sym = p["symbol"]
        if qty <= 0:
            self.con.execute("UPDATE positions SET status='void', reason='buy filled nothing' WHERE id=?", (p["id"],))
            self.con.commit()
            return self.act(sym, "skipped", {"reason": "the market buy filled nothing (expired)"})
        avg = cost / qty
        stop_dist = p["stop0"]                           # stored at send time: the distance, re-anchored to the fill
        stop, target = avg - stop_dist, avg + 2 * stop_dist
        hold = qty - fee_base
        self.con.execute("UPDATE positions SET status='open', qty=?, qty_open=?, entry=?, cost_usdt=?, stop0=?, stop=?, target=?, opened_utc=? WHERE id=?",
                         (hold, hold, avg, cost, stop, stop, target, now(), p["id"]))
        self.con.commit()
        self.act(sym, "bought", {"qty": round(hold, 8), "price": avg, "usdt": round(cost, 2), "stop": stop, "target": target})
        self.protect(self.row(p["id"]))

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
        room = HARD_CAP_USDT - self.deployed()                      # never more than the cap at work, whatever the profits
        usdt = min(usdt, room)
        if not self.shadow:
            usdt = min(usdt, self.ex.balances().get("USDT", (0.0, 0.0))[0] * 0.98)
        if usdt < float(f["min_notional"]) * 1.2 or usdt < 10:
            return self.act(sym, "skipped", {"reason": "position below the exchange minimum (or the cap is used)", "usdt": round(usdt, 2)})
        pid = f"{self.mode}:{c['bot']}:{sym}:{o['signal_date']}"
        cid = self.cid_for("e", pid)
        # intent first: if the reply is lost, the next run asks the exchange by this id instead of buying again
        self.con.execute("INSERT INTO positions(id, mode, bot, symbol, signal_date, status, qty, qty_open, entry, cost_usdt, stop0, stop, target, opened_utc, entry_cid) "
                         "VALUES(?,?,?,?,?,'pending',0,0,?,?,?,?,?,?,?)", (pid, self.mode, c["bot"], sym, o["signal_date"], px, usdt, stop_dist, 0, 0, now(), cid))
        self.con.commit()
        p = self.row(pid)
        if self.shadow:
            return self.book_entry(p, usdt / px, usdt * 1.001)
        try:
            r = self.ex.market_buy_quote(sym, usdt, cid)
        except Exception as e:
            if self.outcome(e) == "rejected":
                self.con.execute("DELETE FROM positions WHERE id=?", (pid,))
                self.con.commit()
                return self.act(sym, "skipped", {"reason": f"buy refused: {str(e)[:120]}"})
            return self.act(sym, "entry unconfirmed", {"reason": f"no clear answer ({str(e)[:80]}); checked next run, never bought twice"})
        qty, cost = float(r.get("executedQty") or 0), float(r.get("cummulativeQuoteQty") or 0)
        fee_base = sum(float(x["commission"]) for x in r.get("fills", []) if x.get("commissionAsset") == f["base"])
        self.book_entry(p, qty, cost, fee_base)

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
