#!/usr/bin/env python3
"""
Bee's Bots - paper trading portfolio (no real orders, no exchange keys, no network access).

Runs on the Mac after every analytics rebuild:  python3 ~/BinanceData/bin/bot.py
  * One $100,000 paper portfolio split across several bots. Each bot has its own capital, rules, paper account,
    integrity log and evidence; the portfolio is their sum.
  * Every bot is replayed day by day from the inception date using only closed daily candles from
    data/binance.db. The engine is deterministic: the account can be rebuilt at any time and must come out
    identical. Each trade is recorded the first time it appears (bot_trades_log) and later runs are checked
    against that record (integrity check).
  * Each bot's full-history backtest uses the same code.
  * Writes tables bot_*, highlights for bot actions and BTC filter changes, and web_export/bot.json.
"""
import json
import math, os, sqlite3, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot_sim as S
from bot_core import day_num, score_parts, SCORE_PARTS

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(ROOT, "data", "binance.db")
OUT = os.path.join(ROOT, "web_export")

PORTFOLIO = "multi-1"
START_EQUITY = 100000.0
INCEPTION = "2026-10-23"                   # first signal day (close); first fills at the next open
BACKTEST = ("2018-03-01", "2021-12-31", "2022-01-01")   # start, end of design period, start of unseen period
V2 = dict(sides="l", n=20, exit_n=10, k=1.5, trail=0, trend=True, tp_r=2.0, tp_frac=1 / 3, be_after_tp=True)
V3 = dict(V2, rvol=1.25)       # Leaders v3: breakout day volume >= 1.25x its 20-day average (approved 2026-10-25)
COMMON_RULES = [
    "Costs: 0.1% fee per side, slippage by liquidity (0.05% to 0.6%), extra slippage on stop exits.",
]
NO_LEVERAGE = "No leverage: total exposure never above the bot's capital. Paper trading only."
BOTS = [
    {"id": "leaders", "version": "v3", "name": "Leaders", "status": "proven", "capital": 40000.0,
     "strategy": ("breakout", V3), "config": dict(top_n=10),
     "summary": "Volume-confirmed breakouts on the 10 most liquid coins, tight stop, partial profit, trailing exit.",
     "rules": [
         "Universe: the 10 most liquid USDT pairs by 20-day average volume (stablecoins, wrapped and leveraged tokens excluded), 200+ days of history.",
         "Market filter: longs only while BTC closes above its 200-day average.",
         "Entry: daily close above the highest high of the previous 20 days, above the pair's own 200-day average, on volume at least 1.25x its 20-day average (a real breakout, not a drift). Bought at the next open.",
         "Stop: entry − 1.5 × ATR(20). Never moved down.",
         "Partial profit: one third sold at +2R; the stop on the rest then moves to break-even.",
         "Trailing exit: the rest is sold at the next open after a close below the 10-day low.",
         "Size: 1% of the bot's equity at risk per trade, max 20% per position, max 10 positions."]},
    {"id": "majors", "version": "v1", "name": "Majors", "status": "proven", "capital": 30000.0,
     "strategy": ("breakout", V2), "config": dict(top_n=2, max_pos_frac=0.5, risk=0.02),
     "summary": "The same breakout rules on the 2 most liquid coins (BTC and ETH), larger positions.",
     "rules": [
         "Universe: the 2 most liquid USDT pairs by 20-day average volume (normally BTC and ETH).",
         "Market filter: longs only while BTC closes above its 200-day average.",
         "Entry: daily close above the 20-day high and the pair's 200-day average (no volume requirement: for BTC and ETH it lowered returns in testing). Bought at the next open.",
         "Stop entry − 1.5 × ATR(20); one third sold at +2R, then the stop moves to break-even; the rest exits on a close below the 10-day low.",
         "Size: 2% of the bot's equity at risk per trade, max 50% per position."]},
    {"id": "accum", "version": "v1", "name": "Accumulation", "status": "experimental", "capital": 15000.0,
     "strategy": ("accumulation", dict(vol_x=2.0, pos=0.7, max_range=2.5, k=2.0, ttl=20, exit_n=10)),
     "config": dict(top_n=0, min_vol=3e5, max_vol=3e7, min_bars=90),
     "summary": "Small and mid caps where volume is building near the top of a contained range, bought before the breakout.",
     "rules": [
         "Universe: pairs with $0.3M to $30M average daily volume and 90+ days of history. No BTC filter.",
         "Entry: 20-day average volume at least 2x the 60-day average, close in the top 30% of its 60-day range, range no wider than 2.5x, not yet above the 60-day high. Bought at the next open; strongest volume build-up first.",
         "Stop: entry − 2 × ATR(20).",
         "Exit: close below the 10-day low, or after 20 days.",
         "Size: 1% of the bot's equity at risk per trade, max 20% per position, max 10 positions.",
         "Evidence is weak (roughly break-even after costs from 2022 on, deep drawdowns): it runs to be proven or retired on live data."]},
    {"id": "turbo", "version": "v1", "name": "Turbo", "status": "experimental", "capital": 15000.0,
     "strategy": ("breakout", V3), "config": dict(top_n=10, risk=0.03, max_pos_frac=0.5, max_gross=2.0, max_pos=4),
     "leverage_rule": "Up to 2x exposure: positions together may reach twice the bot's capital (needs a margin or futures account with real money). Paper trading only.",
     "summary": "The Leaders rules with 3x the risk per trade, at most 4 positions and up to 2x exposure. Aims higher; expect deep drawdowns.",
     "rules": [
         "Universe, market filter, entry, stop and exits: exactly the Leaders v3 rules (volume-confirmed 20-day breakout on the 10 most liquid pairs, BTC above its 200-day average, stop entry − 1.5 × ATR(20), one third at +2R then break-even, exit on a close below the 10-day low).",
         "Size: 3% of the bot's equity at risk per trade, max 50% per position, at most 4 positions.",
         "Evidence (same code, delisted pairs included): about +39% per year on unseen data 2022 to 2026 with a worst drop of −37%, and a −43% worst drop over the full history; with $100,000 instead of $15,000 the same rules made about +34% per year (bigger orders fill worse). Returns are lumpy: big in bull years, flat in bear years. Approved as an experiment on 2026-10-25 (replaces Wide net)."]},
]


