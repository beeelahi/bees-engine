"""
Live "sell high, rebuy lower" grid on MEXC (Python 3.9 stdlib only). Same rules as mm_grid.Grid (simulator):
  * sell ladder: `size` coins at anchor x (1+step)^k, k = 1..levels, at most `sell_orders_live` resting at once;
  * every filled sell gets a rebuy of the same size one step lower; when it fills, that level is re-armed;
  * coins out (sold, not yet rebought) never exceed (start_coins - floor): the inventory never drops below `floor`;
  * re-centre down when nothing is outstanding and the price is `recenter_steps` steps below the ladder;
  * all orders are post-only (LIMIT_MAKER): never take liquidity, never trade with ourselves. If a rebuy can't
    rest because the market already traded below it, it is placed as a plain LIMIT at the rebuy price (we only
    ever buy at or below the planned rebuy price);
  * the slots go to the levels nearest the market (as in the backtest): a nearer armed level displaces the
    farthest resting order, which re-arms;
  * pauses (cancels its resting sells) while the mid is outside `band`; rebuys stay working.
  * buy-first rungs (`buy_rungs`, 0 = off): the mirror image below the anchor, paid from USDT. A rung buys `size`
    coins at anchor / (1+step)^k and, once filled, re-sells them one step higher; then the rung re-arms. At most
    `buy_orders_live` rungs rest at once, and USDT committed to rungs (resting + coins held) never exceeds
    `max_usdt`, after keeping enough USDT for every rebuy the sell ladder still owes. The year-long backtest of this exact code is described in docs/MARKET_MAKING.md.
State lives in data/mm.db (table grid), so restarts are safe. Kill switch: MM_STOP file cancels every bot order.
"""
import os
import time
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STOP = os.path.join(ROOT, "MM_STOP")

