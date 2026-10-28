"""
MM-Bee: Bilal's sell-first ZIG ladder (Python 3.9 stdlib only). Runs on the same exchange interface as the other
market makers (mexc_exchange.Mexc live, mm_paper.PaperEx on paper).

Rules (defaults below; any can be set per coin in mm.json with "mode": "bee"):
  * base = the previous day's closing price (MEXC daily candle, UTC);
  * sell first: `units` (20) sell orders of `size` (15,000) coins each, the first `first` (3%) above the base and each
    next one `gap` (2% of the base) above the one before: base x (1.03, 1.05, 1.07, ...). With "gap_from": "prev"
    each step is 2% of the previous sell instead: base x 1.03 x 1.02^k;
  * when a sell fills at S, a buy of the same size is placed `rebuy` (3.1%) lower: S x (1 - 0.031);
  * when that buy fills at B, the round is booked (profit = size x (S - B)) and a sell is placed `resell` (3%) higher:
    B x 1.03; when it fills, the next buy goes 3.1% under it, and so on: every unit loops sell -> buy -> sell;
  * all sold and nothing bought back: the buys simply wait;
  * no fill at all for `idle_hours` (12): the base is revised to the latest hourly close and the unsold ladder orders
    are moved to the new ladder (units in a sell/buy cycle keep their prices). If by then every unit of the ladder is
    sold (the coins are all out, waiting for buys), a fresh ladder of `units` x `size` coins is opened from the new
    base instead, at most `extra_ladders` (1) times;
  * coins used: units x size per ladder (300,000 ZIG), never more than the wallet holds; USDT: buys are paid from what
    the sells raised (a buy is always 3.1% cheaper than its sell), and the bot never spends more than `max_usdt`
    (10,000) of the wallet's own USDT beyond that;
  * every order is post-only (LIMIT_MAKER). A buy whose price the market already went under, or a resell whose price
    it already went over, is placed as a plain LIMIT at the planned price (it only ever trades at that price or
    better). A first-ladder sell that the market is already above is not chased: it waits until it can rest.
State lives in the bot's database (tables bee_units, bee_meta, bee_log), so restarts are safe. Kill switch: the STOP
file cancels every order this ladder placed.
"""
import os
import time
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STOP = os.path.join(ROOT, "MM_STOP")

DEFAULTS = {"mode": "bee", "units": 20, "size": 15000, "first": 0.03, "gap": 0.02, "gap_from": "base",
            "rebuy": 0.031, "resell": 0.03, "idle_hours": 12, "max_usdt": 10000, "extra_ladders": 1}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bee_units(symbol TEXT, ladder INTEGER, k INTEGER, state TEXT, kind TEXT, sell_px REAL,
    buy_px REAL, sell_cid TEXT, buy_cid TEXT, qty REAL, cycles INTEGER, updated_utc TEXT, PRIMARY KEY(symbol, ladder, k));
CREATE TABLE IF NOT EXISTS bee_meta(symbol TEXT PRIMARY KEY, base REAL, base_utc TEXT, base_from TEXT, ladders INTEGER,
    rounds INTEGER, profit_usdt REAL, proceeds_usdt REAL, spent_usdt REAL, last_fill_utc TEXT, active_utc TEXT);
CREATE TABLE IF NOT EXISTS bee_log(utc TEXT, symbol TEXT, event TEXT, ladder INTEGER, k INTEGER, price REAL, qty REAL,
    profit REAL, note TEXT);
