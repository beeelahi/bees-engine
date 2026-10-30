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
    live_view(con, E.OUT)
    con.close()
    paper_mm(E.OUT)
    world(E.OUT)
    with open(os.path.join(HERE, "..", "web", "index.html"), encoding="utf-8") as f:
        page = P.wrap(f.read())
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    print("site_smoke_data: package written to", out)


def live_view(con, out_data):
    """Hourly candles for 'today' so the bots' Live now card has something to show (bot_live.json)."""
    import json
    import bot_live
    try:
        b = json.load(open(os.path.join(out_data, "bot.json")))
    except (OSError, ValueError):
        return
    d0 = bot_live.day_ms(b["data_through"]) + bot_live.DAY
    con.execute("CREATE TABLE IF NOT EXISTS candles_1h(symbol TEXT, open_ms INTEGER, open REAL, high REAL, low REAL, close REAL, close_ms INTEGER)")
    for (sym, c) in con.execute("SELECT symbol, close FROM daily WHERE date=?", (b["data_through"],)).fetchall():
        for k in range(3):
            con.execute("INSERT INTO candles_1h VALUES(?,?,?,?,?,?,?)", (sym, d0 + k * 3_600_000, c, c * 1.01, c * 0.995, c * 1.004, d0 + (k + 1) * 3_600_000))
    bot_live.run(con, out_data, now_ms=d0 + 3 * 3_600_000 + 1)


def world(out_data):
    """The World tab's data from canned replies (the same ones the unit tests use)."""
    import tempfile
    sys.path.insert(0, os.path.join(HERE, ".."))
    import world_markets as W
    from tests.test_world_markets import fake
    W.refresh(force=True, log=print, fetch=fake, out=os.path.join(out_data, "world.json"), db=os.path.join(tempfile.mkdtemp(), "world.db"))


