"""
MM-Bee for new long positions (Bilal, 29 Oct 2026): ~$25K in each of about 10 coins, 25% of each traded. The year
study (mm_bee_multi.py) showed the market maker earns on every coin but, in a strong rally, selling a quarter of the
coin on the way up costs more than the rounds earn. This study tests two fixes on top of the three bots:
  * chase (upside stop): a unit that sold and is waiting to buy back lower buys back at the market once the price runs
    `chase` above its sell price (a small capped loss, taker fee 0.1%, instead of staying out of the rally);
  * trend: while the coin trades more than 3% above its 30-candle 4h EMA, the sell ladder starts twice as far and is
    twice as wide (sells less in an uptrend).
Every setting runs on the real year and on its mirror image (the same moves in the other direction), so a setting
has to work in a falling and in a rising market. Ranked by the worse of the two (robust), then by their average.

    python3 research/mm_bee_tune.py [DB]        (server: ops bee-tune, read with bee-tune-log)
Writes data/mm_bee_tune.json next to the database.
"""
import itertools
import json
import os
import sqlite3
import sys
import time
from multiprocessing import Pool

import _env  # noqa: F401
import mm_bee_backtest as BT
from mm_bee_study2 import mirror
from mm_bee_multi import COINS, POSITION, SHARE, UNITS

FAMILIES = {"v2": {"vol_fast": 0}, "plus": {"recycle": "ladder", "vol_fast": 0},
            "pro": {"recycle": "ladder", "vol_fast": 0, "first": 0.04, "gap": 0.01, "rebuy": 0.045, "resell": 0.045}}
CHASE = (0.0, 0.08, 0.15)
TREND = (False, True)
TAKER_FEE = 0.001


def settings(fam, chase, trend):
    s = dict(FAMILIES[fam], chase=chase)
    if trend:
        f, g = s.get("first", BT.mm_bee.DEFAULTS["first"]), s.get("gap", BT.mm_bee.DEFAULTS["gap"])
        s["regimes"] = {"up": {"first": 2 * f, "gap": 2 * g}}
    return s


def name(fam, chase, trend):
    return fam + (f"+chase{int(round(100 * chase))}" if chase else "") + ("+trend" if trend else "")


def job(a):
    sym, path, fam, chase, trend, rows, btc, start, coins = a
    st = settings(fam, chase, trend)
    if sym == "BTCUSDT":
        st["btc_guard"] = 0
    size = coins * SHARE / UNITS
    r = BT.run(rows, start=start, settings=dict(st, size=size, units=UNITS, extra_ladders=0, max_usdt=0),
               coins=coins, usdt=0.0, btc=btc, symbol=sym, taker_fee=TAKER_FEE)
    return (sym, path, name(fam, chase, trend), {"vs_hold": round(r["vs_hold_usdt"], 2), "locked": round(r["profit_usdt"], 2),
                                                  "rounds": r["rounds"], "taker": r["taker_fills"], "start_price": r["start_price"],
                                                  "end_price": r["end_price"], "end_utc": r["end_utc"]})


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_env.HERE, "..", "data", "binance.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    last = con.execute("SELECT MAX(open_ms) FROM candles_1h WHERE symbol='BTCUSDT'").fetchone()[0] // 1000
    since = last - 367 * 86400
    btc = BT.candles_from_db(con, "BTCUSDT", since)
    combos = list(itertools.product(FAMILIES, CHASE, TREND))
    print(f"# MM-Bee tuning: {len(COINS)} coins x {len(combos)} settings x real/mirror year; ${POSITION:,.0f} position, "
          f"{100 * SHARE:.0f}% traded, taker fee {100 * TAKER_FEE:.1f}% on chase buys", flush=True)
    jobs, meta = [], {}
    for sym in COINS:
        rows = BT.candles_from_db(con, sym, since)
        if len(rows) < 24 * 60:
            print(f"{sym}: not enough hourly history", flush=True)
            continue
        start = BT.iso(rows[0][0] - rows[0][0] % 86400 + 2 * 86400)
        p0 = rows[next(i for i, r in enumerate(rows) if r[0] >= BT.t_of(start)) - 1][4]
        coins = POSITION / p0
        meta[sym] = {"start_utc": start, "start_price": p0}
        for path, z, b in (("real", rows, btc), ("mirror", mirror(rows), mirror(btc))):
            for fam, ch, tr in combos:
                jobs.append((sym, path, fam, ch, tr, z, b, start, coins))
    res, t0 = {}, time.time()
    with Pool(max(1, min(2, os.cpu_count() or 1))) as pool:
        for k, (sym, path, nm, r) in enumerate(pool.imap_unordered(job, jobs, chunksize=2), 1):
            res.setdefault(sym, {}).setdefault(nm, {})[path] = r
            if k % 20 == 0:
                print(f"  {k}/{len(jobs)} runs, {time.time() - t0:.0f}s", flush=True)
    names = [name(*c) for c in combos]
    out = {"generated_utc": BT.iso(time.time()), "position_usd": POSITION, "share": SHARE, "units": UNITS,
           "taker_fee": TAKER_FEE, "settings": names, "coins": {}}
    print("\n# per coin: best setting by the worse of real / mirror (USDT ahead of holding)")
    for sym in COINS:
        if sym not in res:
            continue
        R = res[sym]
        rank = sorted(names, key=lambda n: (-min(R[n]["real"]["vs_hold"], R[n]["mirror"]["vs_hold"]),
                                            -(R[n]["real"]["vs_hold"] + R[n]["mirror"]["vs_hold"])))
        out["coins"][sym] = dict(meta[sym], results=R, best=rank[0])
        b, base = R[rank[0]], R["v2"]
        print(f"{sym:11s} best {rank[0]:22s} real {b['real']['vs_hold']:+9.0f} mirror {b['mirror']['vs_hold']:+9.0f} | "
              f"v2 real {base['real']['vs_hold']:+9.0f} mirror {base['mirror']['vs_hold']:+9.0f}", flush=True)
    print("\n# one setting for every coin: totals across coins (USDT ahead of holding)")
    tot = []
    for n in names:
        rr = sum(out["coins"][s]["results"][n]["real"]["vs_hold"] for s in out["coins"])
        mm = sum(out["coins"][s]["results"][n]["mirror"]["vs_hold"] for s in out["coins"])
        worst = sum(min(out["coins"][s]["results"][n]["real"]["vs_hold"], out["coins"][s]["results"][n]["mirror"]["vs_hold"]) for s in out["coins"])
        ok = sum(1 for s in out["coins"] if min(out["coins"][s]["results"][n]["real"]["vs_hold"], out["coins"][s]["results"][n]["mirror"]["vs_hold"]) > 0)
        tot.append((n, rr, mm, worst, ok))
    tot.sort(key=lambda x: (-x[3], -(x[1] + x[2])))
    out["totals"] = [{"setting": n, "real": round(a, 2), "mirror": round(b, 2), "worst_sum": round(w, 2), "coins_ahead_both": ok} for n, a, b, w, ok in tot]
    for n, a, b, w, ok in tot:
        print(f"{n:22s} real {a:+10.0f} mirror {b:+10.0f} worst-case sum {w:+10.0f}  ahead on both paths: {ok}/{len(out['coins'])}")
    path = os.path.join(os.path.dirname(db), "mm_bee_tune.json")
    with open(path + ".tmp", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    print(f"# written {path} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
