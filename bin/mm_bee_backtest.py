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
import bisect
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
# every scenario shown on the MM-Bee tab: (id, start, label). The bot goes live at a daily close (00:00 UTC) and takes
# that day's close as its base, exactly as the live code does.
SCENARIOS = [("5d", "2026-10-24T00:00:00Z", "Live 5 days ago (24 Oct)"),        # Bilal, 29 Oct 2026
             ("15d", START, "Started 13 Oct (15 days)"),
             ("1y", "2025-11-01T00:00:00Z", "Started 1 Nov 2025 (1 year)")]
# the strategies compared on the MM-Bee tab: (id, label, settings over mm_bee.DEFAULTS)
STRATEGIES = [("v2", "MM-Bee v2 (base follows each 4h close)", {}),
              ("plus", "MM-Bee+ (v2, bought-back units rejoin the ladder; no 1h mode)", {"recycle": "ladder", "vol_fast": 0}),
              ("v1", "MM-Bee v1 (daily base, 12 h revision)", {"rebase_hours": 0})]
SYMBOL = "ZIGUSDT"
VERSION = 4                            # bump when the strategy or the backtest changes: the next engine run reruns it
H = 3600


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def t_of(s):
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


class CandleEx:
    """Exchange stand-in driven by candles [(open_s, o, h, l, c)], bar length `bar` seconds."""

    def __init__(self, candles, bar, coins, usdt, others=None):
        self.c, self.bar, self.i = candles, bar, 0
        self.times = [r[0] for r in candles]
        self.others = {k: (v, [r[0] for r in v]) for k, v in (others or {}).items()}
        self.w = {"ZIG": [float(coins), 0.0], "USDT": [float(usdt), 0.0]}
        self.o, self.fills, self.live = {}, [], set()

    # clock and market
    def now(self):
        return self.c[self.i][0] + self.bar                    # the bot acts at this candle's close

    def mid(self):
        return self.c[self.i][4]

    def klines(self, symbol, interval, limit=3):
        """Candles as the exchange would give them at the bot's clock: [(open_ms, close, close_ms, open, high, low)],
        oldest first; the last one may still be running (close_ms after the clock). `symbol` may be another
        series given in `others` (BTCUSDT for the BTC guard)."""
        step = {"1d": 86400, "4h": 4 * H}.get(interval, H)
        t = self.now()
        rows, times = (self.c, self.times) if symbol == SYMBOL or symbol not in self.others else self.others[symbol]
        j = bisect.bisect_right(times, t - self.bar) - 1         # the last candle that has closed by the clock
        if j < 0:
            return []
        cur = t - t % step
        out = []
        for p0 in range(cur - (limit - 1) * step, cur + 1, step):
            a = bisect.bisect_left(times, p0)
            z = min(bisect.bisect_left(times, p0 + step), j + 1)
            if a >= z:
                continue
            seg = rows[a:z]
            out.append((p0 * 1000, seg[-1][4], (p0 + step) * 1000, seg[0][1], max(r[2] for r in seg), min(r[3] for r in seg)))
        return out

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
        self.live.add(cid)

    def limit(self, s, side, q, p, cid):
        q, p = float(q), float(p)
        self._lock(side, q, p)
        self.o[cid] = {"side": side, "price": p, "qty": q, "status": "NEW", "filled": 0.0, "placed": self.i}
        self.live.add(cid)
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
        for cid in list(self.live):                             # only orders still resting (not the whole history)
            o = self.o[cid]
            if o["status"] != "NEW":
                self.live.discard(cid)
                continue
            if o["placed"] >= self.i:
                continue
            if (o["side"] == "SELL" and h > o["price"]) or (o["side"] == "BUY" and l < o["price"]):
                self._fill(cid, "maker")


