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
Version 2 (Bilal, 29 Oct 2026), switched on with "rebase_hours" (default 4):
  * the base follows every 4-hour close: at each new 4h close the unsold first-ladder sells (and accumulation buys not
    yet filled) are re-priced from that close. Units already sold keep their buy-back, units in a loop keep theirs;
    the ladder is rebuilt from +3% for the units still unsold, so the more has been sold the fewer orders move;
  * the move is staggered so the book does not jump at once: the `stagger_first` (5) orders nearest the price move
    right after the close, the rest `stagger_delay_s` (150 s) later; an order whose new price is within `min_move`
    (0.3%) of its current one is left alone (it keeps its place in the queue);
  * the wallet's own USDT is not used at the start: buys only follow sells. Once `acc_after` (3) ladder sells have
    filled (a cushion exists), `acc_units` buy-first units (0 = off) may rest below the base, `acc_first` (3%) under it
    and every `acc_gap` (2%) lower, paid from at most `max_usdt` of the wallet's USDT; each one, once bought, is sold
    `resell` (3%) higher, then bought back `rebuy` lower, and loops like the others (its round is booked on the sell);
  * with rebasing on, the 12-hour rule only opens the extra ladder (when everything is sold and nothing filled);
  * orders move one at a time (cancel one, place its replacement, wait `drip_min`..`drip_max` = 2-4 s at random,
    next), so a rebase reads as ordinary order flow rather than the whole ladder jumping at once;
  * BTC guard (`btc_guard` 2%): when BTC rises 2%+ over about two hours while ZIG is up less than `btc_zig_max` (1%),
    every sell is pulled for `btc_pause_h` (8) hours, so ZIG is not sold cheap before it follows BTC (buys stay);
  * volatility mode (`vol_fast` 10%): while ZIG has moved 10%+ in the running 4-hour or day candle, the base follows
    every 1-hour close, until both are back under `vol_calm` (5%). Set either to 0 to switch it off.
