"""
Market-making bot (MEXC spot), Python 3.9 stdlib only. Runs as its own always-on process on the server:
    python3 ~/BinanceData/bin/mm_bot.py            (systemd service "bees-mm")

For each coin in ~/BinanceData/mm.json it keeps post-only quotes on both sides of the mid price:
  * spread: at least `spread_bps` around the mid, wide enough to clear fees; `levels` quotes per side, each
    `level_step_bps` further out, `order_usdt` each;
  * inventory: the target share of the allocation held in the coin is `target_coin_pct`. When fills push the
    split away from it, quotes lean back (buy cheaper / sell cheaper) by up to `max_skew_bps`;
  * band: it only quotes while the mid price is inside `band` [low, high]; outside, it cancels and waits;
  * limits: never more than `max_coin` / less than `min_coin` coins, never more than `max_daily_usdt` traded per
    UTC day; quotes are LIMIT_MAKER (post-only), so it never takes liquidity and can never trade with itself.
Kill switches (files in ~/BinanceData): MM_STOP cancels this bot's quotes and pauses; the bot never market-sells.
Every fill is recorded in data/mm.db and summarised on Telegram (if set up).
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CFG = os.path.join(ROOT, "mm.json")
DB = os.path.join(ROOT, "data", "mm.db")
STOP = os.path.join(ROOT, "MM_STOP")
LOOP_S = 15
HEARTBEAT = os.path.join(ROOT, "data", "mm_heartbeat.json")   # private status file for ops (never published)
# Code-level ceilings for MM-Bee (raise only by a code change): the coins one coin's ladder may sell, valued at the
# start, and all coins together. A coin over the limit is refused at start, with a message; nothing is placed.
# Checked when a coin STARTS (a running coin is never dropped because its price rose).
MAX_COIN_USD = 15_000.0
MAX_TOTAL_USD = 120_000.0
ERR_PAUSE = 5                # consecutive errors on one coin: that coin waits ERR_BACKOFF_S, Telegram is told
ERR_BACKOFF_S = 600
REQUOTE_BPS = 15             # replace a quote when the target price moved more than this
FEE_FLOOR_BPS = 20           # never quote tighter than 2 x 0.10% (covers taker-priced fee tiers too)

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY, symbol TEXT, side TEXT, price REAL, qty REAL, quote REAL, utc TEXT);
CREATE TABLE IF NOT EXISTS events(utc TEXT, symbol TEXT, event TEXT, detail TEXT);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_quotes(c, mid, coin_qty, usdt, fees_bps=FEE_FLOOR_BPS):
    """Target quotes for one coin: list of (side, price, qty). Pure function (tested)."""
    lo, hi = c["band"]
    if not (lo <= mid <= hi):
        return []
    value_coin = coin_qty * mid
    alloc = value_coin + usdt
    if alloc <= 0:
        return []
    share = 100.0 * value_coin / alloc
    dev = (share - c["target_coin_pct"]) / max(1.0, c.get("skew_range_pct", 25.0))      # -1 .. +1 at the edges
    dev = max(-1.0, min(1.0, dev))
    half = max(c["spread_bps"], fees_bps) / 2.0
    skew = -dev * min(c["max_skew_bps"], half - 5)    # too much coin -> lower both quotes (sell easier, buy less eagerly);
    #                                                  capped so the ask stays above the mid and the bid below it
    out = []
    for k in range(c["levels"]):
        off = half + k * c["level_step_bps"]
        bid = mid * (1 + (skew - off) / 1e4)
        ask = mid * (1 + (skew + off) / 1e4)
        q_bid = c["order_usdt"] / bid
        q_ask = c["order_usdt"] / ask
        if coin_qty + q_bid * (k + 1) <= c["max_coin"] and usdt >= c["order_usdt"] * (k + 1) and bid >= lo:
            out.append(("BUY", bid, q_bid))
        if coin_qty - q_ask * (k + 1) >= c["min_coin"] and ask <= hi:
            out.append(("SELL", ask, q_ask))
    return out


class MM:
    def __init__(self, ex, cfg, con, log=print, notify=None):
        self.ex, self.cfg, self.con, self.log = ex, cfg, con, log
        self.notify = notify or (lambda t: None)
        con.executescript(SCHEMA)
        self.since = int(time.time() * 1000) - 3600 * 1000
        self.digest = []

    def traded_today(self, sym):
        return self.con.execute("SELECT COALESCE(SUM(quote),0) FROM fills WHERE symbol=? AND utc>=?", (sym, now()[:10])).fetchone()[0]

    def record_fills(self, sym):
        for t in self.ex.my_trades(sym, self.since):
            tid = f"{sym}:{t['id']}"
            if self.con.execute("SELECT 1 FROM fills WHERE id=?", (tid,)).fetchone():
                continue
            side = "BUY" if t.get("isBuyer") else "SELL"
            self.con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?)", (tid, sym, side, float(t["price"]), float(t["qty"]), float(t["quoteQty"]), now()))
            self.digest.append(f"{sym}: {side} {float(t['qty']):g} @ {float(t['price']):g}")
        self.con.commit()

    def step(self):
        if os.path.exists(STOP):
            for c in self.cfg["coins"]:
                for o in self.ex.open_orders(c["symbol"]):
                    self.ex.cancel(c["symbol"], o["clientOrderId"])
            return "stopped"
        bal = self.ex.balances()
        for c in self.cfg["coins"]:
            sym = c["symbol"]
            if c.get("mode") == "bee":                 # MM-Bee: sell-first ladder from the daily close (mm_bee.py)
                self.bee_step(c)
                continue
            if c.get("mode") == "grid":                # sell-high / rebuy-lower ladder (mm_grid_live.py)
                import mm_grid_live
                if not hasattr(self, "grids"):
                    self.grids = {}
                g = self.grids.setdefault(sym, mm_grid_live.GridLive(self.ex, c, self.con, self.log))
                bids, asks = self.ex.book(sym, 5)
                if bids and asks:
                    g.step((bids[0][0] + asks[0][0]) / 2)
                    self.digest += g.events
                    g.events = []
                continue
            inf = self.ex.info(sym)
            if not inf["trading"]:
                continue
            self.record_fills(sym)
            bids, asks = self.ex.book(sym, 5)
            if not bids or not asks:
                continue
            mid = (bids[0][0] + asks[0][0]) / 2
            coin = min(bal.get(inf["base"], (0, 0))[0] + bal.get(inf["base"], (0, 0))[1], c["max_coin"])
            usdt = min(sum(bal.get("USDT", (0, 0))), c["max_usdt"])
            want = [] if self.traded_today(sym) >= c["max_daily_usdt"] else plan_quotes(c, mid, coin, usdt)
            have = self.ex.open_orders(sym)
            # cancel quotes that are no longer wanted or drifted too far
            keep = []
            for o in have:
                p = float(o["price"])
                match = next((w for w in want if w[0] == o["side"] and abs(w[1] / p - 1) * 1e4 <= REQUOTE_BPS), None)
                if match:
                    keep.append(match)
                    want.remove(match)
                else:
                    self.ex.cancel(sym, o["clientOrderId"])
            for side, price, q in want:
                qq, pp = self.ex.qty(sym, q), self.ex.px(sym, price, up=(side == "SELL"))
                if qq <= 0 or float(qq * pp) < float(inf["min_quote"]):
                    continue
                self.seq = getattr(self, "seq", 0) + 1
                cid = f"bm{side[0]}{int(time.time()) % 10**8}{self.seq % 10**5:05d}{sym[:4]}"[:32]
                try:
                    self.ex.limit_maker(sym, side, qq, pp, cid)
                except Exception as e:             # post-only rejected (would cross) or a limit: skip this round
                    self.con.execute("INSERT INTO events VALUES(?,?,?,?)", (now(), sym, "rejected", str(e)[:160]))
        self.con.commit()
        return "ok"

    # ---------------------------------------------------------------- MM-Bee, hardened
    def bee_plan_usd(self, c, mid):
        import mm_bee
        b = dict(mm_bee.DEFAULTS, **c)
        return b["units"] * b["size"] * (1 + b["extra_ladders"]) * mid

    def bee_step(self, c):
        """One MM-Bee coin, isolated: an error on one coin never stops the others. Five errors in a row pause that
        coin for ten minutes and send an alert. Refused at start when its ladder is over the code-level ceilings."""
        import mm_bee
        sym = c["symbol"]
        if not hasattr(self, "bees"):
            self.bees, self.errs, self.wait, self.state = {}, {}, {}, {}
        if time.time() < self.wait.get(sym, 0):
            self.state[sym] = "backing off after errors"
            return
        try:
            bids, asks = self.ex.book(sym, 5)
            if not bids or not asks:
                self.state[sym] = "no order book"
                return
            mid = (bids[0][0] + asks[0][0]) / 2
            if sym not in self.bees:
                inf = self.ex.info(sym)
                if not inf.get("trading", True):
                    self.state[sym] = "not trading on MEXC"
                    return
                started = self.con.execute("SELECT 1 FROM sqlite_master WHERE name='bee_meta'").fetchone() and \
                    self.con.execute("SELECT 1 FROM bee_meta WHERE symbol=?", (sym,)).fetchone()
                usd = 0.0 if started else self.bee_plan_usd(c, mid)
                others = sum(self.bee_plan_usd(x, mid if x["symbol"] == sym else x.get("_mid", mid)) for x in self.cfg["coins"]
                             if x.get("mode") == "bee" and x["symbol"] in self.bees)
                if usd > MAX_COIN_USD or others + usd > MAX_TOTAL_USD:
                    msg = f"{sym}: ladder worth ${usd:,.0f} (all coins ${others + usd:,.0f}) is over the ceiling (${MAX_COIN_USD:,.0f} a coin, ${MAX_TOTAL_USD:,.0f} total); not started"
                    if self.state.get(sym) != "refused":
                        self.log("mm: " + msg)
                        self.notify("Market maker: " + msg)
                    self.state[sym] = "refused"
                    return
                c["_mid"] = mid
                b = mm_bee.BeeLadder(self.ex, c, self.con, self.log)
                n = b.reconcile()
                if n:
                    self.digest.append(f"{sym}: {n} unknown order(s) cancelled at start")
                self.bees[sym] = b
            b = self.bees[sym]
            self.state[sym] = b.step(mid) or "ok"
            self.digest += b.events
            alerts = [e for e in b.events if "ALERT" in e or "CIRCUIT" in e or "price check" in e]
            if alerts:
                self.notify("Market maker ALERT\n" + "\n".join(alerts[:10]))
            b.events = []
            self.errs[sym] = 0
        except Exception as e:
            self.errs[sym] = self.errs.get(sym, 0) + 1
            self.state[sym] = f"error: {str(e)[:120]}"
            self.con.execute("INSERT INTO events VALUES(?,?,?,?)", (now(), sym, "error", str(e)[:200]))
            if self.errs[sym] >= ERR_PAUSE:
                self.wait[sym] = time.time() + ERR_BACKOFF_S
                self.notify(f"Market maker: {sym} failed {self.errs[sym]} times in a row ({str(e)[:120]}); paused {ERR_BACKOFF_S // 60} min")
                self.errs[sym] = 0

    def heartbeat(self):
        """Private status for ops (mm-status): when the loop last ran and what each coin is doing."""
        d = {"utc": now(), "coins": {}}
        for sym, b in getattr(self, "bees", {}).items():
            try:
                m = b.meta() or {}
                st = {}
                for (s_,) in self.con.execute("SELECT state FROM bee_units WHERE symbol=?", (sym,)):
                    st[s_] = st.get(s_, 0) + 1
                d["coins"][sym] = {"state": self.state.get(sym), "units": st, "rounds": m.get("rounds"), "profit_usdt": m.get("profit_usdt"),
                                   "base": m.get("base"), "paused_until": m.get("pause_until") or None, "last_fill_utc": m.get("last_fill_utc")}
            except Exception as e:
                d["coins"][sym] = {"state": f"heartbeat error: {str(e)[:80]}"}
        for sym, st in getattr(self, "state", {}).items():
            d["coins"].setdefault(sym, {"state": st})
        tmp = HEARTBEAT + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, HEARTBEAT)

    def run_forever(self):
        last_note = time.time()
        while True:
            try:
                self.ex.sync_time()
                self.step()
                self.heartbeat()
            except Exception as e:
                self.log(f"mm: {e}")
                self.con.execute("INSERT INTO events VALUES(?,?,?,?)", (now(), "*", "error", str(e)[:200]))
                self.con.commit()
                time.sleep(30)
            if self.digest and time.time() - last_note > 3600:          # hourly fill digest
                self.notify("Market maker (MEXC)\n" + "\n".join("• " + x for x in self.digest[:20]) +
                            (f"\n…and {len(self.digest) - 20} more" if len(self.digest) > 20 else ""))
                self.digest, last_note = [], time.time()
            time.sleep(LOOP_S)


def main():
    import mexc_exchange as X
    with open(CFG) as f:
        cfg = json.load(f)
    if not cfg.get("enabled"):
        print("mm.json: enabled is false; nothing to do")
        return 0
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)

    def tg(text):
        try:
            import alerts
            cred = alerts.read_cred()
            if cred:
                alerts.send(*cred, text)
        except Exception:
            pass
    ex = X.Mexc()
    try:                                 # refuse to start on a key that can withdraw, or that cannot trade
        ex.sync_time()
        ex.account()
    except Exception as e:
        print(f"mm: not starting: {e}")
        tg(f"Market maker NOT started: {str(e)[:200]}")
        return 1
    tg("Market maker started: " + ", ".join(c["symbol"] for c in cfg.get("coins", [])) +
       (" (kill switch MM_STOP is set: orders stay cancelled)" if os.path.exists(STOP) else ""))
    MM(ex, cfg, con, print, tg).run_forever()


if __name__ == "__main__":
    import sys
    sys.exit(main())