def run(candles, start=START, bar=H, settings=None, coins=None, usdt=None, end=None, btc=None):
    """Backtest from `start` (ISO, UTC) over candles [(open_s, o, h, l, c)] sorted by time. Needs the day before."""
    if end:
        candles = [r for r in candles if r[0] < t_of(end)]
    # orders move at the candle close in the backtest: the 150-second stagger is shorter than one candle
    c = dict(mm_bee.DEFAULTS, **dict({"stagger_delay_s": 0}, **(settings or {})), symbol=SYMBOL)
    coins = c["units"] * c["size"] * (1 + c["extra_ladders"]) if coins is None else coins
    usdt = c["max_usdt"] if usdt is None else usdt
    t0 = t_of(start)
    first = next((i for i, r in enumerate(candles) if r[0] >= t0), None)
    if first is None or first == 0 or candles[0][0] > t0 - 86400:
        raise RuntimeError("mm bee backtest: need candles from the day before the start")
    ex = CandleEx(candles, bar, coins, usdt, others={c["btc_symbol"]: btc} if btc else None)
    ex.i = first - 1                                    # the bot starts at the close just before START
    con = sqlite3.connect(":memory:")
    bee = mm_bee.BeeLadder(ex, c, con, log=lambda *a: None, clock=ex.now, sleep=lambda s: None)
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
    start_value = coins * p0 + usdt
    return {"start_utc": start, "end_utc": iso(ex.now()), "bar_seconds": bar, "candles": len(candles) - first + 1,
            "start_value_usdt": round(start_value, 2), "change_usdt": round(value - start_value, 2),
            "hold_change_usdt": round(hold - start_value, 2),
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
            "method": "1-hour MEXC candles; orders fill only on a later candle that trades through their price; maker fee 0%.",
            "guards": [e for e in events if e[1] in ("btc guard", "fast mode", "calm")][:50]}


def candles_from_db(con, symbol=SYMBOL, since_s=None):
    q = "SELECT open_ms/1000, open, high, low, close FROM candles_1h WHERE symbol=?" + (" AND open_ms>=?" if since_s else "") + " ORDER BY open_ms"
    return [tuple(r) for r in con.execute(q, (symbol, since_s * 1000) if since_s else (symbol,))]


def refresh(con, out=OUT, log=print):
    """Re-run when a new hourly candle exists. Returns True when the file was rewritten."""
    since = min(t_of(x[1]) for x in SCENARIOS) - 2 * 86400
    rows = candles_from_db(con, SYMBOL, since)
    now_s = datetime.now(timezone.utc).timestamp()
    rows = [r for r in rows if r[0] + H <= now_s]          # completed candles only
    if not rows:
        return False
    last = iso(rows[-1][0])
    try:
        with open(out) as f:
            old = json.load(f)
            if old.get("last_candle_utc") == last and old.get("version") == VERSION:
                return False
    except (OSError, ValueError):
        pass
    btc = [r for r in candles_from_db(con, "BTCUSDT", since) if r[0] + H <= now_s] or None
    runs = []
    for gid, glabel, gset in STRATEGIES:
        for sid, start, label in SCENARIOS:
            try:
                r = run(rows, start=start, settings=gset, btc=btc)
            except RuntimeError:                           # not enough history for this window
                continue
            r.update(id=sid, label=label, strategy=gid, strategy_label=glabel)
            if sid == "1y":                                # keep the page light: a year of detail is summarised
                r["equity"] = r["equity"][::12]
                r["fill_list"] = r["fill_list"][:150]
                r["events"] = r["events"][:150]
            runs.append(r)
    res = dict(runs[0], runs=runs)                         # top level = the first scenario (older pages read it)
    res["last_candle_utc"] = last
    res["version"] = VERSION
    res["generated_utc"] = iso(now_s)
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(res, f, separators=(",", ":"))
    os.replace(tmp, out)
    for r in runs:
        log(f"mm bee backtest {r['strategy']}/{r['id']}: since {r['start_utc']}, {r['rounds']} rounds, {r['profit_usdt']:+,.2f} USDT locked in, "
            f"{r['vs_hold_usdt']:+,.2f} vs holding")
    return True


if __name__ == "__main__":
    db = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'binance.db')}?mode=ro", uri=True)
    r = run([x for x in candles_from_db(db, SYMBOL, t_of(START) - 2 * 86400)])
    print(json.dumps({k: v for k, v in r.items() if not isinstance(v, list)}, indent=1))