SCHEMA = """
CREATE TABLE IF NOT EXISTS grid(symbol TEXT, level INTEGER, state TEXT, sell_px REAL, rebuy_px REAL, sell_cid TEXT,
    buy_cid TEXT, qty REAL, updated_utc TEXT, PRIMARY KEY(symbol, level));
CREATE TABLE IF NOT EXISTS grid_meta(symbol TEXT PRIMARY KEY, anchor REAL, rounds INTEGER, profit_usdt REAL);
CREATE TABLE IF NOT EXISTS grid_buy(symbol TEXT, level INTEGER, state TEXT, buy_px REAL, resell_px REAL, buy_cid TEXT,
    sell_cid TEXT, qty REAL, updated_utc TEXT, PRIMARY KEY(symbol, level));
CREATE TABLE IF NOT EXISTS grid_log(utc TEXT, symbol TEXT, event TEXT, level INTEGER, price REAL, qty REAL, profit REAL);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GridLive:
    def __init__(self, ex, c, con, log=print):
        self.ex, self.c, self.con, self.log = ex, c, con, log
        self.sym = c["symbol"]
        con.executescript(SCHEMA)
        self.seq = 0
        self.events = []

    # ---------------------------------------------------------------- state
    def meta(self):
        r = self.con.execute("SELECT anchor, rounds, profit_usdt FROM grid_meta WHERE symbol=?", (self.sym,)).fetchone()
        return r if r else (None, 0, 0.0)

    def set_anchor(self, a):
        _, rounds, profit = self.meta()
        self.con.execute("INSERT OR REPLACE INTO grid_meta VALUES(?,?,?,?)", (self.sym, a, rounds, profit))
        self.con.execute("DELETE FROM grid WHERE symbol=? AND state='armed'", (self.sym,))
        for k in range(1, self.c["levels"] + 1):
            px = a * (1 + self.c["step"]) ** k
            self.con.execute("INSERT OR IGNORE INTO grid VALUES(?,?,?,?,?,?,?,?,?)",
                             (self.sym, k, "armed", px, px / (1 + self.c["step"]), None, None, self.c["size"], now()))
        self.con.commit()
        self.note("anchor", 0, a, 0, 0)
        self.arm_rungs(a)

    # ---------------------------------------------------------------- buy-first rungs
    def arm_rungs(self, a):
        """(Re)place the armed rungs under anchor a. Rungs holding coins (bought / reselling) keep their prices."""
        n = int(self.c.get("buy_rungs", 0))
        for lvl, st, bp, rp, bcid, scid, q in self.brows("buying"):
            try:
                self.ex.cancel(self.sym, bcid)
            except Exception:
                pass
            s, filled, _ = self.order_status(bcid)
            if s == "FILLED" or filled > 0:               # filled while we moved: keep it, resell what we got
                self.bupd(lvl, state="bought", buy_cid=None, qty=filled if s != "FILLED" else q)
                self.note("rung bought", lvl, bp, filled if s != "FILLED" else q, 0)
            else:
                self.bupd(lvl, state="armed", buy_cid=None)
        self.con.execute("DELETE FROM grid_buy WHERE symbol=? AND state='armed'", (self.sym,))
        for k in range(1, n + 1):
            px = a / (1 + self.c["step"]) ** k
            self.con.execute("INSERT OR IGNORE INTO grid_buy VALUES(?,?,?,?,?,?,?,?,?)",
                             (self.sym, k, "armed", px, px * (1 + self.c["step"]), None, None, self.c["size"], now()))
        self.con.commit()

    def brows(self, state=None):
        q = "SELECT level, state, buy_px, resell_px, buy_cid, sell_cid, qty FROM grid_buy WHERE symbol=?" + (" AND state=?" if state else "") + " ORDER BY level"
        return self.con.execute(q, (self.sym, state) if state else (self.sym,)).fetchall()

    def bupd(self, level, **kw):
        sets = ", ".join(f"{k}=?" for k in kw) + ", updated_utc=?"
        self.con.execute(f"UPDATE grid_buy SET {sets} WHERE symbol=? AND level=?", list(kw.values()) + [now(), self.sym, level])
        self.con.commit()

    def step_rungs(self, mid, in_band):
        c = self.c
        if not int(c.get("buy_rungs", 0)):
            return
        if not self.brows():                            # rungs switched on for a grid that already runs
            self.arm_rungs(self.meta()[0])
        # progress
        for lvl, st, bp, rp, bcid, scid, q in self.brows():
            if st == "buying" and bcid:
                s, filled, _ = self.order_status(bcid)
                if s == "FILLED":
                    self.bupd(lvl, state="bought", buy_cid=None)
                    self.note("rung bought", lvl, bp, q, 0)
                elif s in ("CANCELED", "EXPIRED", "REJECTED"):
                    if filled > 0:
                        self.bupd(lvl, state="bought", buy_cid=None, qty=filled)
                        self.note("rung bought", lvl, bp, filled, 0)
                    else:
                        self.bupd(lvl, state="armed", buy_cid=None)
            elif st == "reselling" and scid:
                s, filled, _ = self.order_status(scid)
                if s == "FILLED":
                    gain = q * (rp - bp)
                    _, rounds, profit = self.meta()
                    self.con.execute("UPDATE grid_meta SET rounds=?, profit_usdt=? WHERE symbol=?", (rounds + 1, profit + gain, self.sym))
                    self.bupd(lvl, state="armed", sell_cid=None, qty=c["size"])
                    self.note("rung resold", lvl, rp, q, gain)
                elif s in ("CANCELED", "EXPIRED", "REJECTED"):
                    if filled > 0:
                        _, rounds, profit = self.meta()
                        self.con.execute("UPDATE grid_meta SET profit_usdt=? WHERE symbol=?", (profit + filled * (rp - bp), self.sym))
                        self.note("rung resold", lvl, rp, filled, filled * (rp - bp))
                    self.bupd(lvl, state="bought", sell_cid=None, qty=q - filled)
        # resell everything bought, one step higher (never below the planned price)
        for lvl, st, bp, rp, bcid, scid, q in self.brows("bought"):
            px, qq = self.ex.px(self.sym, rp, up=True), self.ex.qty(self.sym, q)
            cid = self.cid("R")
            try:
                self.ex.limit_maker(self.sym, "SELL", qq, px, cid)
            except Exception:
                try:                                    # market already above: sell at the planned price or better
                    self.ex.limit(self.sym, "SELL", qq, px, cid)
                except Exception as e:
                    self.log(f"mm grid: rung resell L{lvl} not placed: {e}")
                    continue
            self.bupd(lvl, state="reselling", sell_cid=cid)
        live = self.brows("buying")
        if not in_band:                                 # outside the band: no new buying, cancel resting rung buys
            for lvl, st, bp, rp, bcid, scid, q in live:
                try:
                    self.ex.cancel(self.sym, bcid)
                except Exception:
                    pass
            return
        self.make_room("buying", [r[2] for r in self.brows("armed") if r[2] < mid],
                       int(c.get("buy_orders_live", 6)) - len(live), far=min, rows=self.brows, upd=self.bupd,
                       cid_col=4, cid_key="buy_cid")
        live = self.brows("buying")
        # USDT budget: rebuys owed by the sell ladder come first; rungs use at most max_usdt in total
        owed = sum(q * rp for lvl, st, sp, rp, a, b, q in self.rows() if st in ("sold", "rebuying"))
        committed = sum(q * bp for lvl, st, bp, rp, a, b, q in self.brows() if st != "armed")
        try:
            usdt_free = sum(self.ex.balances().get("USDT", (0.0, 0.0)))
        except Exception:
            return
        usdt_free -= owed + sum(q * bp for lvl, st, bp, rp, a, b, q in live)
        room = int(c.get("buy_orders_live", 6)) - len(live)
        for lvl, st, bp, rp, bcid, scid, q in self.brows("armed"):
            if room <= 0:
                break
            cost = q * bp
            if bp >= mid or committed + cost > c["max_usdt"] or cost > usdt_free:
                continue
            cid = self.cid("P")
            try:
                self.ex.limit_maker(self.sym, "BUY", self.ex.qty(self.sym, q), self.ex.px(self.sym, bp), cid)
                self.bupd(lvl, state="buying", buy_cid=cid)
                committed += cost
                usdt_free -= cost
                room -= 1
            except Exception as e:
                self.log(f"mm grid: rung buy L{lvl} not placed: {e}")

    def make_room(self, state, wanted, room, far, rows, upd, cid_col, cid_key, max_swaps=3):
        """Keep the orders nearest the market resting, as the backtest (mm_grid.Grid) does. When every slot is taken
        and an armed level is nearer the market than the farthest resting order, cancel that farthest order (it
        re-arms) so the nearer level can be placed this cycle. Returns how many slots were freed. An order that
        filled (fully or partly) while being cancelled is left to the normal progress step on the next cycle."""
        freed = 0
        wanted = sorted(wanted, reverse=far is min)      # nearest the market first
        for p in wanted[:max_swaps]:
            if room + freed > 0:
                break
            live = rows(state)
            if not live:
                break
            r = far(live, key=lambda x: x[2])
            if (far is max and r[2] <= p) or (far is min and r[2] >= p):
                break
            try:
                self.ex.cancel(self.sym, r[cid_col])
            except Exception:
                pass
            s, filled, _ = self.order_status(r[cid_col])
            if s in ("CANCELED", "EXPIRED", "REJECTED") and filled == 0:
                upd(r[0], state="armed", **{cid_key: None})
                freed += 1
            else:
                break
        return freed

    def rows(self, state=None):
        q = "SELECT level, state, sell_px, rebuy_px, sell_cid, buy_cid, qty FROM grid WHERE symbol=?" + (" AND state=?" if state else "") + " ORDER BY level"
        return self.con.execute(q, (self.sym, state) if state else (self.sym,)).fetchall()

    def upd(self, level, **kw):
        sets = ", ".join(f"{k}=?" for k in kw) + ", updated_utc=?"
        self.con.execute(f"UPDATE grid SET {sets} WHERE symbol=? AND level=?", list(kw.values()) + [now(), self.sym, level])
        self.con.commit()

    def note(self, event, level, price, qty, profit):
        self.con.execute("INSERT INTO grid_log VALUES(?,?,?,?,?,?,?)", (now(), self.sym, event, level, price, qty, profit))
        self.con.commit()
        if event in ("sold", "rebought", "rung bought", "rung resold"):
            self.events.append(f"{self.sym.replace('USDT', '')}: {event} {qty:,.0f} @ {price:.6g}" + (f" (+{profit:,.2f} USDT)" if profit else ""))

    def cid(self, side):
        self.seq += 1
        return f"bm{side}{int(time.time()) % 10**8}{self.seq % 10**4:04d}"

    def order_status(self, cid):
        try:
            o = self.ex.order(self.sym, cid)
            return o.get("status"), float(o.get("executedQty") or 0), float(o.get("price") or 0)
        except Exception:
            return None, 0.0, 0.0

    # ---------------------------------------------------------------- one cycle
    def step(self, mid):
        c = self.c
        if os.path.exists(STOP):
            for lvl, st, sp, rp, scid, bcid, q in self.rows() + self.brows():
                for x in (scid, bcid):
                    if x:
                        try:
                            self.ex.cancel(self.sym, x)
                        except Exception:
                            pass
            return "stopped"
        anchor, rounds, profit = self.meta()
        if anchor is None:
            self.set_anchor(mid)
        # 1) progress: sells filled -> place rebuy; rebuys filled -> profit, re-arm
        for lvl, st, sp, rp, scid, bcid, q in self.rows():
            if st == "selling" and scid:
                s, filled, _ = self.order_status(scid)
                if s == "FILLED":
                    self.upd(lvl, state="sold", sell_cid=None)
                    self.note("sold", lvl, sp, q, 0)
                elif s in ("CANCELED", "EXPIRED", "REJECTED"):
                    if filled > 0:                      # partly sold before it was cancelled: rebuy what was sold
                        self.upd(lvl, state="sold", sell_cid=None, qty=filled)
                        self.note("sold", lvl, sp, filled, 0)
                    else:
                        self.upd(lvl, state="armed", sell_cid=None)
            elif st == "rebuying" and bcid:
                s, filled, _ = self.order_status(bcid)
                if s == "FILLED":
                    gain = q * (sp - rp)                # maker fee on MEXC spot is 0%
                    _, rounds, profit = self.meta()
                    self.con.execute("UPDATE grid_meta SET rounds=?, profit_usdt=? WHERE symbol=?", (rounds + 1, profit + gain, self.sym))
                    self.upd(lvl, state="armed", buy_cid=None, qty=c["size"])
                    self.note("rebought", lvl, rp, q, gain)
                elif s in ("CANCELED", "EXPIRED", "REJECTED"):
                    if filled > 0:                      # partly rebought: book that part, keep rebuying the rest
                        _, rounds, profit = self.meta()
                        self.con.execute("UPDATE grid_meta SET profit_usdt=? WHERE symbol=?", (profit + filled * (sp - rp), self.sym))
                        self.note("rebought", lvl, rp, filled, filled * (sp - rp))
                    self.upd(lvl, state="sold", buy_cid=None, qty=q - filled)
        # 2) rebuys for every sold level
        for lvl, st, sp, rp, scid, bcid, q in self.rows("sold"):
            px, qq = self.ex.px(self.sym, rp), self.ex.qty(self.sym, q)
            cid = self.cid("B")
            try:
                self.ex.limit_maker(self.sym, "BUY", qq, px, cid)
            except Exception:
                try:                                    # market already below the rebuy: buy at the rebuy price
                    self.ex.limit(self.sym, "BUY", qq, px, cid)
                except Exception as e:
                    self.log(f"mm grid: rebuy L{lvl} not placed: {e}")
                    continue
            self.upd(lvl, state="rebuying", buy_cid=cid)
        # 3) sells: only inside the band, only while coins out stay above the floor
        out = sum(1 for r in self.rows() if r[1] in ("sold", "rebuying"))
        live_sells = [r for r in self.rows("selling")]
        lo, hi = c["band"]
        in_band = lo <= mid <= hi
        self.step_rungs(mid, in_band)
        if not in_band:
            for lvl, st, sp, rp, scid, bcid, q in live_sells:
                try:
                    self.ex.cancel(self.sym, scid)
                except Exception:
                    pass
            return "paused (price outside band)"
        cap_out = int((c["start_coins"] - c["floor"]) // c["size"])
        room = min(c.get("sell_orders_live", 6) - len(live_sells), cap_out - out - len(live_sells))
        room += self.make_room("selling", [r[2] for r in self.rows("armed") if r[2] > mid], room, far=max,
                               rows=self.rows, upd=self.upd, cid_col=4, cid_key="sell_cid")
        for lvl, st, sp, rp, scid, bcid, q in self.rows("armed"):
            if room <= 0:
                break
            if sp <= mid:                               # never place a sell below the market (post-only would reject anyway)
                continue
            cid = self.cid("S")
            try:
                self.ex.limit_maker(self.sym, "SELL", self.ex.qty(self.sym, q), self.ex.px(self.sym, sp, up=True), cid)
                self.upd(lvl, state="selling", sell_cid=cid)
                room -= 1
            except Exception as e:
                self.log(f"mm grid: sell L{lvl} not placed: {e}")
        # 4) re-centre down when nothing is outstanding and the market fell well below the ladder
        if out == 0 and not self.rows("rebuying"):
            anchor, _, _ = self.meta()
            if mid < anchor / (1 + c["step"]) ** c.get("recenter_steps", 3):
                for lvl, st, sp, rp, scid, bcid, q in self.rows("selling"):
                    try:
                        self.ex.cancel(self.sym, scid)
                    except Exception:
                        pass
                    self.upd(lvl, state="armed", sell_cid=None)
                self.set_anchor(mid)
        return "ok"