"""
# unit states: armed (holds coins, ladder sell not resting yet) -> selling -> sold (coins out, buy to place) -> buying
#              -> bought (coins back, resell to place) -> selling -> ...   kind: "ladder" (first sell) or "cycle"
DONE = ("CANCELED", "EXPIRED", "REJECTED")


def utc(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def ladder_prices(base, c, n=None):
    n = c["units"] if n is None else n
    if c.get("gap_from") == "prev":
        return [base * (1 + c["first"]) * (1 + c["gap"]) ** k for k in range(n)]
    return [base * (1 + c["first"] + c["gap"] * k) for k in range(n)]


def last_close(ex, symbol, interval, clock=time.time):
    """Close of the last COMPLETED candle (MEXC: "1d" days in UTC, "60m" hours)."""
    rows = ex.klines(symbol, interval, 3)
    t = clock() * 1000
    done = [r for r in rows if r[2] <= t]
    if not done:
        raise RuntimeError(f"mm bee: no completed {interval} candle for {symbol}")
    return done[-1][1]


class BeeLadder:
    def __init__(self, ex, c, con, log=print, clock=time.time):
        self.ex, self.c, self.con, self.log, self.clock = ex, dict(DEFAULTS, **c), con, log, clock
        self.sym = c["symbol"]
        con.executescript(SCHEMA)
        self.seq = 0
        self.events = []

    def now(self):
        return utc(self.clock())

    # ---------------------------------------------------------------- state
    def meta(self):
        r = self.con.execute("SELECT base, base_utc, base_from, ladders, rounds, profit_usdt, proceeds_usdt, spent_usdt, "
                             "last_fill_utc, active_utc FROM bee_meta WHERE symbol=?", (self.sym,)).fetchone()
        keys = ("base", "base_utc", "base_from", "ladders", "rounds", "profit_usdt", "proceeds_usdt", "spent_usdt", "last_fill_utc", "active_utc")
        return dict(zip(keys, r)) if r else None

    def set_meta(self, **kw):
        sets = ", ".join(f"{k}=?" for k in kw)
        self.con.execute(f"UPDATE bee_meta SET {sets} WHERE symbol=?", list(kw.values()) + [self.sym])

    def units(self, state=None):
        q = "SELECT ladder, k, state, kind, sell_px, buy_px, sell_cid, buy_cid, qty, cycles FROM bee_units WHERE symbol=?" + \
            (" AND state=?" if state else "") + " ORDER BY ladder, k"
        return self.con.execute(q, (self.sym, state) if state else (self.sym,)).fetchall()

    def upd(self, ladder, k, **kw):
        sets = ", ".join(f"{x}=?" for x in kw) + ", updated_utc=?"
        self.con.execute(f"UPDATE bee_units SET {sets} WHERE symbol=? AND ladder=? AND k=?", list(kw.values()) + [self.now(), self.sym, ladder, k])

    def note(self, event, ladder, k, price, qty, profit=0.0, note=""):
        self.con.execute("INSERT INTO bee_log VALUES(?,?,?,?,?,?,?,?,?)", (self.now(), self.sym, event, ladder, k, price, qty, profit, note))
        if event in ("sold", "bought"):
            self.events.append(f"{self.sym.replace('USDT', '')} bee: {event} {qty:,.0f} @ {price:.6g}" + (f" (+{profit:,.2f} USDT)" if profit else ""))

    def cid(self, side):
        self.seq += 1
        return f"bm{side}{int(self.clock()) % 10**8}{self.seq % 10**4:04d}"

    def status(self, cid):
        try:
            o = self.ex.order(self.sym, cid)
            return o.get("status"), float(o.get("executedQty") or 0)
        except Exception:
            return None, 0.0

    def coins_free(self):
        """Free coins in the wallet not already owned by a unit waiting to sell (armed or bought)."""
        inf = self.ex.info(self.sym)
        held = sum(u[8] for u in self.units() if u[2] in ("armed", "bought"))
        return self.ex.balances().get(inf["base"], (0.0, 0.0))[0] - held

    # ---------------------------------------------------------------- ladders
    def open_ladder(self, base, base_from):
        c, m = self.c, self.meta()
        n_ladder = 0 if m is None else m["ladders"]
        if m is None:
            self.con.execute("INSERT INTO bee_meta VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             (self.sym, base, self.now(), base_from, 1, 0, 0.0, 0.0, 0.0, None, self.now()))
        else:
            self.set_meta(base=base, base_utc=self.now(), base_from=base_from, ladders=n_ladder + 1, active_utc=self.now())
        for k, px in enumerate(ladder_prices(base, c)):
            self.con.execute("INSERT INTO bee_units VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (self.sym, n_ladder, k, "armed", "ladder", px, None, None, None, float(c["size"]), 0, self.now()))
        self.note("ladder", n_ladder, 0, base, c["units"] * c["size"], 0, f"base {base:.6g} ({base_from})")
        self.con.commit()

    def revise(self, mid):
        """12 hours without a fill: new base from the latest hourly close."""
        c, m = self.c, self.meta()
        base = last_close(self.ex, self.sym, "60m", self.clock)
        latest = m["ladders"] - 1
        holding = [u for u in self.units() if u[0] == latest and u[2] in ("armed", "selling", "bought")]
        if not holding and m["ladders"] <= c["extra_ladders"]:
            if self.coins_free() + 1e-9 >= c["units"] * c["size"]:
                self.open_ladder(base, "hourly close, 12 h without a fill, all sold: new ladder")
                return "new ladder"
            self.note("revise", latest, 0, base, 0, 0, "new ladder needed but the wallet lacks the coins")
        movable = []
        for u in self.units():
            ladder, k, st, kind, sp, bp, scid, bcid, q, cyc = u
            if kind != "ladder" or st not in ("armed", "selling"):
                continue
            if st == "selling":
                try:
                    self.ex.cancel(self.sym, scid)
                except Exception:
                    pass
                s, filled = self.status(scid)
                if s == "FILLED" or filled > 0:          # it traded while we moved it: leave it to the progress step
                    continue
                if s not in DONE:
                    continue
            movable.append(u)
        for (ladder, k, *_), px in zip(movable, ladder_prices(base, c, len(movable))):
            self.upd(ladder, k, state="armed", sell_px=px, sell_cid=None)
        self.set_meta(base=base, base_utc=self.now(), base_from="hourly close, 12 h without a fill", active_utc=self.now())
        self.note("revise", latest, 0, base, len(movable) * c["size"], 0, f"{len(movable)} ladder orders moved")
        self.con.commit()
        return "revised"

    # ---------------------------------------------------------------- one cycle
    def step(self, mid):
        c = self.c
        if os.path.exists(STOP):
            for u in self.units():
                for x in (u[6], u[7]):
                    if x:
                        try:
                            self.ex.cancel(self.sym, x)
                        except Exception:
                            pass
            return "stopped"
        if self.meta() is None:
            if self.coins_free() + 1e-9 < c["units"] * c["size"]:
                self.log(f"mm bee: {self.sym}: wallet holds fewer than {c['units'] * c['size']:,.0f} coins; not starting")
                return "not enough coins"
            self.open_ladder(last_close(self.ex, self.sym, "1d", self.clock), "previous daily close")
        m = self.meta()
        filled_any = False
        # 1) progress
        for ladder, k, st, kind, sp, bp, scid, bcid, q, cyc in self.units():
            if st == "selling" and scid:
                s, filled = self.status(scid)
                if s == "FILLED" or (s in DONE and filled > 0):
                    qq = q if s == "FILLED" else filled
                    self.upd(ladder, k, state="sold", sell_cid=None, qty=qq, buy_px=sp * (1 - c["rebuy"]))
                    m["proceeds_usdt"] += qq * sp
                    self.set_meta(proceeds_usdt=m["proceeds_usdt"])
                    self.note("sold", ladder, k, sp, qq, 0, kind)
                    filled_any = True
                elif s in DONE:
                    self.upd(ladder, k, state="armed" if kind == "ladder" else "bought", sell_cid=None)
            elif st == "buying" and bcid:
                s, filled = self.status(bcid)
                if s == "FILLED" or (s in DONE and filled > 0):
                    qq = q if s == "FILLED" else filled
                    gain = qq * (sp - bp)
                    m["rounds"] += 1
                    m["profit_usdt"] += gain
                    m["spent_usdt"] += qq * bp
                    self.set_meta(rounds=m["rounds"], profit_usdt=m["profit_usdt"], spent_usdt=m["spent_usdt"])
                    self.upd(ladder, k, state="bought", kind="cycle", buy_cid=None, qty=qq, sell_px=bp * (1 + c["resell"]), cycles=cyc + 1)
                    self.note("bought", ladder, k, bp, qq, gain)
                    filled_any = True
                elif s in DONE:
                    self.upd(ladder, k, state="sold", buy_cid=None)
        if filled_any:
            self.set_meta(last_fill_utc=self.now(), active_utc=self.now())
            m = self.meta()
        self.con.commit()
        # 2) buys for sold units (paid from the sells; never more than max_usdt of the wallet's own USDT)
        for ladder, k, st, kind, sp, bp, scid, bcid, q, cyc in self.units("sold"):
            resting = sum(u[8] * u[5] for u in self.units("buying"))
            if m["spent_usdt"] + resting + q * bp - m["proceeds_usdt"] > c["max_usdt"]:
                continue
            px, qq = self.ex.px(self.sym, bp), self.ex.qty(self.sym, q)
            cid = self.cid("B")
            try:
                self.ex.limit_maker(self.sym, "BUY", qq, px, cid)
            except Exception:
                try:                                    # market already under the planned buy: buy at that price
                    self.ex.limit(self.sym, "BUY", qq, px, cid)
                except Exception as e:
                    self.log(f"mm bee: buy {ladder}/{k} not placed: {e}")
                    continue
            self.upd(ladder, k, state="buying", buy_cid=cid, buy_px=float(px))     # the price as placed (tick-rounded)
        # 3) resells for bought units
        for ladder, k, st, kind, sp, bp, scid, bcid, q, cyc in self.units("bought"):
            px, qq = self.ex.px(self.sym, sp, up=True), self.ex.qty(self.sym, q)
            cid = self.cid("S")
            try:
                self.ex.limit_maker(self.sym, "SELL", qq, px, cid)
            except Exception:
                try:                                    # market already over the planned sell: sell at that price
                    self.ex.limit(self.sym, "SELL", qq, px, cid)
                except Exception as e:
                    self.log(f"mm bee: resell {ladder}/{k} not placed: {e}")
                    continue
            self.upd(ladder, k, state="selling", sell_cid=cid, sell_px=float(px))
        # 4) first-ladder sells that can rest above the market
        for ladder, k, st, kind, sp, bp, scid, bcid, q, cyc in self.units("armed"):
            if sp <= mid:
                continue
            cid, px = self.cid("S"), self.ex.px(self.sym, sp, up=True)
            try:
                self.ex.limit_maker(self.sym, "SELL", self.ex.qty(self.sym, q), px, cid)
                self.upd(ladder, k, state="selling", sell_cid=cid, sell_px=float(px))
            except Exception as e:
                self.log(f"mm bee: sell {ladder}/{k} not placed: {e}")
        self.con.commit()
        # 5) 12 hours without a fill: revise the base
        if self.clock() - ts(m["active_utc"]) >= c["idle_hours"] * 3600:
            return self.revise(mid)
        return "ok"
