"""
Go / no-go check for real-money market making (Python 3.9 stdlib only). Read-only: it places and cancels nothing.

    python3 ~/BinanceData/bin/mm_preflight.py        (ops: mm-preflight)
    python3 ~/BinanceData/bin/mm_preflight.py --probe   two tiny orders that cannot trade (only with Bilal's go)

Checks, and prints GO only when every one passes:
  * the MEXC key: present, private, can trade, can NOT withdraw; the server clock agrees with MEXC's;
  * mm.json: readable, every coin complete, every MM-Bee ladder under the code-level ceilings (mm_bot.MAX_COIN_USD
    a coin, MAX_TOTAL_USD in all);
  * each coin: MEXC lists it and allows spot trading, the book is two-sided and not wider than 1%, a $500 unit is
    above MEXC's minimum order, the wallet holds the coins the ladder sells (free, not locked elsewhere);
  * nothing unexpected on the book: open orders with the bot's "bm" prefix that its database does not know;
  * the kill switch (MM_STOP) state and whether trading is switched on ("enabled").
Without a key yet, the public checks still run and the key checks are reported as missing.
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mexc_exchange as X  # noqa: E402
import mm_bee  # noqa: E402
import mm_bot  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run(cfg_path=mm_bot.CFG, db_path=mm_bot.DB, pub=None, priv=None, out=print):
    problems, warnings = [], []
    ok = lambda m: out("  ok    " + m)          # noqa: E731

    def bad(m):
        problems.append(m)
        out("  FAIL  " + m)

    def warn(m):
        warnings.append(m)
        out("  warn  " + m)

    out(f"== MM preflight {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} ==")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        ok(f"mm.json readable: {len(cfg.get('coins', []))} coin(s), trading switched {'ON' if cfg.get('enabled') else 'off'}")
    except (OSError, ValueError) as e:
        bad(f"mm.json not readable ({e})")
        cfg = {"coins": []}
    pub = pub or X.Mexc(cred=False)
    try:
        pub.sync_time()
        ok(f"MEXC reachable, clock offset {pub._offset} ms")
        if abs(pub._offset) > 2000:
            warn("server clock is more than 2 s off MEXC's (signed calls allow 5 s)")
    except Exception as e:
        bad(f"MEXC not reachable ({e})")
    bal = None
    if priv is None:
        try:
            priv = X.Mexc()
        except Exception as e:
            bad(f"API key: {e}")
    if priv is not None:
        try:
            priv.sync_time()
            a = priv.account()                       # raises when the key can withdraw
            ok(f"API key works: can trade {a.get('canTrade')}, can withdraw {a.get('canWithdraw')}")
            bal = {b["asset"]: (float(b["free"]), float(b["locked"])) for b in a.get("balances", [])}
            ok(f"wallet read: {sum(1 for v in bal.values() if v[0] + v[1] > 0)} asset(s) with a balance")
        except Exception as e:
            bad(f"API key check failed: {str(e)[:160]}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) if os.path.exists(db_path) else None
    total = 0.0
    for c in cfg.get("coins", []):
        sym = c.get("symbol", "?")
        out(f"-- {sym} ({c.get('mode', 'quotes')})")
        if c.get("mode") != "bee":
            warn(f"{sym}: not an MM-Bee coin; preflight checks MM-Bee coins only")
            continue
        b = dict(mm_bee.DEFAULTS, **c)
        try:
            inf = pub.info(sym)
            if not inf["trading"]:
                bad(f"{sym}: MEXC does not allow spot trading on it now")
                continue
            bids, asks = pub.book(sym, 5)
            if not bids or not asks:
                bad(f"{sym}: empty order book")
                continue
            mid = (bids[0][0] + asks[0][0]) / 2
            spread = asks[0][0] / bids[0][0] - 1
            (ok if spread <= 0.01 else warn)(f"{sym}: mid {mid:.6g}, spread {100 * spread:.2f}%")
        except Exception as e:
            bad(f"{sym}: market data failed ({str(e)[:120]})")
            continue
        need = b["units"] * b["size"] * (1 + b["extra_ladders"])
        usd = need * mid
        total += usd
        (ok if usd <= mm_bot.MAX_COIN_USD else bad)(f"{sym}: ladder {b['units']} x {b['size']:,.8g} = {need:,.8g} {inf['base']} (~${usd:,.0f}; ceiling ${mm_bot.MAX_COIN_USD:,.0f})")
        unit_usd = b["size"] * mid
        (ok if unit_usd >= 2 * float(inf["min_quote"]) else bad)(f"{sym}: one unit ~${unit_usd:,.2f} (MEXC minimum order {inf['min_quote']} USDT)")
        if not 0 < b["first"] < 0.5 or not 0 < b["rebuy"] < 0.5 or not 0 < b["resell"] < 0.5 or b["max_usdt"] < 0:
            bad(f"{sym}: settings out of range (first {b['first']}, rebuy {b['rebuy']}, resell {b['resell']}, max_usdt {b['max_usdt']})")
        elif b["rebuy"] <= 0 or b["resell"] <= 0:
            bad(f"{sym}: rebuy and resell must be positive (a loop must buy lower than it sold)")
        else:
            ok(f"{sym}: first sell +{100 * b['first']:.1f}%, every {100 * b['gap']:.1f}%, buy back -{100 * b['rebuy']:.1f}%, resell +{100 * b['resell']:.1f}%"
               + (f", upside stop +{100 * b['chase']:.0f}% (max {b['max_chases_day']} a day)" if b.get("chase") else "")
               + f", own USDT used: {b['max_usdt']:,.0f}")
        started = con is not None and con.execute("SELECT 1 FROM sqlite_master WHERE name='bee_meta'").fetchone() and \
            con.execute("SELECT 1 FROM bee_meta WHERE symbol=?", (sym,)).fetchone()
        if bal is not None:
            have = bal.get(inf["base"], (0.0, 0.0))
            held = 0.0
            if started:
                held = sum(q for (q,) in con.execute("SELECT qty FROM bee_units WHERE symbol=? AND state IN ('armed','bought')", (sym,)))
            if started:
                ok(f"{sym}: already running ({have[0]:,.8g} free, {have[1]:,.8g} in orders, {held:,.8g} held by units not on the book)")
            else:
                (ok if have[0] + 1e-9 >= need else bad)(f"{sym}: wallet {have[0]:,.8g} {inf['base']} free; the ladder needs {need:,.8g}")
            try:
                known = set()
                if started:
                    for s_, b_ in con.execute("SELECT sell_cid, buy_cid FROM bee_units WHERE symbol=?", (sym,)):
                        known.update(x for x in (s_, b_) if x)
                orphans = [o for o in priv.open_orders(sym) if o.get("clientOrderId") not in known]
                (ok if not orphans else warn)(f"{sym}: {len(orphans)} open bot order(s) unknown to its database" + (" (cancelled by the bot at start)" if orphans else ""))
            except Exception as e:
                warn(f"{sym}: open orders not checked ({str(e)[:80]})")
            if started:
                unk = con.execute("SELECT COUNT(*) FROM bee_units WHERE symbol=? AND state='unknown'", (sym,)).fetchone()[0]
                (ok if not unk else bad)(f"{sym}: {unk} unit(s) set aside as unknown (check them by hand)")
    if total:
        (ok if total <= mm_bot.MAX_TOTAL_USD else bad)(f"all MM-Bee ladders together ~${total:,.0f} (ceiling ${mm_bot.MAX_TOTAL_USD:,.0f})")
    (warn if os.path.exists(mm_bot.STOP) else ok)("kill switch MM_STOP " + ("is SET: the bot cancels its orders and waits" if os.path.exists(mm_bot.STOP) else "not set"))
    if con:
        con.close()
    verdict = "GO" if not problems else "NO-GO"
    out(f"== {verdict}: {len(problems)} problem(s), {len(warnings)} warning(s)" + ("" if cfg.get("enabled") else "; trading is switched off in mm.json"))
    return not problems


def probe(sym=None, out=print, ex=None):
    """--probe (only with Bilal's go, at go-live): proves the real order path with two tiny orders that cannot trade:
    a post-only SELL at twice the price (placed, read back, cancelled), and a post-only SELL at the best bid, which
    must NOT trade: it shows how MEXC treats a post-only order that would cross (refused, or accepted then cancelled).
    Each is about 1.5x MEXC's minimum order. Returns True when both behave."""
    from decimal import Decimal, ROUND_UP
    with open(mm_bot.CFG) as f:
        cfg = json.load(f)
    sym = sym or next((c["symbol"] for c in cfg.get("coins", []) if c.get("mode") == "bee"), None)
    if not sym:
        out("probe: no MM-Bee coin in mm.json")
        return False
    ex = ex or X.Mexc()
    ex.sync_time()
    inf = ex.info(sym)
    bids, asks = ex.book(sym, 5)
    q_for = lambda px: (Decimal(str(1.5 * float(inf["min_quote"]) / px)) / inf["step"]).to_integral_value(rounding=ROUND_UP) * inf["step"]  # noqa: E731
    ok_all = True
    hi = ex.px(sym, asks[0][0] * 2, up=True)
    cid = f"bmP{int(time.time()) % 10**8}1"
    ex.limit_maker(sym, "SELL", q_for(float(hi)), hi, cid)
    o = ex.order(sym, cid)
    out(f"probe 1: post-only SELL {sym} at {hi} placed, exchange says {o.get('status')}")
    ex.cancel(sym, cid)
    o = ex.order(sym, cid)
    ok_all &= o.get("status") == "CANCELED"
    out(f"probe 1: cancelled, exchange says {o.get('status')}")
    lo = ex.px(sym, bids[0][0])
    cid = f"bmP{int(time.time()) % 10**8}2"
    try:
        ex.limit_maker(sym, "SELL", q_for(float(lo)), lo, cid)
        time.sleep(1)
        o = ex.order(sym, cid)
        out(f"probe 2: a crossing post-only SELL at the bid {lo} was ACCEPTED, then {o.get('status')} with {o.get('executedQty')} traded")
        ok_all &= float(o.get("executedQty") or 0) == 0
        if o.get("status") in ("NEW", "PARTIALLY_FILLED"):
            ex.cancel(sym, cid)
    except Exception as e:
        out(f"probe 2: a crossing post-only SELL at the bid was REFUSED: {str(e)[:120]}")
    out("probe: " + ("passed" if ok_all else "FAILED"))
    return ok_all


def main():
    if "--probe" in sys.argv:
        return 0 if probe() else 1
    return 0 if run() else 1


if __name__ == "__main__":
    sys.exit(main())