State lives in the bot's database (tables bee_units, bee_meta, bee_log), so restarts are safe. Kill switch: the STOP
file cancels every order this ladder placed.
"""
import os
import random
import time
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STOP = os.path.join(ROOT, "MM_STOP")

DEFAULTS = {"mode": "bee", "units": 20, "size": 15000, "first": 0.03, "gap": 0.02, "gap_from": "base",
            "rebuy": 0.031, "resell": 0.03, "idle_hours": 12, "max_usdt": 10000, "extra_ladders": 1,
            "rebase_hours": 4, "stagger_first": 5, "stagger_delay_s": 150, "min_move": 0.003,
            "acc_units": 0, "acc_after": 3, "acc_first": 0.03, "acc_gap": 0.02, "recycle": "off",
            "btc_guard": 0.02, "btc_symbol": "BTCUSDT", "btc_zig_max": 0.01, "btc_pause_h": 8,
            "vol_fast": 0.10, "vol_calm": 0.05, "drip_min": 2.0, "drip_max": 4.0,
            "regimes": None, "regime_ema": 30, "regime_band": 0.03, "chase": 0.0,
            "max_chases_day": 6, "sanity": 0.15, "circuit": 0.25, "circuit_pause_h": 2, "taker_reserve": 0.002}
# Safety (live hardening, 30 Oct 2026):
#   max_chases_day  at most this many upside-stop buy-backs per coin per UTC day; then no more that day (a choppy
#                   market cannot bleed the position through repeated buy-backs)
#   sanity          the bot does nothing while the book's mid is more than this away from the last 1h close (a broken
#                   or empty book, a bad print): no order is moved or placed on a price it cannot trust
#   circuit         a move of more than this in the running or last 1h candle pulls every sell and stops buy-backs
#                   for circuit_pause_h hours (flash pump / crash); resting buy-backs below stay
#   taker_reserve   an order that may take liquidity (bmT...) can pay its fee in the coin: the unit keeps this much
#                   less than it bought, so its resell never asks for coins the wallet does not have
# "chase" (0 = off): upside stop. A unit that sold and waits to buy back lower, while the price has run `chase` above
# its sell price, buys back at once at the market (taker), taking a small, capped loss instead of staying out of a rally.
# "regimes": {"up": {...}, "down": {...}, "flat": {...}} switches settings by market state at each rebase: "up" while
# the 4h close is more than regime_band above its regime_ema-candle EMA, "down" while that much below, else "flat".
META_COLS = ("base", "base_utc", "base_from", "ladders", "rounds", "profit_usdt", "proceeds_usdt", "spent_usdt",
             "last_fill_utc", "active_utc", "rebase_ms", "phase2", "ladder_sold", "acc_opened", "pause_until", "fast",
             "guard_ms")

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
DONE = ("CANCELED", "EXPIRED", "REJECTED", "PARTIALLY_CANCELED")
import re  # noqa: E402


def rejected(e):
    """True only when the exchange clearly refused the order (it did not land). A server error (5xx), a rate limit,
    an unreadable reply or a lost connection may hide an order that DID land: those are "unknown", never "rejected"."""
    m = str(e)
    if re.search(r"would cross|post.?only|insufficient|duplicate client|would match|quantity / price", m, re.I):
        return True
    code = re.search(r"MEXC error (-?\d+)", m)
    if code:
        n = int(code.group(1))
        return not (500 <= n <= 599 or n == 429 or n == 418)
    return False


def crossed(e):
    return bool(re.search(r"would cross|post.?only|would match|30087", str(e), re.I))
# placing_s / placing_b: an order was sent but not confirmed (written BEFORE the exchange call, so a crash or a lost
# reply can never leave an order on the exchange that the database does not know). The next step asks the exchange:
# found -> selling / buying; MISSING -> never placed, back to where it was. A confirmed order the exchange no longer
# knows ("unknown") is left alone and reported: the bot never guesses whether coins or USDT moved.
PLACING = {"placing_s": "selling", "placing_b": "buying"}


def utc(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def ladder_prices(base, c, n=None):
    n = c["units"] if n is None else n
    if c.get("gap_from") == "prev":
        return [base * (1 + c["first"]) * (1 + c["gap"]) ** k for k in range(n)]
    return [base * (1 + c["first"] + c["gap"] * k) for k in range(n)]


def acc_prices(base, c, n=None):
    n = c["acc_units"] if n is None else n
    return [base * (1 - c["acc_first"] - c["acc_gap"] * k) for k in range(n)]


def last_candle(ex, symbol, interval, clock=time.time):
    """(close, close_ms) of the last COMPLETED candle."""
    rows = ex.klines(symbol, interval, 3)
    t = clock() * 1000
    done = [r for r in rows if r[2] <= t]
    if not done:
        raise RuntimeError(f"mm bee: no completed {interval} candle for {symbol}")
    return done[-1][1], done[-1][2]


def last_close(ex, symbol, interval, clock=time.time):
    """Close of the last COMPLETED candle (MEXC: "1d" days in UTC, "60m" hours)."""
    rows = ex.klines(symbol, interval, 3)
    t = clock() * 1000
    done = [r for r in rows if r[2] <= t]
    if not done:
        raise RuntimeError(f"mm bee: no completed {interval} candle for {symbol}")
    return done[-1][1]


class BeeLadder:
    def __init__(self, ex, c, con, log=print, clock=time.time, sleep=time.sleep):
        self.ex, self.c, self.con, self.log, self.clock, self.sleep = ex, dict(DEFAULTS, **c), con, log, clock, sleep
        self.c0, self.regime = dict(self.c), None
        self.sym = c["symbol"]
        con.executescript(SCHEMA)
        have = {r[1] for r in con.execute("PRAGMA table_info(bee_meta)")}
        for col, typ in (("rebase_ms", "INTEGER"), ("phase2", "INTEGER"), ("ladder_sold", "INTEGER"), ("acc_opened", "INTEGER"),
                         ("pause_until", "REAL"), ("fast", "INTEGER"), ("guard_ms", "INTEGER")):
            if col not in have:                                  # v1 database: add the v2 columns
                con.execute(f"ALTER TABLE bee_meta ADD COLUMN {col} {typ} DEFAULT 0")
        con.commit()
        self.seq = 0
        self.events = []

    def now(self):
        return utc(self.clock())

    # ---------------------------------------------------------------- state
    def meta(self):
        r = self.con.execute("SELECT " + ", ".join(META_COLS) + " FROM bee_meta WHERE symbol=?", (self.sym,)).fetchone()
        if not r:
            return None
        m = dict(zip(META_COLS, r))
        for k in ("rebase_ms", "phase2", "ladder_sold", "acc_opened", "pause_until", "fast", "guard_ms"):
            m[k] = m[k] or 0
        return m

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

    def snapshot(self):
        """One open-orders call per step: orders still resting are known from it, so the exchange is asked about a
        single order only when it left the book (filled or cancelled). Keeps the bot far under MEXC's rate limits."""
        self._open = None
        if hasattr(self.ex, "open_orders"):
            try:
                self._open = {str(o.get("clientOrderId")): o for o in self.ex.open_orders(self.sym)}
            except Exception as e:
                self.log(f"mm bee: {self.sym} open orders unavailable: {str(e)[:80]}")

    def cancel(self, cid):
        if getattr(self, "_open", None) is not None:
            self._open.pop(cid, None)
        return self.ex.cancel(self.sym, cid)

    def status(self, cid):
        """(status, executed qty). "MISSING" when the exchange says the order does not exist; None when it could
        not be asked (network): then nothing is changed this step."""
        o = (getattr(self, "_open", None) or {}).get(cid)
        if o is not None:
            return o.get("status") or "NEW", float(o.get("executedQty") or 0)
        try:
            o = self.ex.order(self.sym, cid)
            return o.get("status"), float(o.get("executedQty") or 0)
        except KeyError:
            return "MISSING", 0.0
        except Exception as e:
            m = str(e).lower()
            return ("MISSING", 0.0) if ("-2013" in m or "does not exist" in m or "not exist" in m or "unknown order" in m) else (None, 0.0)

    def send(self, ladder, k, side, kind_state, fields, call, revert, restore=None):
        """Place an order the safe way: record the intent first, then call the exchange. Only a clear rejection
        reverts the unit (and raises); anything else (no reply, a server error, a rate limit) keeps the intent and
        returns False: the next step asks the exchange whether the order landed. Never sent twice."""
        self.upd(ladder, k, state="placing_" + side.lower()[0], **fields)
        self.con.commit()
        try:
            call()
        except Exception as e:
            if not rejected(e):
                self.log(f"mm bee: {self.sym} {side.lower()} {ladder}/{k}: outcome unknown ({str(e)[:80]}); will check the exchange")
                return False
            self.upd(ladder, k, state=revert, **{x: None for x in fields if x.endswith("_cid")}, **(restore or {}))
            self.con.commit()
            raise
        self.upd(ladder, k, state=kind_state)
        self.con.commit()
        return True

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
            self.con.execute("INSERT INTO bee_meta(symbol, " + ", ".join(META_COLS) + ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (self.sym, base, self.now(), base_from, 1, 0, 0.0, 0.0, 0.0, None, self.now(), 0, 0, 0, 0, 0, 0, 0))
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
        if c["rebase_hours"]:                    # v2: the 4-hour rebase moves the orders; nothing more to do here
            self.set_meta(active_utc=self.now())
            self.con.commit()
            return "idle"
        movable = []
        for u in self.units():
            ladder, k, st, kind, sp, bp, scid, bcid, q, cyc = u
            if kind != "ladder" or st not in ("armed", "selling"):
                continue
            if st == "selling":
                try:
                    self.cancel(scid)
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
                            self.cancel(x)
                        except Exception:
                            pass
            return "stopped"
        if self.meta() is None:
            if self.coins_free() + 1e-9 < c["units"] * c["size"]:
                self.log(f"mm bee: {self.sym}: wallet holds fewer than {c['units'] * c['size']:,.0f} coins; not starting")
                return "not enough coins"
            self.open_ladder(last_close(self.ex, self.sym, "1d", self.clock), "previous daily close")
        m = self.meta()
        if not self.trusted(mid):
            return "price not trusted"
        filled_any = False
        self.snapshot()
        # 0) sent-but-unconfirmed orders: ask the exchange
        for ladder, k, st, kind, sp, bp, scid, bcid, q, cyc in self.units():
            if st in PLACING:
                cid = scid if st == "placing_s" else bcid
                s, _ = self.status(cid)
                if s == "MISSING":
                    back = ("armed" if kind == "ladder" else "bought") if st == "placing_s" else "sold"
                    self.upd(ladder, k, state=back, **({"sell_cid": None} if st == "placing_s" else {"buy_cid": None}))
                    self.note("not placed", ladder, k, sp if st == "placing_s" else bp, q, 0, "the exchange never got it; retried")
                elif s is not None:
                    self.upd(ladder, k, state=PLACING[st])
        self.con.commit()
        # 1) progress
        got = {}
        for ladder, k, st, kind, sp, bp, scid, bcid, q, cyc in self.units():
            if st in ("selling", "buying") and (scid if st == "selling" else bcid):
                got[scid if st == "selling" else bcid] = self.status(scid if st == "selling" else bcid)
            if st in ("selling", "buying") and got.get(scid if st == "selling" else bcid, (None,))[0] == "MISSING":
                self.upd(ladder, k, state="unknown")
                self.note("unknown", ladder, k, sp if st == "selling" else bp, q, 0,
                          f"the exchange no longer knows order {scid if st == 'selling' else bcid}: unit set aside, check by hand")
                self.events.append(f"{self.sym.replace('USDT', '')} bee: ALERT order {scid if st == 'selling' else bcid} unknown to the exchange; unit {ladder}/{k} set aside")
                continue
            if st == "selling" and scid:
                s, filled = got[scid]
                if s == "FILLED" or (s in DONE and filled > 0):
                    qq = q if s == "FILLED" else filled
                    gain = qq * (sp - bp) if kind == "acc" and bp else 0.0     # an accumulation unit books on its sell
                    if gain:
                        m["rounds"] += 1
                        m["profit_usdt"] += gain
                    if kind == "ladder":
                        m["ladder_sold"] += 1
                    self.upd(ladder, k, state="sold", sell_cid=None, qty=qq, buy_px=sp * (1 - c["rebuy"]), cycles=cyc + (kind == "acc"))
                    if s != "FILLED" and q - qq > 0:               # partly filled, then cancelled: the rest keeps selling
                        self.split(ladder, k, q - qq, "armed" if kind == "ladder" else "bought", kind, sp, bp, sp)
                    m["proceeds_usdt"] += qq * sp
                    self.set_meta(proceeds_usdt=m["proceeds_usdt"], rounds=m["rounds"], profit_usdt=m["profit_usdt"], ladder_sold=m["ladder_sold"])
                    self.note("sold", ladder, k, sp, qq, gain, kind)
                    filled_any = True
                elif s in DONE:
                    self.upd(ladder, k, state="armed" if kind == "ladder" else "bought", sell_cid=None)
            elif st == "buying" and bcid:
                s, filled = got[bcid]
                if s == "FILLED" or (s in DONE and filled > 0):
                    got_q = q if s == "FILLED" else filled
                    if s != "FILLED" and q - got_q > 0:            # partly filled, then cancelled: the rest keeps buying
                        self.split(ladder, k, q - got_q, "sold", kind, sp, bp, None)
                    qq = got_q
                    if str(bcid).startswith("bmT") and c.get("taker_reserve"):     # it may have paid its fee in the coin
                        qq = float(self.ex.qty(self.sym, qq * (1 - c["taker_reserve"])))
                    acc = kind == "acc"
                    gain = 0.0 if acc else got_q * (sp - bp) - (got_q - qq) * bp     # the fee reserve is a cost
                    if not acc:
                        m["rounds"] += 1
                        m["profit_usdt"] += gain
                    m["spent_usdt"] += got_q * bp
                    self.set_meta(rounds=m["rounds"], profit_usdt=m["profit_usdt"], spent_usdt=m["spent_usdt"])
                    self.upd(ladder, k, state="bought", kind="acc" if acc else "cycle", buy_cid=None, qty=qq,
                             sell_px=bp * (1 + c["resell"]), cycles=cyc + (0 if acc else 1))
                    self.note("bought", ladder, k, bp, qq, gain, kind)
                    filled_any = True
                elif s in DONE:
                    self.upd(ladder, k, state="sold", buy_cid=None)
        if filled_any:
            self.set_meta(last_fill_utc=self.now(), active_utc=self.now())
            m = self.meta()
        self.con.commit()
        # 1b) upside stop: buy back units the market has run away from
        if c.get("chase"):
            self.chase(mid)
        # 2) buys for sold units (paid from the sells; never more than max_usdt of the wallet's own USDT)
        self.place_buys(mid)
        # 3) resells for bought units
        self.place_resells(mid)
        # 4) first-ladder sells that can rest above the market
        self.place_armed(mid)
        return self.after(mid, m)

    def chases_today(self):
        return self.con.execute("SELECT COUNT(*) FROM bee_log WHERE symbol=? AND event='chase' AND utc>=?",
                                (self.sym, self.now()[:10])).fetchone()[0]

    def chase(self, mid):
        c = self.c
        if self.paused():
            return
        n_today = self.chases_today()
        for ladder, k, st, kind, sp, bp, scid, bcid, q, cyc in self.units():
            if c.get("max_chases_day") and n_today >= c["max_chases_day"]:
                if not self.con.execute("SELECT 1 FROM bee_log WHERE symbol=? AND event='chase cap' AND utc>=?", (self.sym, self.now()[:10])).fetchone():
                    self.note("chase cap", ladder, k, mid, 0, 0, f"{n_today} buy-backs today: no more until 00:00 UTC")
                break
            if st not in ("sold", "buying") or kind == "acc" or not sp or mid < sp * (1 + c["chase"]):
                continue
            if st == "buying" and bcid:
                try:
                    self.cancel(bcid)
                except Exception:
                    pass
                s, filled = self.status(bcid)
                if s == "FILLED" or filled > 0 or s not in DONE:
                    continue                            # it traded (or is still live): the progress step settles it
                self.upd(ladder, k, state="sold", buy_cid=None)    # its own buy is off the book: not in the budget
            px = self.ex.px(self.sym, mid * 1.002, up=True)        # marketable limit, a little over the price
            free = self.ex.balances().get("USDT", (0.0, 0.0))[0]
            m = self.meta()                                         # the same budget as every buy: what the sells
            budget = m["proceeds_usdt"] + c["max_usdt"] - m["spent_usdt"] - self.resting_buys()   # raised, + max_usdt
            qq = self.ex.qty(self.sym, max(0.0, min(q, free / float(px) if px else 0, budget / float(px) if px else 0)))
            if float(qq) * float(px) < 1.0:
                self.upd(ladder, k, state="sold", buy_cid=None)
                continue
            cid = self.cid("T")
            try:
                self.send(ladder, k, "BUY", "buying", {"buy_cid": cid, "buy_px": float(px), "qty": float(qq)},
                          lambda: self.ex.limit(self.sym, "BUY", qq, px, cid), "sold", restore={"qty": q, "buy_px": bp})
            except Exception as e:
                self.log(f"mm bee: chase {ladder}/{k} not placed: {e}")
                continue
            n_today += 1
            self.note("chase", ladder, k, float(px), float(qq), 0, f"price {mid:.6g} ran {100 * c['chase']:.0f}% over the sell {sp:.6g}")
        self.con.commit()

    def trusted(self, mid):
        """Sanity and circuit breaker, from the 1h candles (asked at most once a minute)."""
        c, t = self.c, self.clock()
        if not getattr(self, "_k1", None) or t - self._k1[0] >= 60:
            try:
                self._k1 = (t, self.ex.klines(self.sym, "60m", 2))
            except Exception as e:
                self.log(f"mm bee: {self.sym} 1h candles unavailable: {str(e)[:80]}")
                self._k1 = (t, getattr(self, "_k1", (0, []))[1])
        rows = [r for r in self._k1[1] if len(r) > 3]
        if not mid or mid <= 0:
            return False
        done = [r for r in rows if r[2] <= t * 1000]
        if c.get("sanity") and done and abs(mid / done[-1][1] - 1) > c["sanity"]:
            if t - getattr(self, "_untrusted_note", 0) > 3600:
                self._untrusted_note = t
                self.note("price check", 0, 0, mid, 0, 0, f"mid {mid:.6g} is {100 * (mid / done[-1][1] - 1):+.1f}% from the last 1h close {done[-1][1]:.6g}: no action")
                self.events.append(f"{self.sym.replace('USDT', '')} bee: price check failed (mid {mid:.6g} vs 1h close {done[-1][1]:.6g}); waiting")
            return False
        if c.get("circuit") and rows and not self.paused():
            mv = max(abs(r[1] / r[3] - 1) if r[3] else 0.0 for r in rows[-2:])
            if mv > c["circuit"]:
                m = self.meta()
                self.set_meta(pause_until=t + c["circuit_pause_h"] * 3600)
                n = self.pull_sells()
                self.note("circuit", m["ladders"] - 1 if m else 0, 0, mid, 0, 0,
                          f"{100 * mv:.0f}% in an hour: {n} sells pulled, no buy-backs for {c['circuit_pause_h']} h")
                self.events.append(f"{self.sym.replace('USDT', '')} bee: CIRCUIT {100 * mv:.0f}% move in 1h, sells pulled for {c['circuit_pause_h']} h")
        return True

    def reconcile(self):
        """At start: cancel this bot's orders ("bm" prefix) the exchange holds for this coin but the database does not
        know (left by a crash or an old database). Returns how many were cancelled."""
        if not hasattr(self.ex, "open_orders"):
            return 0
        known = set()
        for u in self.units():
            known.update(x for x in (u[6], u[7]) if x)
        n = 0
        for o in self.ex.open_orders(self.sym):
            cid = str(o.get("clientOrderId", ""))
            if cid.startswith("bm") and cid not in known:
                try:
                    self.cancel(cid)
                    n += 1
                except Exception as e:
                    self.log(f"mm bee: orphan {cid} not cancelled: {e}")
        if n:
            self.note("reconcile", 0, 0, 0, 0, 0, f"{n} order(s) on the exchange unknown to the database: cancelled")
            self.con.commit()
        return n

    def place_buys(self, mid):
        c, m = self.c, self.meta()
        for ladder, k, st, kind, sp, bp, scid, bcid, q, cyc in self.units("sold"):
            if m["spent_usdt"] + self.resting_buys() + q * bp - m["proceeds_usdt"] > c["max_usdt"]:
                continue
            px, qq = self.ex.px(self.sym, bp), self.ex.qty(self.sym, q)
            take = mid is not None and float(px) >= mid       # the market is already under the planned buy
            cid = self.cid("T" if take else "B")
            try:
                self.send(ladder, k, "BUY", "buying", {"buy_cid": cid, "buy_px": float(px)},
                          (lambda: self.ex.limit(self.sym, "BUY", qq, px, cid)) if take else
                          (lambda: self.ex.limit_maker(self.sym, "BUY", qq, px, cid)), "sold")
            except Exception as e:
                if take or not crossed(e):
                    self.log(f"mm bee: buy {ladder}/{k} not placed: {e}")
                    continue
                cid = self.cid("T")                     # the post-only buy would have crossed: buy at that price
                try:
                    self.send(ladder, k, "BUY", "buying", {"buy_cid": cid, "buy_px": float(px)},
                              lambda: self.ex.limit(self.sym, "BUY", qq, px, cid), "sold")
                except Exception as e2:
                    self.log(f"mm bee: buy {ladder}/{k} not placed: {e2}")
                    continue
        self.con.commit()

    def split(self, ladder, k, rest, state, kind, sp, bp, sell_px):
        """A partly filled order was cancelled: the unfilled part becomes its own unit (same ladder, a new number),
        so no coin and no USDT silently leaves the loop. Dust under $1 is left out."""
        px = sp if state in ("armed", "bought") else bp
        if not px or rest * px < 1.0:
            return
        k2 = 1 + self.con.execute("SELECT COALESCE(MAX(k), 0) FROM bee_units WHERE symbol=? AND ladder=?", (self.sym, ladder)).fetchone()[0]
        k2 = max(k2, 100)
        self.con.execute("INSERT INTO bee_units VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (self.sym, ladder, k2, state, kind, sell_px if sell_px is not None else sp, bp, None, None, float(rest), 0, self.now()))
        self.note("split", ladder, k2, px, rest, 0, f"rest of unit {ladder}/{k} after a partial fill")

    def resting_buys(self):
        return sum(u[8] * (u[5] or 0) for u in self.units() if u[2] in ("buying", "placing_b"))

    def place_resells(self, mid=None):
        if self.paused():
            return
        for ladder, k, st, kind, sp, bp, scid, bcid, q, cyc in self.units("bought"):
            px, qq = self.ex.px(self.sym, sp, up=True), self.ex.qty(self.sym, q)
            take = mid is not None and float(px) <= mid       # the market is already over the planned sell
            cid = self.cid("S")
            try:
                self.send(ladder, k, "SELL", "selling", {"sell_cid": cid, "sell_px": float(px)},
                          (lambda: self.ex.limit(self.sym, "SELL", qq, px, cid)) if take else
                          (lambda: self.ex.limit_maker(self.sym, "SELL", qq, px, cid)), "bought")
            except Exception as e:
                if take or not crossed(e):
                    self.log(f"mm bee: resell {ladder}/{k} not placed: {e}")
                    continue
                cid = self.cid("S")                     # the post-only sell would have crossed: sell at that price
                try:
                    self.send(ladder, k, "SELL", "selling", {"sell_cid": cid, "sell_px": float(px)},
                              lambda: self.ex.limit(self.sym, "SELL", qq, px, cid), "bought")
                except Exception as e2:
                    self.log(f"mm bee: resell {ladder}/{k} not placed: {e2}")
                    continue
        self.con.commit()

    def after(self, mid, m):
        c = self.c
        # 5) v2: BTC guard and the volatility mode, then follow the 4-hour close (staggered), then accumulation
        res = "ok"
        self.guards(mid)
        if c["rebase_hours"]:
            res = self.rebase(mid) or res
        if c["acc_units"] and not m["acc_opened"] and self.meta()["ladder_sold"] >= c["acc_after"]:
            self.open_acc()
            res = "accumulation opened"
        # 6) 12 hours without a fill: (v1) revise the base; always: open the extra ladder when everything is sold
        if self.clock() - ts(self.meta()["active_utc"]) >= c["idle_hours"] * 3600:
            r = self.revise(mid)
            self.place_armed(mid)
            return r
        return res

    # ---------------------------------------------------------------- v2: the 4-hour rebase
    def movable(self):
        """Orders that follow the base: unsold first-ladder sells, and accumulation buys not filled yet. With
        recycle "breakeven" or "ladder", units holding coins they bought back also rejoin the sell ladder (so a unit
        bought on the way down is not stuck far above the price): "breakeven" never below its buy price, "ladder"
        wherever the ladder puts it."""
        rec = self.c.get("recycle", "off") != "off"
        sells = [u for u in self.units() if (u[3] == "ladder" or (rec and u[3] in ("cycle", "acc"))) and u[2] in ("armed", "selling", "bought")
                 and (u[3] == "ladder" or u[2] != "armed")]
        buys = [u for u in self.units() if u[3] == "acc" and u[9] == 0 and u[2] in ("sold", "buying")]
        return sorted(sells, key=lambda u: u[4]), sorted(buys, key=lambda u: -(u[5] or 0))

    def rebase(self, mid):
        c, m = self.c, self.meta()
        try:
            iv = "60m" if m["fast"] else {1: "60m", 4: "4h", 24: "1d"}.get(int(c["rebase_hours"]), "4h")
            close, close_ms = last_candle(self.ex, self.sym, iv, self.clock)
        except Exception as e:
            self.log(f"mm bee: 4h close unavailable: {e}")
            return None
        if close_ms > m["rebase_ms"]:
            self.pick_regime()
            first = not m["rebase_ms"] and abs(close / m["base"] - 1) < 1e-12
            self.set_meta(base=close, base_utc=self.now(), base_from=f"{iv.replace('60m', '1h')} close", rebase_ms=close_ms, phase2=1)
            self.con.commit()
            if first:
                return None
            n = self.reprice(close, mid, limit=c["stagger_first"])
            if c["stagger_delay_s"] <= 0:
                n += self.reprice(close, mid)
                self.set_meta(phase2=0)
            if n:
                self.note("rebase", m["ladders"] - 1, 0, close, 0, 0, f"{iv.replace('60m', '1h')} close; {n} orders moved")
            self.con.commit()
            return "rebased"
        if m["phase2"] and self.clock() * 1000 >= m["rebase_ms"] + c["stagger_delay_s"] * 1000:
            n = self.reprice(m["base"], mid)
            self.set_meta(phase2=0)
            if n:
                self.note("rebase", m["ladders"] - 1, 0, m["base"], 0, 0, f"rest of the book: {n} orders moved")
            self.con.commit()
            return "rebased (rest)"
        return None

    def pick_regime(self):
        R = self.c0.get("regimes")
        if not R:
            return
        try:
            rows = [r for r in self.ex.klines(self.sym, "4h", self.c0["regime_ema"] * 2) if r[2] <= self.clock() * 1000]
        except Exception:
            return
        n = self.c0["regime_ema"]
        if len(rows) < n:
            return
        a, e = 2 / (n + 1), rows[0][1]
        for r in rows[1:]:
            e = a * r[1] + (1 - a) * e
        x = rows[-1][1] / e - 1
        reg = "up" if x > self.c0["regime_band"] else "down" if x < -self.c0["regime_band"] else "flat"
        if reg != self.regime:
            self.regime = reg
            self.c = dict(self.c0, **R.get(reg, {}))
            m = self.meta()
            self.note("regime", m["ladders"] - 1 if m else 0, 0, rows[-1][1], 0, 0, f"{reg} ({100 * x:+.1f}% vs the {n}-candle 4h EMA)")

    def reprice(self, base, mid, limit=None):
        """Move the nearest `limit` (all when None) movable orders of each side to the ladder from `base`."""
        c, moved = self.c, 0
        sells, buys = self.movable()
        for units, prices, side in ((sells, ladder_prices(base, c, len(sells)), "S"), (buys, acc_prices(base, c, len(buys)), "B")):
            for u, px in list(zip(units, prices))[:limit]:
                ladder, k, st, kind, sp, bp, scid, bcid, q, cyc = u
                if side == "S" and kind != "ladder" and c.get("recycle") == "breakeven" and bp:
                    px = max(px, bp)                           # a bought-back unit never sells under its cost
                cur = sp if side == "S" else bp
                if cur and abs(px / cur - 1) < c["min_move"]:
                    continue                                   # close enough: keep its place in the queue
                cid = scid if side == "S" else bcid
                if cid:
                    try:
                        self.cancel(cid)
                    except Exception:
                        pass
                    s, filled = self.status(cid)
                    if s == "FILLED" or filled > 0 or s not in DONE:
                        continue                               # it traded while we moved it: the progress step books it
                if side == "S" and kind != "ladder":
                    self.upd(ladder, k, state="bought", sell_px=px, sell_cid=None)
                    self.con.commit()
                    self.place_resells(mid)
                elif side == "S":
                    self.upd(ladder, k, state="armed", sell_px=px, sell_cid=None)
                    self.con.commit()
                    self.place_armed(mid)
                else:
                    self.upd(ladder, k, state="sold", buy_px=px, buy_cid=None)
                    self.con.commit()
                    self.place_buys(mid)
                moved += 1
                self.drip()                                    # one order at a time: cancel one, place one, wait
        self.con.commit()
        self.place_armed(mid)
        self.place_buys(mid)
        self.place_resells(mid)
        return moved

    def drip(self):
        """A short, uneven pause between two order moves (drip_min..drip_max seconds), so a rebase looks like
        ordinary order flow on the book rather than the whole ladder jumping at once."""
        lo, hi = self.c["drip_min"], self.c["drip_max"]
        if hi > 0:
            self.sleep(random.uniform(lo, hi))

    def open_acc(self):
        """Buy-first units below the base, once the cushion exists (acc_after ladder sells filled)."""
        c, m = self.c, self.meta()
        lad = 1000 + m["ladders"]                              # their own ladder number, apart from the sell ladders
        for k, px in enumerate(acc_prices(m["base"], c)):
            self.con.execute("INSERT OR IGNORE INTO bee_units VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (self.sym, lad, k, "sold", "acc", None, px, None, None, float(c["size"]), 0, self.now()))
        self.set_meta(acc_opened=1)
        self.note("accumulate", lad, 0, m["base"], c["acc_units"] * c["size"], 0,
                  f"{c['acc_units']} buy units from {100 * c['acc_first']:.1f}% under the base")
        self.con.commit()
        self.place_buys(None)

    def paused(self):
        m = self.meta()
        return bool(m and m["pause_until"] and self.clock() < m["pause_until"])

    def guards(self, mid):
        """BTC guard: BTC up btc_guard (2%) over the last ~2 hours while ZIG is up less than btc_zig_max (1%): every
        sell is pulled for btc_pause_h (8) hours, so ZIG is not sold cheap before it follows BTC. Buys stay.
        Volatility mode: ZIG has moved more than vol_fast (10%) in the running 4-hour or day candle (or the last
        closed 4-hour one): the base follows every 1-hour close until both running candles are back under
        vol_calm (5%). Checked once per minute."""
        c, m = self.c, self.meta()
        tick = int(self.clock() // 60)
        if tick == m["guard_ms"]:
            return
        self.set_meta(guard_ms=tick)
        if c["btc_guard"] and not self.paused():
            try:
                b, z = self.ex.klines(c["btc_symbol"], "60m", 3), self.ex.klines(self.sym, "60m", 3)
                now_ms = self.clock() * 1000
                bd, zd = [r for r in b if r[2] <= now_ms], [r for r in z if r[2] <= now_ms]
                if len(bd) >= 2 and len(zd) >= 2 and len(b[-1]) > 3 and len(z[-1]) > 3:
                    # from the open of the second-to-last closed hour to the latest price (2 to 3 hours)
                    br, zr = b[-1][1] / bd[-2][3] - 1, z[-1][1] / zd[-2][3] - 1
                    if br >= c["btc_guard"] and zr < c["btc_zig_max"]:
                        self.set_meta(pause_until=self.clock() + c["btc_pause_h"] * 3600)
                        n = self.pull_sells()
                        self.note("btc guard", m["ladders"] - 1, 0, mid or 0, 0, 0,
                                  f"BTC {100 * br:+.1f}%, ZIG {100 * zr:+.1f}%: {n} sells pulled for {c['btc_pause_h']} h")
            except Exception as e:
                self.log(f"mm bee: BTC guard skipped: {e}")
        if c["vol_fast"]:
            try:
                k4, kd = self.ex.klines(self.sym, "4h", 2), self.ex.klines(self.sym, "1d", 1)
                if k4 and kd and len(k4[-1]) > 3:
                    mv = lambda r: abs(r[1] / r[3] - 1) if r[3] else 0.0           # noqa: E731
                    run4, rund = mv(k4[-1]), mv(kd[-1])
                    last4 = mv(k4[-2]) if len(k4) > 1 else 0.0
                    if not m["fast"] and max(run4, rund, last4) > c["vol_fast"]:
                        self.set_meta(fast=1)
                        self.note("fast mode", m["ladders"] - 1, 0, mid or 0, 0, 0, f"ZIG moved {100 * max(run4, rund, last4):.1f}%: base follows every 1h close")
                    elif m["fast"] and run4 < c["vol_calm"] and rund < c["vol_calm"]:
                        self.set_meta(fast=0)
                        self.note("calm", m["ladders"] - 1, 0, mid or 0, 0, 0, "moves back under 5%: base follows the 4h close again")
            except Exception as e:
                self.log(f"mm bee: volatility check skipped: {e}")
        self.con.commit()

    def pull_sells(self):
        n = 0
        for ladder, k, st, kind, sp, bp, scid, bcid, q, cyc in self.units("selling"):
            try:
                self.cancel(scid)
            except Exception:
                pass
            s, filled = self.status(scid)
            if s == "FILLED" or filled > 0 or s not in DONE:
                continue
            self.upd(ladder, k, state="armed" if kind == "ladder" else "bought", sell_cid=None)
            n += 1
        self.con.commit()
        return n

    def place_armed(self, mid):
        if self.paused():
            return
        for ladder, k, st, kind, sp, bp, scid, bcid, q, cyc in self.units("armed"):
            if sp <= mid:
                continue
            cid, px, qq = self.cid("S"), self.ex.px(self.sym, sp, up=True), self.ex.qty(self.sym, q)
            try:
                self.send(ladder, k, "SELL", "selling", {"sell_cid": cid, "sell_px": float(px)},
                          lambda: self.ex.limit_maker(self.sym, "SELL", qq, px, cid), "armed")
            except Exception as e:
                self.log(f"mm bee: sell {ladder}/{k} not placed: {e}")
        self.con.commit()