def paper_mm(out_data):
    """A short MM-Bee + grid paper run on a synthetic ZIG walk, exported as the MM-Bee tab's data (mmbee.json)."""
    import json
    import tempfile
    import time as T
    from decimal import Decimal
    import mm_paper as MP
    import mm_site

    class Pub:
        def __init__(s):
            s.bk, s.tr, s._offset, s.t = ([(0.0420, 1e7)], [(0.0421, 1e7)]), [], 0, 0

        def info(s, sym): return {"base": "ZIG", "quote": "USDT", "step": Decimal("0.01"), "tick": Decimal("0.000001"), "min_quote": Decimal("1"), "trading": True}
        def qty(s, sym, q): return (Decimal(str(q)) / Decimal("0.01")).to_integral_value(rounding="ROUND_DOWN") * Decimal("0.01")
        def px(s, sym, p, up=False): return (Decimal(str(p)) / Decimal("0.000001")).to_integral_value(rounding="ROUND_UP" if up else "ROUND_DOWN") * Decimal("0.000001")
        def book(s, sym, limit=20): return s.bk
        def trades(s, sym, limit=1000): return list(s.tr)
        def klines(s, sym, interval, limit=3): return [(0, 0.0410, 1)]
        def sync_time(s): pass

    tmp = tempfile.mkdtemp()
    MP.DATA = tmp
    MP.mm_bot.STOP = MP.mm_bee.STOP = MP.mm_grid_live.STOP = os.path.join(tmp, "STOP")
    pub = Pub()
    feed = MP.Feed(pub)
    clock = [int(T.time() * 1000) - 3 * 86_400_000]
    feed.now_ms = lambda: clock[0]
    feed.refresh("ZIGUSDT")
    grid = {"symbol": "ZIGUSDT", "mode": "grid", "step": 0.015, "levels": 20, "size": 15000, "floor": 700000, "start_coins": 1000000,
            "band": [0.03, 0.08], "max_usdt": 10000, "buy_rungs": 15, "buy_orders_live": 15, "sell_orders_live": 10}
    runs = [MP.Variant(n, c, feed, None, log=lambda *a: None) for n, c in MP.variants(grid)]
    for vn, extra in MP.BEE_VARIANTS:
        bc = dict(MP.bee_coin({"symbol": "ZIGUSDT"}, {}, extra), btc_guard=0, vol_fast=0, drip_max=0)   # no BTC feed / sleeps here
        runs.append(MP.Variant(vn, bc, feed, None, log=lambda *a: None))
    np_c = dict(MP.np_coin("ZIGUSDT", feed, data=tmp), btc_guard=0, drip_max=0)       # a new-position run (Pro + buy-back)
    runs.append(MP.Variant("np_aaausdt", np_c, feed, None, log=lambda *a: None))
    rnd, px = random.Random(11), 0.0420
    for i in range(72 * 4):
        clock[0] += 900_000
        px = max(0.03, px * (1 + rnd.gauss(0.0004, 0.012)))
        pub.bk = ([(px * 0.9995, 1e7)], [(px * 1.0005, 1e7)])
        pub.tr = [(clock[0] - 1000, px * (1 + rnd.choice((-1, 1)) * 0.004), 1e6, rnd.choice(("BUY", "SELL")))]
        feed.refresh("ZIGUSDT")
        for v in runs:
            v.step()
            if i % 4 == 0:
                v.ex.set_meta("snap_t", 0)
    for v in runs:
        v.con.close()
    import mm_bee_backtest as BT                 # backtest on a synthetic hourly walk
    t0, cs, p = BT.t_of(BT.START) - 2 * 86400, [], 0.058
    for i in range(18 * 24):
        o = p
        p = max(0.02, p * (1 + rnd.gauss(-0.0006, 0.01)))
        cs.append((t0 + i * 3600, o, max(o, p) * 1.004, min(o, p) * 0.996, p))
    with open(os.path.join(tmp, "mm_bee_backtest.json"), "w") as f:
        st0 = BT.iso(cs[0][0] - cs[0][0] % 86400 + 2 * 86400)    # the synthetic walk is short: every period starts here
        runs = [dict(BT.run(cs, start=st0, settings=gs), id=pid, label=lab, strategy=g, strategy_label=gl)
                for g, gl, gs in BT.STRATEGIES for pid, _, lab in BT.PERIODS]
        json.dump(dict(runs[0], runs=runs), f)
    multi = {"generated_utc": BT.iso(T.time()), "position_usd": 25000.0, "share": 0.25, "units": 20, "strategies": ["v2", "pro"], "coins": []}
    for sym in ("AAAUSDT", "BBBUSDT"):                        # the other-coin table, on the same synthetic walk
        c = {"symbol": sym, "start_utc": BT.START, "real": {}, "mirror": {}, "periods": {"15d": {}, "1y": {}}}
        for g, gs in (("v2", {}), ("pro", {"recycle": "ladder"})):
            for path in ("real", "mirror"):
                r = BT.run(cs, start=BT.START, settings=dict(gs, btc_guard=0), symbol=sym)
                c[path][g] = {"days": 15.0, "start_price": r["start_price"], "end_price": r["end_price"], "rounds": r["rounds"], "sells": r["sells"], "buys": r["buys"],
                              "profit_usdt": r["profit_usdt"], "vs_hold_usdt": r["vs_hold_usdt"], "locked_pct_year": 12.3}
                c["periods"]["15d"][g] = c["periods"]["1y"][g] = c["real"][g]
        multi["coins"].append(c)
    with open(os.path.join(tmp, "mm_bee_multi.json"), "w") as f:
        json.dump(multi, f)
    names = ["pro+chase8", "pro+chase15", "v2+chase8", "pro", "plus", "v2"]
    res = lambda a, b: {"real": {"vs_hold": a, "locked": abs(a), "rounds": 10, "taker": 1}, "mirror": {"vs_hold": b, "locked": abs(b), "rounds": 9, "taker": 2}}  # noqa: E731
    tune = {"generated_utc": BT.iso(T.time()), "position_usd": 25000.0, "share": 0.25, "units": 20, "taker_fee": 0.001, "settings": names,
            "totals": [{"setting": n, "real": 1000.0 - 100 * k, "mirror": 500.0 - 400 * k, "worst_sum": 400.0 - 300 * k, "coins_ahead_both": max(0, 2 - k)} for k, n in enumerate(names)],
            "coins": {s: {"start_utc": BT.START, "start_price": 1.0, "best": "pro+chase8", "results": {n: res(300.0 - 50 * k, 200.0 - 400 * k) for k, n in enumerate(names)}}
                      for s in ("AAAUSDT", "BBBUSDT")}}
    with open(os.path.join(tmp, "mm_bee_tune.json"), "w") as f:
        json.dump(tune, f)
    mm_site.export(out_data, data=tmp)
    print("site_smoke_data: MM-Bee paper data:", sorted(json.load(open(os.path.join(out_data, "mmbee.json")))["variants"]))


if __name__ == "__main__":
    main(sys.argv[1])