def load(con):
    tokens = dict(con.execute("SELECT symbol, token FROM pairs"))
    rows = con.execute("SELECT symbol, date, open, high, low, close, volume_usdt FROM daily ORDER BY symbol, date")
    return S.prepare(rows, tokens)


def iso(dn, data):
    """day number -> 'YYYY-MM-DD' via BTC's calendar (complete daily series), else arithmetic."""
    B = data["BTCUSDT"]
    i = B["idx"].get(dn)
    if i is not None:
        return B["date"][i]
    from datetime import date
    return date.fromordinal(dn - day_num("0001-01-01") + 1).isoformat()


def r(x, n=8):
    return None if x is None else float(f"{x:.{n}g}")


def rm(d):
    """Round metrics; an infinite profit factor (no losing trades) is written as null - JSON has no infinity."""
    return {k: ((None if math.isinf(v) or math.isnan(v) else r(v, 5)) if isinstance(v, float) else v) for k, v in d.items()}


def cid(t):
    return t.get("cid") or (t["sym"], t["entry_day"], 0)


def partials_by_cid(trades):
    out = {}
    for t in trades:
        if t.get("partial"):
            out.setdefault(cid(t), []).append(t)
    return out


def trade_row(t, data, eq_at, parts=()):
    """One row per position. Partial profit-takes are folded into their parent trade (never counted as
    separate wins); `partials` lists them for display."""
    full_qty = t["qty"] + sum(x["qty"] for x in parts)
    pnl = t["pnl"] + sum(x["pnl"] for x in parts)
    notional = full_qty * t["entry"]
    eq = eq_at.get(t["entry_day"] - 1) or eq_at.get(t["entry_day"])
    return {
        "symbol": t["sym"], "side": t["side"], "signal_date": iso(t["entry_day"] - 1, data),
        "entry_date": iso(t["entry_day"], data), "entry": r(t["entry"]), "stop": r(t["stop0"]),
        "exit_date": iso(t["exit_day"], data), "exit": r(t["exit"]), "reason": t["reason"],
        "qty": r(full_qty), "notional": r(notional, 6), "size_pct": r(100 * notional / eq, 4) if eq else None,
        "leverage": 1, "risk_usd": r(t["risk_amt"], 6), "pnl_usd": r(pnl, 6),
        "pnl_pct": r(100 * pnl / notional, 5) if notional else None,
        "r": r(pnl / t["risk_amt"], 4) if t["risk_amt"] else None, "days": t["bars"],
        "partials": [{"date": iso(x["exit_day"], data), "price": r(x["exit"]), "share": r(x["qty"] / full_qty, 3),
                      "pnl_usd": r(x["pnl"], 6)} for x in parts],
    }


