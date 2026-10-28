"""
Paper trading for the market-making bot (Python 3.9 stdlib only).

Runs the exact live strategy code (mm_bot.MM, which drives mm_grid_live.GridLive in grid mode) against a simulated
MEXC account that is filled from MEXC's real public trades and order book. No API key is read: the exchange client
is public-only, so no order can ever reach MEXC.

    python3 ~/BinanceData/bin/mm_paper.py            always-on service "bees-mm-paper"
    python3 ~/BinanceData/bin/mm_paper.py --report   results so far (ops command mm-paper-report)

Coins and settings come from the private ~/BinanceData/mm.json; its "enabled" flag is ignored (paper never trades).
Variants run side by side on the same market data, each with its own wallet, so the live result decides the setting:
  base  exactly as configured
  tight grid step (or quote spread) x 2/3
  wide  grid step (or quote spread) x 4/3
Fill model (conservative):
  * a resting order fills in full when a public trade prints THROUGH its price, or the opposite best price crosses it;
  * at exactly its price it waits behind the size already resting there when it was placed (queue), and only taker
    volume from the other side counts;
  * only trades printed after the order was placed count;
  * LIMIT_MAKER orders that would cross are rejected (as on MEXC); a plain LIMIT that crosses fills at once at its
    limit price, as a taker;
  * fees: maker 0%, taker 0.05% (MEXC standard spot); "paper_fees": {"maker": x, "taker": y} in mm.json overrides.
Wallet per variant: "paper_coins" / "paper_usdt" from the coin's settings if present, else start_coins and max_usdt
(grid) or half of max_coin and max_usdt (quotes). State: data/mm_paper_<variant>.db, so restarts are safe.
Pause: create ~/BinanceData/MM_PAPER_STOP (the paper bot cancels its paper orders), or stop the service.
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mexc_exchange as X  # noqa: E402
import mm_bot  # noqa: E402
import mm_grid_live  # noqa: E402
import mm_bee  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CFG = os.path.join(ROOT, "mm.json")
DATA = os.path.join(ROOT, "data")
STOP = os.path.join(ROOT, "MM_PAPER_STOP")
LOOP_S = 15
SNAP_S = 3600
FEES = {"maker": 0.0, "taker": 0.0005}

SCHEMA = """
CREATE TABLE IF NOT EXISTS p_orders(cid TEXT PRIMARY KEY, symbol TEXT, side TEXT, type TEXT, price REAL, qty REAL,
    filled REAL, status TEXT, queue REAL, placed_ms INTEGER, updated_utc TEXT);
CREATE TABLE IF NOT EXISTS p_wallet(asset TEXT PRIMARY KEY, free REAL, locked REAL);
CREATE TABLE IF NOT EXISTS p_fills(n INTEGER PRIMARY KEY AUTOINCREMENT, cid TEXT, symbol TEXT, side TEXT, price REAL,
    qty REAL, fee_usdt REAL, liquidity TEXT, utc TEXT, ms INTEGER);
CREATE TABLE IF NOT EXISTS p_meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS p_snap(utc TEXT, symbol TEXT, mid REAL, coins REAL, usdt REAL, value REAL, hold_value REAL);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Feed:
    """Public market data, fetched once per loop and shared by every variant."""

    def __init__(self, pub):
        self.pub, self.books, self.trades, self.synced = pub, {}, {}, 0.0

    def now_ms(self):
        """MEXC's clock (trade timestamps are MEXC time)."""
        return int(time.time() * 1000) + getattr(self.pub, "_offset", 0)

    def refresh(self, sym):
        if time.time() - self.synced > 600:
            self.pub.sync_time()
            self.synced = time.time()
        self.books[sym] = self.pub.book(sym, 100)
        self.trades[sym] = self.pub.trades(sym, 1000)


