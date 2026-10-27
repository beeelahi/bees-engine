"""
Builds a small, synthetic but complete website data package for the browser smoke test (tools/site_smoke.js):
a fresh database with a few pairs (one of them from another venue, without trade counts), the full analytics
build, the paper bots and the web export, exactly as the engine runs them. Python 3.9 stdlib only.

    python3 tools/site_smoke_data.py OUT_DIR        -> OUT_DIR/index.html + OUT_DIR/data/...
"""
import os
import random
import sqlite3
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))


def main(out):
    import binance_sync as B
    import analytics as A
    import export_web as E
    import publish_site as P
    os.makedirs(out, exist_ok=True)
    E.OUT, E.ROOT = os.path.join(out, "data"), out
    db = os.path.join(out, "smoke.db")
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    con.executescript(B.SCHEMA)
    con.execute("INSERT INTO meta VALUES('history_start','2017-01-01')")
    d0 = date(2018, 1, 1)
    rnd, n = random.Random(7), (date.today() - d0).days - 1
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "ZIGUSDT"]
    for k, sym in enumerate(syms):
        ext = sym == "ZIGUSDT"
        con.execute("INSERT INTO pairs(symbol, token, status, first_seen, backfilled, first_candle) VALUES(?,?,?,?,1,?)",
                    (sym, sym[:-4], "TRADING", d0.isoformat(), d0.isoformat()))
        p = 10.0 * (k + 1)
        for i in range(n):
            d = d0 + timedelta(days=i)
            o = p
            p = max(0.01, p * (1 + rnd.gauss(0.0005, 0.035)))
            ms = int(B.d2ms(d.isoformat()))
            vq = 5e6 * (1 + rnd.random()) * (1 if k < 8 else 0.1)
            con.execute("INSERT INTO daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sym, d.isoformat(), o, max(o, p) * 1.02, min(o, p) * 0.98, p, vq / p, vq, None if ext else 1000,
                         None if ext else vq / p / 2, None if ext else vq / 2, ms, ms + 86_399_999, "smoke"))
        con.execute("INSERT INTO ticker_snapshots VALUES(?,?,?,?,?)", ((d0 + timedelta(days=n - 1)).isoformat(), sym, p, 5e6, "smoke"))
    con.commit()
    con.execute(f"CREATE VIEW IF NOT EXISTS v_universe AS {B.FILTERED_SQL}")
    A.run(con, verbose=False)
    try:
        import bot
        bot.OUT = E.OUT
        bot.run(con, lambda *a: None)
    except Exception as e:                       # the smoke test is about the page; the bots have their own tests
        print("site_smoke_data: bots skipped:", e)
    E.run(con)
    con.close()
    with open(os.path.join(HERE, "..", "web", "index.html"), encoding="utf-8") as f:
        page = P.wrap(f.read())
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    print("site_smoke_data: package written to", out)


if __name__ == "__main__":
    main(sys.argv[1])