def paper_account(bot, data, regime, last):
    strat = S.STRATS[bot["strategy"][0]](**bot["strategy"][1])
    cfg = dict(S.DEFAULTS)
    cfg.update(bot["config"])
    cfg["start_equity"] = bot["capital"]
    a = day_num(INCEPTION)
    st = S.simulate(data, regime, strat, a, last, cfg)
    eq_at = dict(st["curve"])
    parts = partials_by_cid(st["trades"])
    closed = [trade_row(t, data, eq_at, parts.get(cid(t), ())) for t in st["trades"] if not t.get("partial")]
    opened = []
    for p in st["positions"]:
        F = data[p["sym"]]
        px = F["c"][p["last_i"]]
        pp = parts.get((p["sym"], p["entry_day"], p.get("unit", 0)), [])
        full_qty = p["qty"] + sum(x["qty"] for x in pp)
        notional = full_qty * p["entry"]
        unreal = p["qty"] * (px - p["entry"]) if p["side"] == "long" else p["qty"] * (p["entry"] - px)
        pnl = unreal - p["cost"] + sum(x["pnl"] for x in pp)   # after fees so far; exit fee on the rest not yet paid
        eq = eq_at.get(p["entry_day"] - 1) or eq_at.get(p["entry_day"])
        opened.append({
            "symbol": p["sym"], "side": p["side"], "signal_date": iso(p["entry_day"] - 1, data),
            "entry_date": iso(p["entry_day"], data), "entry": r(p["entry"]), "stop0": r(p["stop0"]),
            "stop": r(p["stop"]), "last_date": F["date"][p["last_i"]], "last": r(px), "qty": r(full_qty),
            "qty_open": r(p["qty"]), "partials": [{"date": iso(x["exit_day"], data), "price": r(x["exit"]),
                                                   "share": r(x["qty"] / full_qty, 3), "pnl_usd": r(x["pnl"], 6)} for x in pp],
            "notional": r(notional, 6), "size_pct": r(100 * notional / eq, 4) if eq else None, "leverage": 1,
            "risk_usd": r(p["risk_amt"], 6), "pnl_usd": r(pnl, 6), "pnl_pct": r(100 * pnl / notional, 5),
            "r": r(pnl / p["risk_amt"], 4) if p["risk_amt"] else None, "days": p["bars"],
            "exit_next_open": bool(p["exit_flag"]),
        })
    orders = []
    for o in st["pending"]:
        F = data[o["sym"]]
        i = F["idx"][o["day"]]
        orders.append({"symbol": o["sym"], "side": o["side"], "signal_date": iso(o["day"], data),
                       "ref_close": r(F["c"][i]), "stop_dist": r(o["stop_dist"]),
                       "stop_dist_pct": r(100 * o["stop_dist"] / F["c"][i], 4), "add": bool(o.get("add"))})
    equity = st["curve"][-1][1] if st["curve"] else cfg["start_equity"]
    return cfg, strat, st, {"start_equity": cfg["start_equity"], "equity": r(equity, 8), "open": opened, "closed": closed,
                             "orders": orders, "metrics": rm(S.metrics(st, a, last)),
                             "exposure_pct": r(100 * sum(p["qty_open"] * p["last"] for p in opened) / equity, 4) if equity else 0}


def check_integrity(con, key, paper, stamp):
    """A trade, once recorded, must never change. Records new trades, closes open ones, reports mismatches."""
    rec = {(s, e): (x, xd, xp) for s, e, x, xd, xp in con.execute(
        "SELECT symbol, entry_date, entry, exit_date, exit FROM bot_trades_log WHERE version=?", (key,))}
    mismatches = []
    for t in paper["closed"]:
        k = (t["symbol"], t["entry_date"])
        if k in rec:
            e, xd, xp = rec[k]
            if abs(e - t["entry"]) > 1e-9 * max(1, abs(e)) or (xd and (xd != t["exit_date"] or abs(xp - t["exit"]) > 1e-9 * max(1, abs(xp)))):
                mismatches.append(k)
    for p in paper["open"]:
        k = (p["symbol"], p["entry_date"])
        if k in rec and (abs(rec[k][0] - p["entry"]) > 1e-9 * max(1, abs(rec[k][0])) or rec[k][1] is not None):
            mismatches.append(k)
    rows = [(key, t["symbol"], t["side"], t["entry_date"], t["entry"], t["exit_date"], t["exit"], t["pnl_usd"],
             t["reason"], stamp) for t in paper["closed"]]
    rows += [(key, p["symbol"], p["side"], p["entry_date"], p["entry"], None, None, None, "open", stamp) for p in paper["open"]]
    for row in rows:
        k = (row[1], row[3])
        if k not in rec:
            con.execute("INSERT INTO bot_trades_log VALUES(?,?,?,?,?,?,?,?,?,?)", row)
        elif rec[k][1] is None and row[5] is not None:      # open -> closed
            con.execute("UPDATE bot_trades_log SET exit_date=?, exit=?, pnl_usd=?, reason=? WHERE version=? AND symbol=? AND entry_date=?",
                        (row[5], row[6], row[7], row[8], key, row[1], row[3]))
    return "ok" if not mismatches else "MISMATCH: " + ", ".join(f"{s} {d}" for s, d in mismatches[:5])