class PaperEx:
    """Same interface as mexc_exchange.Mexc, backed by a local wallet. Prices / sizes are rounded by MEXC's rules."""

    def __init__(self, feed, con, fees=None, clock=None):
        self.feed, self.pub, self.con = feed, feed.pub, con
        self.fees = dict(FEES, **(fees or {}))
        self.clock = clock or getattr(feed, "now_ms", None) or (lambda: int(time.time() * 1000))
        con.executescript(SCHEMA)

    # ---------------------------------------------------------------- wallet
    def meta(self, k, default=None):
        r = self.con.execute("SELECT v FROM p_meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, k, v):
        self.con.execute("INSERT OR REPLACE INTO p_meta VALUES(?,?)", (k, json.dumps(v)))

    def fund(self, asset, amount):
        """Initial deposit (only when the wallet has never been funded for this asset)."""
        if not self.con.execute("SELECT 1 FROM p_wallet WHERE asset=?", (asset,)).fetchone():
            self.con.execute("INSERT INTO p_wallet VALUES(?,?,0)", (asset, float(amount)))
            self.con.commit()

    def _bal(self, asset):
        r = self.con.execute("SELECT free, locked FROM p_wallet WHERE asset=?", (asset,)).fetchone()
        return (r[0], r[1]) if r else (0.0, 0.0)

    def _add(self, asset, free=0.0, locked=0.0):
        f, l = self._bal(asset)
        self.con.execute("INSERT OR REPLACE INTO p_wallet VALUES(?,?,?)", (asset, f + free, max(0.0, l + locked)))

    # ---------------------------------------------------------------- public (delegated)
    def sync_time(self):
        pass

    def info(self, symbol):
        return self.pub.info(symbol)

    def qty(self, symbol, q):
        return self.pub.qty(symbol, q)

    def klines(self, symbol, interval, limit=3):
        return self.pub.klines(symbol, interval, limit)

    def px(self, symbol, p, up=False):
        return self.pub.px(symbol, p, up)

    def book(self, symbol, limit=20):
        b, a = self.feed.books[symbol]
        return b[:limit], a[:limit]

    # ---------------------------------------------------------------- account
    def account(self):
        return {"canTrade": True, "canWithdraw": False}

    def balances(self):
        return {a: (f, l) for a, f, l in self.con.execute("SELECT asset, free, locked FROM p_wallet")}

    def _view(self, r):
        cid, sym, side, typ, price, qty, filled, status = r
        return {"clientOrderId": cid, "symbol": sym, "side": side, "type": typ, "price": f"{price:.10g}",
                "origQty": f"{qty:.10g}", "executedQty": f"{filled:.10g}", "status": status}

    def open_orders(self, symbol):
        return [self._view(r) for r in self.con.execute(
            "SELECT cid, symbol, side, type, price, qty, filled, status FROM p_orders WHERE symbol=? AND status IN ('NEW','PARTIALLY_FILLED')", (symbol,))]

    def order(self, symbol, client_id):
        r = self.con.execute("SELECT cid, symbol, side, type, price, qty, filled, status FROM p_orders WHERE symbol=? AND cid=?",
                             (symbol, client_id)).fetchone()
        if not r:
            raise X.MexcError("MEXC error -2013: Order does not exist.")
        return self._view(r)

    def my_trades(self, symbol, start_ms):
        return [{"id": n, "isBuyer": side == "BUY", "price": str(p), "qty": str(q), "quoteQty": str(p * q)} for n, side, p, q in
                self.con.execute("SELECT n, side, price, qty FROM p_fills WHERE symbol=? AND ms>=? ORDER BY n", (symbol, start_ms))]

    # ---------------------------------------------------------------- orders
    def _check(self, symbol, side, qty, price):
        inf = self.info(symbol)
        if qty <= 0 or price <= 0:
            raise X.MexcError("MEXC error 400: quantity / price must be positive")
        if qty * price < float(inf["min_quote"]):
            raise X.MexcError("MEXC error 30002: order value below the minimum")
        asset, need = (inf["quote"], qty * price) if side == "BUY" else (inf["base"], qty)
        if self._bal(asset)[0] + 1e-12 < need:
            raise X.MexcError("MEXC error 30004: insufficient balance")
        return inf

    def _crosses(self, symbol, side, price):
        b, a = self.feed.books[symbol]
        return (side == "BUY" and a and price >= a[0][0]) or (side == "SELL" and b and price <= b[0][0])

    def _queue(self, symbol, side, price):
        b, a = self.feed.books[symbol]
        lvl = b if side == "BUY" else a
        return sum(q for p, q in lvl if abs(p - price) <= price * 1e-9)

    def _rest(self, symbol, side, typ, qty, price, cid, inf):
        if self.con.execute("SELECT 1 FROM p_orders WHERE cid=?", (cid,)).fetchone():
            raise X.MexcError("MEXC error 30016: duplicate client order id")
        if side == "BUY":
            self._add(inf["quote"], free=-qty * price, locked=qty * price)
        else:
            self._add(inf["base"], free=-qty, locked=qty)
        self.con.execute("INSERT INTO p_orders VALUES(?,?,?,?,?,?,0,'NEW',?,?,?)",
                         (cid, symbol, side, typ, price, qty, self._queue(symbol, side, price), self.clock(), now()))
        self.con.commit()
        return {"orderId": cid, "clientOrderId": cid}

    def limit_maker(self, symbol, side, qty, price, client_id):
        assert client_id.startswith("bm")
        qty, price = float(qty), float(price)
        inf = self._check(symbol, side, qty, price)
        if self._crosses(symbol, side, price):
            raise X.MexcError("MEXC error 30087: post-only order would take liquidity")
        return self._rest(symbol, side, "LIMIT_MAKER", qty, price, client_id, inf)

    def limit(self, symbol, side, qty, price, client_id):
        assert client_id.startswith("bm")
        qty, price = float(qty), float(price)
        inf = self._check(symbol, side, qty, price)
        out = self._rest(symbol, side, "LIMIT", qty, price, client_id, inf)
        if self._crosses(symbol, side, price):               # takes liquidity: fills now, at its limit price (conservative)
            self._fill(client_id, qty, "taker")
        return out

    def cancel(self, symbol, client_id):
        r = self.con.execute("SELECT side, price, qty, filled, status FROM p_orders WHERE symbol=? AND cid=?", (symbol, client_id)).fetchone()
        if not r or r[4] not in ("NEW", "PARTIALLY_FILLED"):
            raise X.MexcError("MEXC error -2011: order is not open")
        side, price, qty, filled, _ = r
        inf, rest = self.info(symbol), qty - filled
        if side == "BUY":
            self._add(inf["quote"], free=rest * price, locked=-rest * price)
        else:
            self._add(inf["base"], free=rest, locked=-rest)
        self.con.execute("UPDATE p_orders SET status='CANCELED', updated_utc=? WHERE cid=?", (now(), client_id))
        self.con.commit()
        return {"clientOrderId": client_id}

    def _fill(self, cid, q, liquidity):
        sym, side, price, qty, filled = self.con.execute("SELECT symbol, side, price, qty, filled FROM p_orders WHERE cid=?", (cid,)).fetchone()
        q = min(q, qty - filled)
        if q <= 0:
            return
        inf = self.info(sym)
        fee = q * price * self.fees[liquidity]
        if side == "BUY":
            self._add(inf["quote"], free=-fee, locked=-q * price)
            self._add(inf["base"], free=q)
        else:
            self._add(inf["base"], locked=-q)
            self._add(inf["quote"], free=q * price - fee)
        done = filled + q >= qty - 1e-12
        self.con.execute("UPDATE p_orders SET filled=?, status=?, updated_utc=? WHERE cid=?",
                         (qty if done else filled + q, "FILLED" if done else "PARTIALLY_FILLED", now(), cid))
        self.con.execute("INSERT INTO p_fills(cid, symbol, side, price, qty, fee_usdt, liquidity, utc, ms) VALUES(?,?,?,?,?,?,?,?,?)",
                         (cid, sym, side, price, q, fee, liquidity, now(), self.clock()))

    # ---------------------------------------------------------------- matching
    def tick(self, symbol):
        """Match resting paper orders against the public trades printed since the last tick, then the current book."""
        last = self.meta("last_ms:" + symbol, 0)
        seen = set(map(tuple, self.meta("seen:" + symbol, [])))
        new = [t for t in self.feed.trades.get(symbol, []) if t[0] > last or (t[0] == last and tuple(t) not in seen)]
        orders = self.con.execute("SELECT cid, side, price, qty, filled, queue, placed_ms FROM p_orders WHERE symbol=? AND status IN ('NEW','PARTIALLY_FILLED')",
                                  (symbol,)).fetchall()
        state = {o[0]: [o[1], o[2], o[3] - o[4], o[5], o[6]] for o in orders}
        for ms, p, q, taker in new:
            for cid, st in state.items():
                side, price, rest, queue, placed = st
                if rest <= 0 or ms < placed:
                    continue
                through = (side == "SELL" and p > price) or (side == "BUY" and p < price)
                if through:
                    self._fill(cid, rest, "maker")
                    st[2] = 0
                elif abs(p - price) <= price * 1e-9 and taker != side:     # a taker hit our price level
                    left = q - queue
                    st[3] = max(0.0, queue - q)
                    if left > 0:
                        take = min(rest, left)
                        self._fill(cid, take, "maker")
                        st[2] = rest - take
        for cid, st in state.items():
            self.con.execute("UPDATE p_orders SET queue=? WHERE cid=?", (st[3], cid))
        b, a = self.feed.books.get(symbol, ([], []))
        for cid, (side, price, rest, queue, placed) in state.items():                  # the book moved through us
            if rest > 0 and ((side == "SELL" and b and b[0][0] > price) or (side == "BUY" and a and a[0][0] < price)):
                self._fill(cid, rest, "maker")
        if new:
            top = max(t[0] for t in new)
            self.set_meta("last_ms:" + symbol, top)
            self.set_meta("seen:" + symbol, [list(t) for t in new if t[0] == top])
        self.con.commit()
        return len(new)


# -------------------------------------------------------------------- variants and the loop
def variants(coin):
    """base / tight / wide versions of one coin's settings."""
    out = []
    for name, f in (("base", 1.0), ("tight", 2 / 3), ("wide", 4 / 3)):
        c = dict(coin)
        if c.get("mode") == "grid":
            c["step"] = round(coin["step"] * f, 5)
        else:
            c["spread_bps"] = round(coin["spread_bps"] * f, 1)
        out.append((name, c))
    return out


BEE_VARIANTS = [("bee", {}), ("bee_plus", {"recycle": "ladder", "vol_fast": 0})]


def bee_coin(c, cfg, extra=None):
    """The MM-Bee settings for one coin: mm_bee.DEFAULTS, then mm.json's "bee" block (top level, or per coin).
    Paper wallet: enough coins for every ladder it may open, and max_usdt of USDT. "bee": false turns it off."""
    over = c.get("bee", cfg.get("bee", {}))
    if over is False:
        return None
    b = dict(mm_bee.DEFAULTS, **(over or {}), **(extra or {}), symbol=c["symbol"])
    b.setdefault("paper_coins", b["units"] * b["size"] * (1 + b["extra_ladders"]))
    b.setdefault("paper_usdt", b["max_usdt"])
    return b


def grid_ok(c):
    need = ("symbol", "step", "levels", "size", "floor", "start_coins", "band", "max_usdt") if c.get("mode") == "grid" else \
        ("symbol", "spread_bps", "levels", "level_step_bps", "order_usdt", "target_coin_pct", "max_skew_bps", "band", "max_coin", "min_coin", "max_usdt", "max_daily_usdt")
    return [k for k in need if k not in c]


class Variant:
    def __init__(self, name, coin, feed, fees, log=print):
        self.name, self.coin, self.log = name, coin, log
        os.makedirs(DATA, exist_ok=True)
        self.con = sqlite3.connect(os.path.join(DATA, f"mm_paper_{name}.db"))
        self.ex = PaperEx(feed, self.con, fees)
        inf = self.ex.info(coin["symbol"])
        grid = coin.get("mode") == "grid"
        self.start_coins = float(coin["paper_coins"] if "paper_coins" in coin else coin["start_coins"] if grid else coin["max_coin"] / 2)
        self.start_usdt = float(coin.get("paper_usdt", coin["max_usdt"]))
        self.ex.fund(inf["base"], self.start_coins)
        self.ex.fund(inf["quote"], self.start_usdt)
        self.mm = mm_bot.MM(self.ex, {"coins": [coin]}, self.con, lambda m: log(f"[{name}] {m}"))
        self.base, self.quote = inf["base"], inf["quote"]

    def mid(self):
        b, a = self.ex.book(self.coin["symbol"], 1)
        return (b[0][0] + a[0][0]) / 2 if b and a else None

    def step(self):
        sym = self.coin["symbol"]
        self.ex.tick(sym)
        mid = self.mid()
        if mid and self.ex.meta("start") is None:
            self.ex.set_meta("start", {"utc": now(), "mid": mid, "coins": self.start_coins, "usdt": self.start_usdt,
                                       "settings": {k: self.coin.get(k) for k in ("mode", "step", "spread_bps", "levels", "size", "floor", "band", "buy_rungs",
                                                                               "units", "first", "gap", "gap_from", "rebuy", "resell", "idle_hours", "max_usdt", "extra_ladders",
                                                                               "rebase_hours", "stagger_first", "stagger_delay_s", "min_move", "recycle", "acc_units",
                                                                               "btc_guard", "btc_pause_h", "vol_fast", "vol_calm", "drip_min", "drip_max")
                                                    if self.coin.get(k) is not None}})
        if mid:
            self.ex.set_meta("last_mid", mid)
        self.mm.step()
        self.ex.tick(sym)                    # orders that crossed at placement are matched right away
        for e in self.mm.digest:
            self.log(f"[{self.name}] {e}")
        self.mm.digest = []
        if mid and time.time() - (self.ex.meta("snap_t") or 0) >= SNAP_S:
            bal = self.ex.balances()
            coins, usdt = sum(bal.get(self.base, (0, 0))), sum(bal.get(self.quote, (0, 0)))
            self.con.execute("INSERT INTO p_snap VALUES(?,?,?,?,?,?,?)", (now(), sym, mid, coins, usdt, coins * mid + usdt,
                                                                       self.start_coins * mid + self.start_usdt))
            self.ex.set_meta("snap_t", time.time())
        self.con.commit()


def load_coins(path=CFG):
    with open(path) as f:
        cfg = json.load(f)
    coins = [c for c in cfg.get("coins", []) if c.get("symbol")]
    return cfg, coins


def report(path=CFG, data=DATA):
    """Plain-text results for every variant (read-only)."""
    lines = [f"== paper market making, {now()} =="]
    import glob
    for db in sorted(glob.glob(os.path.join(data, "mm_paper_*.db")), key=lambda x: ("base" not in x, "tight" not in x, x)):
        name = os.path.basename(db)[9:-3]
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            meta = {k: json.loads(v) for k, v in con.execute("SELECT k, v FROM p_meta")}
            st = meta.get("start")
            if not st:
                continue
            snap = con.execute("SELECT mid FROM p_snap ORDER BY utc DESC LIMIT 1").fetchone()
            bal = {a: f + l for a, f, l in con.execute("SELECT asset, free, locked FROM p_wallet")}
            usdt = bal.pop("USDT", 0.0)
            coins = sum(bal.values())
            fills = con.execute("SELECT COUNT(*), COALESCE(SUM(fee_usdt),0), COALESCE(SUM(liquidity='taker'),0), COALESCE(SUM(price*qty),0) FROM p_fills").fetchone()
            g = con.execute("SELECT COALESCE(SUM(rounds),0), COALESCE(SUM(profit_usdt),0) FROM grid_meta").fetchone() \
                if con.execute("SELECT 1 FROM sqlite_master WHERE name='grid_meta'").fetchone() else (0, 0)
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='bee_meta'").fetchone():
                bg = con.execute("SELECT COALESCE(SUM(rounds),0), COALESCE(SUM(profit_usdt),0) FROM bee_meta").fetchone()
                g = (g[0] + bg[0], g[1] + bg[1])
            opn = con.execute("SELECT COUNT(*) FROM p_orders WHERE status IN ('NEW','PARTIALLY_FILLED')").fetchone()[0]
            low = con.execute("SELECT MIN(coins) FROM p_snap").fetchone()[0]
        finally:
            con.close()
        mid = meta.get("last_mid") or (snap[0] if snap else st["mid"])
        val, hold = coins * mid + usdt, st["coins"] * mid + st["usdt"]
        start_val = st["coins"] * st["mid"] + st["usdt"]
        hrs = (datetime.now(timezone.utc) - datetime.strptime(st["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 3600
        s = st["settings"]
        lines += [f"-- {name}: step {s.get('step')} levels {s.get('levels')} size {s.get('size')} rungs {s.get('buy_rungs')} | running {hrs:.1f} h since {st['utc']}",
                  f"   price {st['mid']:.6g} -> {mid:.6g} ({100 * (mid / st['mid'] - 1):+.2f}%)",
                  f"   value {val:,.2f} USDT vs just holding {hold:,.2f}: {val - hold:+,.2f} ({100 * (val / hold - 1) if hold else 0:+.2f}%) | start {start_val:,.2f}",
                  f"   completed rounds {g[0]}, grid profit {g[1]:+,.2f} USDT | fills {fills[0]} ({fills[2]} taker), traded {fills[3]:,.0f} USDT, fees {fills[1]:,.2f}",
                  f"   holdings now {coins:,.2f} coins + {usdt:,.2f} USDT (lowest coins at an hourly check: {low if low is not None else '-'}) | open orders {opn}"]
    if len(lines) == 1:
        lines.append("no paper results yet (service not started, or mm.json has no complete coin settings)")
    return "\n".join(lines)


def data_dump(data=DATA):
    """Everything needed to chart and audit the paper results, as JSON (read-only): per variant the start, the
    latest price, hourly snapshots, every fill, the resting orders, the grid ladder and its counters."""
    import glob
    out = {"utc": now(), "variants": {}}
    for db in sorted(glob.glob(os.path.join(data, "mm_paper_*.db"))):
        name = os.path.basename(db)[9:-3]
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            q = lambda sql: [list(r) for r in con.execute(sql)]          # noqa: E731
            has = lambda t: con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone() is not None  # noqa: E731
            meta = {k: json.loads(v) for k, v in con.execute("SELECT k, v FROM p_meta") if not k.startswith("seen:")}
            out["variants"][name] = {
                "meta": meta,
                "wallet": q("SELECT asset, free, locked FROM p_wallet"),
                "snaps": q("SELECT utc, mid, coins, usdt, value, hold_value FROM p_snap ORDER BY utc"),
                "fills": q("SELECT utc, side, price, qty, fee_usdt, liquidity, cid FROM p_fills ORDER BY n"),
                "open": q("SELECT cid, side, type, price, qty, filled, queue, placed_ms FROM p_orders WHERE status IN ('NEW','PARTIALLY_FILLED') ORDER BY price"),
                "grid": q("SELECT level, state, sell_px, rebuy_px, qty FROM grid ORDER BY level") if has("grid") else [],
                "rungs": q("SELECT level, state, buy_px, resell_px, qty FROM grid_buy ORDER BY level") if has("grid_buy") else [],
                "grid_meta": q("SELECT anchor, rounds, profit_usdt FROM grid_meta") if has("grid_meta") else [],
                "log": q("SELECT utc, event, level, price, qty, profit FROM grid_log ORDER BY utc DESC LIMIT 200") if has("grid_log") else [],
                "bee_units": q("SELECT ladder, k, state, kind, sell_px, buy_px, qty, cycles, updated_utc FROM bee_units ORDER BY ladder, k") if has("bee_units") else [],
                "bee_meta": q("SELECT base, base_utc, base_from, ladders, rounds, profit_usdt, proceeds_usdt, spent_usdt, last_fill_utc, active_utc FROM bee_meta") if has("bee_meta") else [],
                "bee_log": q("SELECT utc, event, ladder, k, price, qty, profit, note FROM bee_log ORDER BY rowid DESC LIMIT 300") if has("bee_log") else []}
        finally:
            con.close()
    return out


def main():
    if "--json" in sys.argv:
        print(json.dumps(data_dump(), separators=(",", ":")))
        return 0
    if "--report" in sys.argv:
        print(report())
        return 0
    mm_bot.STOP = mm_grid_live.STOP = mm_bee.STOP = STOP          # paper has its own pause switch; the live MM_STOP is not touched
    try:
        cfg, coins = load_coins()
    except (OSError, ValueError) as e:
        print(f"mm_paper: cannot read mm.json ({e})")
        return 0
    good = []
    for c in coins:
        miss = grid_ok(c)
        if miss:
            print(f"mm_paper: {c.get('symbol')} skipped, settings missing: {', '.join(miss)}")
        else:
            good.append(c)
    if not good:
        print("mm_paper: no coin with complete settings in mm.json; nothing to do")
        return 0
    feed = Feed(X.Mexc(cred=False))
    runs = []
    for c in good:
        for name, cv in variants(c):
            vname = name if len(good) == 1 else f"{name}_{c['symbol'].lower()}"
            runs.append(Variant(vname, cv, feed, cfg.get("paper_fees")))
    for c in good:                       # MM-Bee: Bilal's sell-first ladder (v2), and MM-Bee+ (recycling), on paper
        for vn, extra in BEE_VARIANTS:
            bc = bee_coin(c, cfg, extra)
            if bc:
                runs.append(Variant(vn if len(good) == 1 else f"{vn}_{c['symbol'].lower()}", bc, feed, cfg.get("paper_fees")))
    print(f"mm_paper: started, {len(runs)} variant(s), loop {LOOP_S}s, no API key in use")
    while True:
        try:
            for c in good:
                feed.refresh(c["symbol"])
        except Exception as e:
            print(f"mm_paper: market data error: {str(e)[:160]}")
            time.sleep(30)
            continue
        for v in runs:
            try:
                v.step()
            except Exception as e:
                print(f"mm_paper: [{v.name}] {str(e)[:200]}")
        time.sleep(LOOP_S)


if __name__ == "__main__":
    sys.exit(main())
