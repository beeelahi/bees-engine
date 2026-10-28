"""
MM-Bee backtest (Python 3.9 stdlib only): "as if MM-Bee had started on START" on real ZIG candles.

It runs the exact strategy code (mm_bee.BeeLadder) against a candle-driven simulated exchange:
  * the bot acts at every candle's close (the close is the market price it sees);
  * an order placed at a close can only fill from the NEXT candle on, and only when that candle trades THROUGH its
    price (a sell needs high > price, a buy low < price; touching the price is not enough). So within one candle a
    unit can never sell and buy back: conservative, the result is a floor rather than a best case;
  * post-only orders that would cross the close are rejected, as on MEXC; a plain LIMIT at a price the market already
    passed fills at once at that price;
  * maker fee 0% (MEXC spot), no slippage (every order is a resting limit at its own price);
  * the base is the previous UTC day's close; "hourly close" for the 12-hour revision is the last completed 1h close.
Wallet as on paper: 600,000 ZIG (two ladders of 300,000) + 10,000 USDT.

    python3 bin/mm_bee_backtest.py                 last run's summary from the database candles
Called by the engine (binance_sync) once per new hourly candle; writes data/mm_bee_backtest.json for the MM-Bee tab.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mm_bee  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data", "mm_bee_backtest.json")
START = "2026-10-13T00:00:00Z"        # "created 15 days ago" (Bilal, 28 Oct 2026)
SYMBOL = "ZIGUSDT"
H = 3600


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def t_of(s):
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


class CandleEx:
    """Exchange stand-in driven by candles [(open_s, o, h, l, c)], bar length `bar` seconds."""

    def __init__(self, candles, bar, coins, usdt):
        self.c, self.bar, self.i = candles, bar, 0
        self.w = {"ZIG": [float(coins), 0.0], "USDT": [float(usdt), 0.0]}
        self.o, self.fills = {}, []

    # clock and market
    def now(self):
        return self.c[self.i][0] + self.bar                    # the bot acts at this candle's close

    def mid(self):
        return self.c[self.i][4]

    def klines(self, symbol, interval, limit=3):
        step = 86400 if interval == "1d" else H
        t = self.now()
        cur = t - t % step                                     # the open period at the bot's clock
        last = None
        for r in self.c[:self.i + 1]:                          # close of the last candle inside the last full period
            if cur - step <= r[0] < cur:
                last = r[4]
        if last is None:
            for r in self.c[:self.i + 1]:
                if r[0] < cur:
                    last = r[4]
        return [((cur - step) * 1000, last, cur * 1000)] if last is not None else []

    # exchange interface used by BeeLadder
    def info(self, s):
        return {"base": "ZIG", "quote": "USDT"}

    def qty(self, s, q):
        from decimal import Decimal, ROUND_DOWN
        return (Decimal(str(q)) / Decimal("0.01")).to_integral_value(rounding=ROUND_DOWN) * Decimal("0.01")

    def px(self, s, p, up=False):
        from decimal import Decimal, ROUND_DOWN, ROUND_UP
        return (Decimal(str(p)) / Decimal("0.000001")).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * Decimal("0.000001")

    def balances(self):
        return {a: (f, l) for a, (f, l) in self.w.items()}

    def _lock(self, side, q, p):
        a, amt = ("ZIG", q) if side == "SELL" else ("USDT", q * p)
        if self.w[a][0] + 1e-9 < amt:
            raise RuntimeError("insufficient balance")
        self.w[a][0] -= amt
        self.w[a][1] += amt

    def limit_maker(self, s, side, q, p, cid):
        q, p, m = float(q), float(p), self.mid()
        if (side == "SELL" and p <= m) or (side == "BUY" and p >= m):
            raise RuntimeError("post-only would cross")
        self._lock(side, q, p)
        self.o[cid] = {"side": side, "price": p, "qty": q, "status": "NEW", "filled": 0.0, "placed": self.i}

    def limit(self, s, side, q, p, cid):
        q, p = float(q), float(p)
        self._lock(side, q, p)
        self.o[cid] = {"side": side, "price": p, "qty": q, "status": "NEW", "filled": 0.0, "placed": self.i}
        m = self.mid()
        if (side == "BUY" and m <= p) or (side == "SELL" and m >= p):
            self._fill(cid, "taker")

    def cancel(self, s, cid):
        o = self.o.get(cid)
        if o and o["status"] == "NEW":
            o["status"] = "CANCELED"
            a, amt = ("ZIG", o["qty"]) if o["side"] == "SELL" else ("USDT", o["qty"] * o["price"])
            self.w[a][0] += amt
            self.w[a][1] -= amt

    def order(self, s, cid):
        o = self.o[cid]
        return {"status": o["status"], "executedQty": str(o["filled"]), "price": str(o["price"])}

    def _fill(self, cid, liq):
        o = self.o[cid]
        q, p = o["qty"], o["price"]
        o.update(status="FILLED", filled=q)
        if o["side"] == "SELL":
            self.w["ZIG"][1] -= q
            self.w["USDT"][0] += q * p
        else:
            self.w["USDT"][1] -= q * p
            self.w["ZIG"][0] += q
        self.fills.append([iso(self.now()), o["side"], p, q, liq])           # stamped at the close of the candle it filled in

    def advance(self):
        """Move to the next candle and fill what it trades through."""
        self.i += 1
        _, o_, h, l, c = self.c[self.i]
        for cid, o in list(self.o.items()):
            if o["status"] != "NEW" or o["placed"] >= self.i:
                continue
            if (o["side"] == "SELL" and h > o["price"]) or (o["side"] == "BUY" and l < o["price"]):
                self._fill(cid, "maker")


def run(candles, start=START, bar=H, settings=None, coins=None, usdt=None):
    """Backtest from `start` (ISO, UTC) over candles [(open_s, o, h, l, c)] sorted by time. Needs the day before."""
    c = dict(mm_bee.DEFAULTS, **(settings or {}), symbol=SYMBOL)
    coins = c["units"] * c["size"] * (1 + c["extra_ladders"]) if coins is None else coins
    usdt = c["max_usdt"] if usdt is None else usdt
    t0 = t_of(start)
    first = next((i for i, r in enumerate(candles) if r[0] >= t0), None)
    if first is None or first == 0 or candles[0][0] > t0 - 86400:
        raise RuntimeError("mm bee backtest: need candles from the day before the start")
    ex = CandleEx(candles, bar, coins, usdt)
    ex.i = first - 1                                    # the bot starts at the close just before START
    con = sqlite3.connect(":memory:")
    bee = mm_bee.BeeLadder(ex, c, con, log=lambda *a: None, clock=ex.now)
    p0 = ex.mid()
    equity, steps = [], 0
    while True:
        bee.step(ex.mid())
        steps += 1
        t, m = ex.now(), ex.mid()
        if t % H == 0:
            zig, us = sum(ex.w["ZIG"]), sum(ex.w["USDT"])
            equity.append([iso(t), round(m, 8), round(zig * m + us, 2), round(coins * m + usdt, 2)])
        if ex.i + 1 >= len(candles):
            break
        ex.advance()
    meta = bee.meta()
    zig, us, m = sum(ex.w["ZIG"]), sum(ex.w["USDT"]), ex.mid()
    value, hold = zig * m + us, coins * m + usdt
    units = [list(u) for u in con.execute("SELECT ladder, k, state, kind, sell_px, buy_px, qty, cycles, updated_utc FROM bee_units ORDER BY ladder, k")]
    events = [list(e) for e in con.execute("SELECT utc, event, ladder, k, price, qty, profit, note FROM bee_log ORDER BY rowid DESC")]
    days = {}
    for e in events:
        d = days.setdefault(e[0][:10], {"date": e[0][:10], "sold": 0, "bought": 0, "profit_usdt": 0.0})
        if e[1] == "sold":
            d["sold"] += 1
        elif e[1] == "bought":
            d["bought"] += 1
            d["profit_usdt"] += e[6]
    hi = max(r[2] for r in candles[first:]) if first < len(candles) else p0
    lo = min(r[3] for r in candles[first:]) if first < len(candles) else p0
    con.close()
    peak, dd = None, 0.0
    for x in equity:
        edge = x[2] - x[3]
        peak = edge if peak is None or edge > peak else peak
        dd = min(dd, edge - peak)
    return {"start_utc": start, "end_utc": iso(ex.now()), "bar_seconds": bar, "candles": len(candles) - first + 1,
            "settings": {k: c[k] for k in mm_bee.DEFAULTS if k != "mode"}, "start_coins": coins, "start_usdt": usdt,
            "start_price": p0, "end_price": m, "high": hi, "low": lo,
            "rounds": meta["rounds"], "profit_usdt": round(meta["profit_usdt"], 2), "ladders": meta["ladders"],
            "base": meta["base"], "base_from": meta["base_from"], "proceeds_usdt": round(meta["proceeds_usdt"], 2),
            "spent_usdt": round(meta["spent_usdt"], 2), "coins": round(zig, 2), "usdt": round(us, 2),
            "value_usdt": round(value, 2), "hold_usdt": round(hold, 2), "vs_hold_usdt": round(value - hold, 2),
            "worst_dip_vs_hold_usdt": round(dd, 2), "fills": len(ex.fills),
            "sells": sum(1 for f in ex.fills if f[1] == "SELL"), "buys": sum(1 for f in ex.fills if f[1] == "BUY"),
            "taker_fills": sum(1 for f in ex.fills if f[4] == "taker"),
            "equity": equity, "fill_list": ex.fills[::-1], "events": events, "units": units,
            "days": sorted(days.values(), key=lambda d: d["date"]),
            "method": "1-hour MEXC candles; orders fill only on a later candle that trades through their price; maker fee 0%."}


def candles_from_db(con, symbol=SYMBOL, since_s=None):
    q = "SELECT open_ms/1000, open, high, low, close FROM candles_1h WHERE symbol=?" + (" AND open_ms>=?" if since_s else "") + " ORDER BY open_ms"
    return [tuple(r) for r in con.execute(q, (symbol, since_s * 1000) if since_s else (symbol,))]


def refresh(con, out=OUT, log=print):
    """Re-run when a new hourly candle exists. Returns True when the file was rewritten."""
    since = t_of(START) - 2 * 86400
    rows = candles_from_db(con, SYMBOL, since)
    now_s = datetime.now(timezone.utc).timestamp()
    rows = [r for r in rows if r[0] + H <= now_s]          # completed candles only
    if not rows:
        return False
    last = iso(rows[-1][0])
    try:
        with open(out) as f:
            if json.load(f).get("last_candle_utc") == last:
                return False
    except (OSError, ValueError):
        pass
    res = run(rows)
    res["last_candle_utc"] = last
    res["generated_utc"] = iso(now_s)
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(res, f, separators=(",", ":"))
    os.replace(tmp, out)
    log(f"mm bee backtest: since {START}, {res['rounds']} rounds, {res['profit_usdt']:+,.2f} USDT locked in, "
        f"{res['vs_hold_usdt']:+,.2f} vs holding")
    return True


if __name__ == "__main__":
    db = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'binance.db')}?mode=ro", uri=True)
    r = run([x for x in candles_from_db(db, SYMBOL, t_of(START) - 2 * 86400)])
    print(json.dumps({k: v for k, v in r.items() if not isinstance(v, list)}, indent=1))