def bot_highlights(bot, paper, last, data):
    """Bot actions on the latest day (fills happen at the open of `last`; orders are for the next open)."""
    tag = lambda k: f"{k}:{bot['id']}"
    meta = {"bot": bot["id"], "bot_name": bot["name"], "status": bot["status"]}
    hl = []
    for p in paper["open"]:
        if day_num(p["entry_date"]) == last:
            hl.append((p["symbol"], "1d", p["entry_date"], tag("bot_buy"), "bullish",
                       json.dumps(dict(meta, entry=p["entry"], stop=p["stop0"], size_pct=p["size_pct"]))))
        for x in p["partials"]:
            if day_num(x["date"]) == last:
                hl.append((p["symbol"], "1d", x["date"], tag("bot_partial"), "bullish",
                           json.dumps(dict(meta, price=x["price"], share=x["share"], pnl_usd=x["pnl_usd"]))))
    for t in paper["closed"]:
        if day_num(t["exit_date"]) == last:
            hl.append((t["symbol"], "1d", t["exit_date"], tag("bot_sell"), "bullish" if t["pnl_usd"] > 0 else "bearish",
                       json.dumps(dict(meta, exit=t["exit"], pnl_pct=t["pnl_pct"], r=t["r"], reason=t["reason"]))))
    for o in paper["orders"]:
        hl.append((o["symbol"], "1d", o["signal_date"], tag("bot_signal"), "bullish",
                   json.dumps(dict(meta, ref_close=o["ref_close"], stop_dist_pct=o["stop_dist_pct"]))))
    return hl


def backtest(bot, cfg, strat, data, regime, last):
    b0, b1, b2 = (day_num(x) for x in BACKTEST)
    bt = S.simulate(data, regime, strat, b0, last, cfg)
    bt_eq = dict(bt["curve"])
    m = lambda a_, z: rm(S.metrics(bt, a_, z))
    years = {str(y): m(max(b0, day_num(f"{y}-01-01")), min(last, day_num(f"{y}-12-31")))
             for y in range(int(BACKTEST[0][:4]), int(iso(last, data)[:4]) + 1)}
    parts = partials_by_cid(bt["trades"])
    trades = [trade_row(t, data, bt_eq, parts.get(cid(t), ())) for t in bt["trades"] if not t.get("partial")]
    return bt, {"from": BACKTEST[0], "design_until": BACKTEST[1], "unseen_from": BACKTEST[2], "to": iso(last, data),
                "full": m(b0, last), "design": m(b0, b1), "unseen": m(b2, last), "years": years, "trades": trades}


def weekly_curve(curves, data, b0, last):
    """Weekly points of the summed equity curve(s) plus BTC close."""
    tot = {}
    for c in curves:
        for d, e in c:
            tot[d] = tot.get(d, 0.0) + e
    B = data["BTCUSDT"]
    out = []
    for d in sorted(tot):
        if (d - b0) % 7 == 0 or d == last:
            i = B["idx"].get(d)
            out.append([iso(d, data), r(tot[d], 7), r(B["c"][i], 7) if i is not None else None])
    return out


def paper_daily(curves, data, a, last):
    """Daily paper equity since inception: [date, total, bot 1, bot 2, ..., BTC close] (bots in BOTS order)."""
    per = [dict(c) for c in curves]
    B = data["BTCUSDT"]
    out = []
    for d in range(a, last + 1):
        if not any(d in c for c in per):
            continue
        vals = [c.get(d) for c in per]
        i = B["idx"].get(d)
        out.append([iso(d, data), r(sum(v for v in vals if v is not None), 8)] + [r(v, 8) for v in vals] +
                   [r(B["c"][i], 7) if i is not None else None])
    return out


