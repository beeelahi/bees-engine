"""
MM-Bee on other coins (Bilal, 29 Oct 2026): he plans ~$25K positions in about 10 coins from his portfolio list and
wants 25% of each traded by the market maker. Same code as ZIG (mm_bee), sized in dollars: 20 units, together 25% of
the position at the start price, sell first, no wallet USDT. Binance 1-hour candles from the engine database, last
365 days (or less if the coin is younger), real path and mirror image. Run on the server (ops: bee-study).

    python3 research/mm_bee_multi.py [DB] > out.txt
"""
import json
import os
import sqlite3
import sys
import time

import _env  # noqa: F401
import mm_bee_backtest as BT
from mm_bee_study2 import mirror

COINS = ["MORPHOUSDT", "ONDOUSDT", "CAKEUSDT", "UNIUSDT", "JUPUSDT", "PENGUUSDT", "FLOKIUSDT", "PEPEUSDT", "DOGEUSDT",
         "TAOUSDT", "SUIUSDT", "APTUSDT", "SOLUSDT", "ETHUSDT", "BTCUSDT"]
POSITION, SHARE, UNITS = 25000.0, 0.25, 20
STRATS = [("v2", {"vol_fast": 0}), ("plus", {"recycle": "ladder", "vol_fast": 0}),
          ("pro", {"recycle": "ladder", "vol_fast": 0, "first": 0.04, "gap": 0.01, "rebuy": 0.045, "resell": 0.045})]


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_env.HERE, "..", "data", "binance.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    last = con.execute("SELECT MAX(open_ms) FROM candles_1h WHERE symbol='BTCUSDT'").fetchone()[0] // 1000
    since = last - 367 * 86400
    btc = BT.candles_from_db(con, "BTCUSDT", since)
    print(f"# MM-Bee on other coins: ${POSITION:,.0f} position, {100 * SHARE:.0f}% traded, {UNITS} units; 1h Binance candles")
    out = {"generated_utc": BT.iso(time.time()), "position_usd": POSITION, "share": SHARE, "units": UNITS,
           "strategies": [n for n, _ in STRATS], "coins": []}
    for sym in COINS:
        rows = BT.candles_from_db(con, sym, since)
        if len(rows) < 24 * 60:
            print(f"{sym:12s} not enough hourly history ({len(rows)} candles)")
            continue
        start = BT.iso(rows[0][0] - rows[0][0] % 86400 + 2 * 86400)
        p0 = rows[next(i for i, r in enumerate(rows) if r[0] >= BT.t_of(start)) - 1][4]
        coins = POSITION / p0
        size = coins * SHARE / UNITS
        coin = {"symbol": sym, "start_utc": start, "real": {}, "mirror": {}, "periods": {}}
        out["coins"].append(coin)
        for pid, pstart, _ in BT.scenarios(last + 3600):          # 15-D ... 1-Y on real prices, a fresh position each
            i0 = next((i for i, r in enumerate(rows) if r[0] >= BT.t_of(pstart)), None)
            if not i0:
                continue
            cp = POSITION / rows[i0 - 1][4]
            for name, st in STRATS:
                st = dict(st, btc_guard=0) if sym == "BTCUSDT" else st
                try:
                    r = BT.run(rows, start=pstart, settings=dict(st, size=cp * SHARE / UNITS, units=UNITS, extra_ladders=0, max_usdt=0),
                               coins=cp, usdt=0.0, btc=btc, symbol=sym)
                except RuntimeError:
                    continue
                coin["periods"].setdefault(pid, {})[name] = {k: r[k] for k in ("start_utc", "end_utc", "start_price", "end_price", "rounds",
                                                                                 "profit_usdt", "vs_hold_usdt", "sells", "buys")}
        for path, z, b in (("real", rows, btc), ("mirror", mirror(rows), mirror(btc))):
            for name, st in STRATS:
                if sym == "BTCUSDT":
                    st = dict(st, btc_guard=0)                     # the guard compares a coin with BTC: not for BTC itself
                r = BT.run(z, start=start, settings=dict(st, size=size, units=UNITS, extra_ladders=0, max_usdt=0),
                           coins=coins, usdt=0.0, btc=b, symbol=sym)
                days = (BT.t_of(r["end_utc"]) - BT.t_of(start)) / 86400
                traded = POSITION * SHARE
                coin[path][name] = {"days": round(days, 1), "start_price": r["start_price"], "end_price": r["end_price"],
                                    "rounds": r["rounds"], "profit_usdt": round(r["profit_usdt"], 2), "vs_hold_usdt": round(r["vs_hold_usdt"], 2),
                                    "locked_pct_year": round(100 * r["profit_usdt"] / traded / days * 365, 1) if days else None}
                print(f"{sym:12s} {path:6s} {name:4s} {days:4.0f}d price {r['start_price']:.6g}->{r['end_price']:.6g} "
                      f"({100 * (r['end_price'] / r['start_price'] - 1):+.0f}%) rounds {r['rounds']:4d} locked {r['profit_usdt']:+9.2f} "
                      f"({100 * r['profit_usdt'] / traded / days * 365:+.0f}%/yr of the traded ${traded:,.0f}) vs hold {r['vs_hold_usdt']:+9.2f}", flush=True)

    path = os.path.join(os.path.dirname(db), "mm_bee_multi.json")
    with open(path + ".tmp", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    print(f"# written {path}")


if __name__ == "__main__":
    main()
