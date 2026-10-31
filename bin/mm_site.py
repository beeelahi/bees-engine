"""
Website data for the MM-Bee tab (Python 3.9 stdlib only): web_export/mmbee.json, rebuilt by every engine run.

Reads ONLY the paper market-making files (data/mm_paper_*.db, read-only) and the MM-Bee backtest
(data/mm_bee_backtest.json). The live market maker's database (mm.db),
its settings and its keys are never read here, so nothing about real orders can reach the public website.
"""
import glob
import json
import os
import sqlite3
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")
MAX_ROWS = 2000


def _variant(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        has = lambda t: con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone() is not None  # noqa: E731
        meta = {k: json.loads(v) for k, v in con.execute("SELECT k, v FROM p_meta") if k in ("start", "last_mid", "u_setting")}
        st = meta.get("start")
        if not st:
            return None
        mid = meta.get("last_mid") or st["mid"]
        wallet = {a: [round(f, 8), round(l, 8)] for a, f, l in con.execute("SELECT asset, free, locked FROM p_wallet")}
        usdt = sum(wallet.pop("USDT", [0, 0]))
        coins = sum(sum(v) for v in wallet.values())
        fills = con.execute("SELECT COUNT(*), COALESCE(SUM(price*qty),0), COALESCE(SUM(fee_usdt),0) FROM p_fills").fetchone()
        rounds, profit = 0, 0.0
        for t in ("grid_meta", "bee_meta"):
            if has(t):
                r = con.execute(f"SELECT COALESCE(SUM(rounds),0), COALESCE(SUM(profit_usdt),0) FROM {t}").fetchone()
                rounds, profit = rounds + r[0], profit + r[1]
        value, hold = coins * mid + usdt, st["coins"] * mid + st["usdt"]
        out = {"started_utc": st["utc"], "start_price": st["mid"], "price": mid, "settings": st.get("settings", {}),
               "start_coins": st["coins"], "start_usdt": st["usdt"], "coins": round(coins, 2), "usdt": round(usdt, 2),
               "value_usdt": round(value, 2), "hold_usdt": round(hold, 2), "vs_hold_usdt": round(value - hold, 2),
               "rounds": rounds, "profit_usdt": round(profit, 2), "fills": fills[0], "traded_usdt": round(fills[1], 2),
               "fees_usdt": round(fills[2], 4), "setup": meta.get("u_setting"),
               "taker_fills": con.execute("SELECT COUNT(*) FROM p_fills WHERE liquidity='taker'").fetchone()[0],
               "open_orders": con.execute("SELECT COUNT(*) FROM p_orders WHERE status IN ('NEW','PARTIALLY_FILLED')").fetchone()[0]}
        out["snaps"] = [[t, round(m, 8), round(v, 2), round(h, 2)] for t, m, v, h in
                        con.execute(f"SELECT utc, mid, value, hold_value FROM (SELECT * FROM p_snap ORDER BY utc DESC LIMIT {MAX_ROWS}) ORDER BY utc")]
        if has("bee_meta"):
            k = ("base", "base_utc", "base_from", "ladders", "rounds", "profit_usdt", "proceeds_usdt", "spent_usdt", "last_fill_utc", "active_utc")
            r = con.execute("SELECT " + ", ".join(k) + " FROM bee_meta").fetchone()
            out["bee"] = dict(zip(k, r)) if r else None
            out["units"] = [list(u) for u in con.execute(
                "SELECT ladder, k, state, kind, sell_px, buy_px, qty, cycles, updated_utc FROM bee_units ORDER BY ladder, k")]
            out["events"] = [list(e) for e in con.execute(
                f"SELECT utc, event, ladder, k, price, qty, profit, note FROM bee_log ORDER BY rowid DESC LIMIT {MAX_ROWS}")]
            out["fill_list"] = [list(f) for f in con.execute(
                f"SELECT utc, side, price, qty, liquidity FROM p_fills ORDER BY n DESC LIMIT {MAX_ROWS}")]
            out["orders"] = [list(o) for o in con.execute(
                "SELECT side, price, qty - filled FROM p_orders WHERE status IN ('NEW','PARTIALLY_FILLED') ORDER BY price DESC")]
        return out
    finally:
        con.close()


def build(data=DATA):
    out = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "paper": True, "variants": {}}
    for p in sorted(glob.glob(os.path.join(data, "mm_paper_*.db"))):
        try:
            v = _variant(p)
        except sqlite3.Error:            # busy or damaged: publish nothing this run rather than a partial file
            return None
        if v:
            name = os.path.basename(p)[9:-3]
            if name.startswith(("np_", "u_")):   # new-position and Live Paper U runs: the summary, the hourly curve and the last events only
                v["events"] = (v.get("events") or [])[:40]
                for k in ("fill_list", "orders", "units"):
                    v.pop(k, None)
                v["snaps"] = v["snaps"][-24 * 30:]
            out["variants"][name] = v
    try:                                 # MM-Bee on other coins (research/mm_bee_multi.py, once a day)
        with open(os.path.join(data, "mm_bee_multi.json")) as f:
            out["multi"] = json.load(f)
    except (OSError, ValueError):
        pass
    try:                                 # MM-Bee for new positions (research/mm_bee_tune.py): totals and the key settings per coin
        with open(os.path.join(data, "mm_bee_tune.json")) as f:
            t = json.load(f)
        keep = ("pro+chase8", "pro+chase15", "pro", "plus", "v2")
        running = {n[2:]: v.get("setup") for n, v in out["variants"].items() if n.startswith("u_")}   # Live Paper U settings
        out["tune"] = {k: t[k] for k in ("generated_utc", "position_usd", "share", "units", "taker_fee", "totals") if k in t}
        out["tune"]["coins"] = {s: {"best": c["best"], "start_utc": c["start_utc"],
                                    "results": {n: c["results"][n] for n in set(keep) | {c["best"], running.get(s.lower())} if n in c["results"]}}
                                for s, c in t.get("coins", {}).items()}
    except (OSError, ValueError, KeyError):
        pass
    try:                                 # the MM-Bee backtest (mm_bee_backtest.py), when it has run
        with open(os.path.join(data, "mm_bee_backtest.json")) as f:
            out["backtest"] = json.load(f)
    except (OSError, ValueError):
        pass
    return out


def export(out_dir, data=DATA, log=print):
    """Write mmbee.json when the paper results changed. Returns True when the file was (re)written."""
    d = build(data)
    if not d or not (d["variants"] or d.get("backtest")):
        return False
    path = os.path.join(out_dir, "mmbee.json")
    body = {k: v for k, v in d.items() if k != "generated_utc"}
    try:
        with open(path) as f:
            old = json.load(f)
        if {k: v for k, v in old.items() if k != "generated_utc"} == json.loads(json.dumps(body)):
            return False
    except (OSError, ValueError):
        pass
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, path)
    return True