def top50_list(data, last, held):
    B_ = data["BTCUSDT"]
    bi_ = B_["idx"].get(last)
    btc90 = B_["ret90"][bi_] if bi_ is not None else None
    min_vol = S.DEFAULTS["min_vol"]
    pool = []
    for sym, F in data.items():
        i = F["idx"].get(last)
        if i is None or F["seg_bar"][i] < 200 or (F["vavg20i"][i] or 0) < min_vol:
            continue
        pool.append((F["vavg20i"][i], sym, i))
    pool.sort(reverse=True)
    liq_rank = {sym: k + 1 for k, (_, sym, _) in enumerate(pool)}
    rows = []
    for adv, sym, i in pool:
        F = data[sym]
        parts = score_parts(F, i, btc90)
        ret = lambda n: r(100 * (F["c"][i] / F["c"][i - n] - 1), 5) if F["seg_bar"][i] >= n else None
        rows.append({"symbol": sym, "score": sum(parts), "parts": [1 if x else 0 for x in parts],
                     "liq_rank": liq_rank[sym], "adv_usd": r(adv, 6), "close": r(F["c"][i]),
                     "ret_7d": ret(7), "ret_30d": ret(30), "ret_90d": ret(90),
                     "bot_tradable": liq_rank[sym] <= 10, "bot_holds": sym in held,
                     "breakout_today": bool(F["hh20"][i] is not None and F["c"][i] > F["hh20"][i])})
    rows.sort(key=lambda x: (-x["score"], x["liq_rank"]))
    return {"as_of": iso(last, data), "eligible": len(rows), "min_adv_usd": min_vol, "conditions": SCORE_PARTS,
            "rows": rows[:50]}


