"""
The market-making plan for real money (Python 3.9 stdlib only): MM-Bee settings per coin, sized from today's price.

    python3 ~/BinanceData/bin/mm_plan.py            print the plan and write ~/BinanceData/mm.json.proposed
    python3 ~/BinanceData/bin/mm_plan.py --apply    (Bilal, on the server, interactive) copy the plan into mm.json

Sizing (Bilal, 30 Oct 2026): each new position is $25,000; 40% of it ($10,000) is market-made in 20 units of $500,
sell first, no wallet USDT (buys are paid only from what the sells raised). Settings: MM-Bee Pro with the upside stop
(buy back at the market once the price runs 8% over a sell), the most robust setup of research/mm_bee_tune.py.
ZIG keeps Bilal's original allocation: 20 units of 15,000 ZIG (300,000 ZIG), MM-Bee Pro without the upside stop
(on ZIG it lowered results in both directions).

--apply never switches trading on: it keeps mm.json's "enabled" as it is (false unless Bilal set it), backs up the
old file, and asks for a typed confirmation. Turning real trading on stays a separate, deliberate step.
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CFG = os.path.join(ROOT, "mm.json")
PROPOSED = CFG + ".proposed"

POSITION_USD, SHARE, UNITS = 25_000.0, 0.40, 20
COINS = ["MORPHOUSDT", "ONDOUSDT", "CAKEUSDT", "UNIUSDT", "PEPEUSDT", "JUPUSDT", "PENGUUSDT", "TAOUSDT"]
PRO = {"recycle": "ladder", "vol_fast": 0, "first": 0.04, "gap": 0.01, "rebuy": 0.045, "resell": 0.045}
NEW = dict(PRO, chase=0.08, max_chases_day=6, units=UNITS, extra_ladders=0, max_usdt=0, acc_units=0,
           drip_min=0.5, drip_max=1.5)
ZIG = dict(PRO, units=20, size=15000, extra_ladders=0, max_usdt=0, acc_units=0)


def mid_price(ex, sym):
    b, a = ex.book(sym, 5)
    if not b or not a:
        raise RuntimeError(f"{sym}: empty order book")
    return (b[0][0] + a[0][0]) / 2


def plan(ex, coins=COINS, position=POSITION_USD, share=SHARE):
    out, notes = [], []
    zm = mid_price(ex, "ZIGUSDT")
    out.append(dict({"symbol": "ZIGUSDT", "mode": "bee"}, **ZIG))
    notes.append(f"ZIGUSDT   20 x 15,000 ZIG = 300,000 ZIG (~${300000 * zm:,.0f} at {zm:.6g}), Pro, no upside stop")
    for sym in coins:
        inf = ex.info(sym)
        if not inf.get("trading", True):
            notes.append(f"{sym}: not trading on MEXC, left out")
            continue
        m = mid_price(ex, sym)
        size = float(ex.qty(sym, position * share / UNITS / m))
        out.append(dict({"symbol": sym, "mode": "bee", "size": size, "plan_price": m, "plan_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, **NEW))
        notes.append(f"{sym:10s} 20 x {size:,.8g} {inf['base']} = {size * UNITS:,.8g} {inf['base']} (~${size * UNITS * m:,.0f} at {m:.6g}), "
                     f"Pro + buy back at +8%; first sell {m * 1.04:.6g}, last {m * (1.04 + 0.01 * (UNITS - 1)):.6g}")
    return out, notes


def main():
    import mexc_exchange as X
    ex = X.Mexc(cred=False)                       # prices only: no key needed to plan
    coins, notes = plan(ex)
    try:
        with open(CFG) as f:
            cur = json.load(f)
    except (OSError, ValueError):
        cur = {"enabled": False}
    prop = dict(cur, coins=coins)
    prop["enabled"] = bool(cur.get("enabled"))     # never switched on here
    print("Market-making plan (40% of a $25,000 position per coin, $500 units):")
    for n in notes:
        print("  " + n)
    with open(PROPOSED, "w") as f:
        json.dump(prop, f, indent=1)
    print(f"\nWritten {PROPOSED} (mm.json is unchanged; enabled stays {prop['enabled']}).")
    if "--apply" in sys.argv:
        if not sys.stdin.isatty():
            print("--apply needs Bilal at the server's terminal (interactive); nothing changed.")
            return 1
        if input("Type APPLY to copy this plan into mm.json: ").strip() != "APPLY":
            print("Nothing changed.")
            return 1
        if os.path.exists(CFG):
            shutil.copy2(CFG, CFG + time.strftime(".%Y%m%dT%H%M%S.bak"))
        old = os.umask(0o077)
        try:
            with open(CFG + ".tmp", "w") as f:
                json.dump(prop, f, indent=1)
            os.replace(CFG + ".tmp", CFG)
        finally:
            os.umask(old)
        print(f"mm.json updated (backup kept). Trading enabled: {prop['enabled']}. Run mm_preflight.py next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