def run(con=None, log=print):
    t0 = time.time()
    own = con is None
    if own:
        con = sqlite3.connect(DB, timeout=60)
    assert abs(sum(b["capital"] for b in BOTS) - START_EQUITY) < 1e-6, "bot capital must add up to the portfolio"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data, regime = load(con)
    last = max(F["dn"][-1] for F in data.values())
    last_iso = iso(last, data)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS bot_trades_log(version TEXT, symbol TEXT, side TEXT, entry_date TEXT, entry REAL,
        exit_date TEXT, exit REAL, pnl_usd REAL, reason TEXT, first_recorded_utc TEXT,
        PRIMARY KEY(version, symbol, entry_date));
    CREATE TABLE IF NOT EXISTS bot_runs(run_utc TEXT, version TEXT, data_through TEXT, equity REAL, open_positions INTEGER,
        closed_trades INTEGER, integrity TEXT, seconds REAL);
    """)
    b0 = day_num(BACKTEST[0])
    out_bots, hl, paper_curves, bt_curves, all_bt_trades, all_paper_trades = [], [], [], [], [], []
    ok = True
    for bot in BOTS:
        cfg, strat, st, paper = paper_account(bot, data, regime, last)
        key = f"{bot['id']}:{bot['version']}"
        paper["integrity"] = check_integrity(con, key, paper, stamp)
        ok = ok and paper["integrity"] == "ok"
        hl += bot_highlights(bot, paper, last, data)
        bt, btj = backtest(bot, cfg, strat, data, regime, last)
        btj["curve"] = weekly_curve([bt["curve"]], data, b0, last)
        paper_curves.append(st["curve"])
        bt_curves.append(bt["curve"])
        all_bt_trades += [dict(t, cid=(bot["id"],) + tuple(cid(t))) for t in bt["trades"]]   # keep bots' trades apart
        all_paper_trades += [dict(t, cid=(bot["id"],) + tuple(cid(t))) for t in st["trades"]]
        out_bots.append({"id": bot["id"], "version": bot["version"], "name": bot["name"], "status": bot["status"],
                         "capital": bot["capital"], "summary": bot["summary"], "max_gross": cfg["max_gross"],
                         "strategy": {"rules": bot["rules"] + COMMON_RULES + [bot.get("leverage_rule", NO_LEVERAGE)], "code": bot["strategy"][0],
                                      "params": bot["strategy"][1], "config": {k: cfg[k] for k in sorted(cfg)}},
                         "paper": paper, "backtest": btj})
        con.execute("INSERT INTO bot_runs VALUES(?,?,?,?,?,?,?,?)", (stamp, key, last_iso, paper["equity"], len(paper["open"]),
                                                                       len(paper["closed"]), paper["integrity"], round(time.time() - t0, 1)))
    if regime.get(last) and regime.get(last - 1) and regime[last] != regime[last - 1]:
        hl.append(("BTCUSDT", "1d", last_iso, "btc_regime", "bullish" if regime[last] == 1 else "bearish",
                   json.dumps({"on": regime[last] == 1})))
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='highlight_log'").fetchone():
        con.executemany("INSERT OR IGNORE INTO highlight_log VALUES(?,?,?,?,?,?,?)", [h + (stamp,) for h in hl])

    # ---- portfolio = sum of the bots
    def summed(curves):
        tot = {}
        for c in curves:
            for d, e in c:
                tot[d] = tot.get(d, 0.0) + e
        return sorted(tot.items())
    a = day_num(INCEPTION)
    pf_paper = {"curve": summed(paper_curves), "trades": all_paper_trades}
    pf_bt = {"curve": summed(bt_curves), "trades": all_bt_trades}
    b1, b2 = day_num(BACKTEST[1]), day_num(BACKTEST[2])
    B = data["BTCUSDT"]
    bi0, bi1 = B["idx"][b0], B["idx"][last]
    pk, dd = 0.0, 0.0
    for i in range(bi0, bi1 + 1):
        pk = max(pk, B["c"][i])
        dd = min(dd, B["c"][i] / pk - 1)
    yrs = (last - b0) / 365.25
    bench = {"cagr": r((B["c"][bi1] / B["c"][bi0]) ** (1 / yrs) - 1, 5), "maxdd": r(dd, 5)}
    equity = sum(b["paper"]["equity"] for b in out_bots)
    portfolio = {
        "id": PORTFOLIO, "start_equity": START_EQUITY, "equity": r(equity, 8),
        "open": sum(len(b["paper"]["open"]) for b in out_bots), "closed": sum(len(b["paper"]["closed"]) for b in out_bots),
        "orders": sum(len(b["paper"]["orders"]) for b in out_bots),
        "integrity": "ok" if ok else "; ".join(f"{b['name']}: {b['paper']['integrity']}" for b in out_bots if b["paper"]["integrity"] != "ok"),
        "paper_metrics": rm(S.metrics(pf_paper, a, last)),
        "paper_curve": paper_daily(paper_curves, data, a, last),
        "allocation": [{"id": b["id"], "name": b["name"], "status": b["status"], "capital": b["capital"],
                        "equity": b["paper"]["equity"]} for b in out_bots],
        "backtest": {"from": BACKTEST[0], "design_until": BACKTEST[1], "unseen_from": BACKTEST[2], "to": last_iso,
                     "full": rm(S.metrics(pf_bt, b0, last)), "design": rm(S.metrics(pf_bt, b0, b1)),
                     "unseen": rm(S.metrics(pf_bt, b2, last)), "benchmark_btc": bench,
                     "curve": weekly_curve(bt_curves, data, b0, last),
                     "proven_only": {"unseen": rm(S.metrics({"curve": summed([c for c, b in zip(bt_curves, BOTS) if b["status"] == "proven"]),
                                                             "trades": []}, b2, last))}},
    }
    held = {p["symbol"] for b in out_bots if b["id"] == "leaders" for p in b["paper"]["open"]}
    payload = {
        "generated_utc": stamp, "portfolio_version": PORTFOLIO, "data_through": last_iso, "inception": INCEPTION,
        "regime": {"btc_above_sma200": regime.get(last) == 1, "date": last_iso},
        "portfolio": portfolio, "bots": out_bots, "top50": top50_list(data, last, held),
    }
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, "bot.json.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, separators=(",", ":"), allow_nan=False)
    os.replace(tmp, os.path.join(OUT, "bot.json"))
    con.commit()
    log(f"bots {PORTFOLIO}: data through {last_iso}, portfolio {equity:,.2f} | " +
        " | ".join(f"{b['name']} {b['paper']['equity']:,.0f} ({len(b['paper']['open'])} open, {len(b['paper']['closed'])} closed, "
                   f"{len(b['paper']['orders'])} orders)" for b in out_bots) +
        f" | integrity {portfolio['integrity']}, {time.time() - t0:.0f}s")
    if own:
        con.close()
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
