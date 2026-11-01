"""
Strategy lab (Bilal, 1 Nov 2026): the published strategy library at quants.md (QuantJourney, Apache-2.0) was tested on
US equity ETFs only. The ones with an edge there that can run on crypto are rebuilt here and tested on our own daily
Binance history, long or cash only (no leverage, no shorting), against simply holding.

  universe   BTC alone, and a basket of 10 large coins (each joins once it has 200 days of history; the list is
             today's large coins, so the basket results carry survivorship bias: read them as optimistic)
  periods    2018 to now; split up to 2021 (the strategies' rules were not fitted on crypto at all) and 2022 on
  trading    decide on the daily close, trade at the next close, 0.10% cost a side on every change of weight
  verdict    "works" only when the strategy beats holding on return per unit of risk (Sharpe) by 0.05+ in BOTH
             periods and has a smaller worst drawdown; "defensive" when its worst drawdown is 15+ points smaller and
             it beat holding (Sharpe) from 2022 on; else "no edge"

    python3 research/strategy_lab.py [DB]       (server: ops strategy-lab, read with strategy-lab-log)
Writes web_export/strategies.json (published with the site) and data/strategy_lab.json.
"""
import json
import math
import os
import sqlite3
import statistics as st
import sys
import time

import _env  # noqa: F401
import diverge as DV

COST, SPLIT, START = 0.001, "2022-01-01", "2018-01-01"
BASKET = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "LTCUSDT"]


def sma(x, n, k):
    return sum(x[k - n + 1:k + 1]) / n if k >= n - 1 else None


def vol(r, k, n=63):
    w = [v for v in r[max(0, k - n + 1):k + 1] if v is not None]
    return st.pstdev(w) * math.sqrt(365) if len(w) >= 20 else None


# ---------------------------------------------------------------- strategies: f(ctx, k) -> {symbol: weight}, decided at close k
def s_hold_btc(c, k):
    return {"BTCUSDT": 1.0}


def s_hold_basket(c, k):
    av = c.avail(k)
    return {s: 1 / len(av) for s in av} if av else {}


def trend_cross(fast, slow):
    def f(c, k):
        x = c.px["BTCUSDT"]
        a, b = sma(x, fast, k), sma(x, slow, k)
        return {"BTCUSDT": 1.0} if a and b and a > b else {}
    return f


def s_macd(c, k):
    m, sg = c.macd["BTCUSDT"]
    return {"BTCUSDT": 1.0} if m[k] is not None and sg[k] is not None and m[k] > sg[k] else {}


def s_rsi_rev(c, k):
    r = c.rsi["BTCUSDT"][k]
    held = c.state.get("rsi", False)
    if r is not None:
        if not held and r < 35:
            held = True
        elif held and r > 60:
            held = False
    c.state["rsi"] = held
    return {"BTCUSDT": 1.0} if held else {}


def s_boll(c, k):
    x = c.px["BTCUSDT"]
    if k < 20:
        return {}
    w = x[k - 19:k + 1]
    m, sd = sum(w) / 20, st.pstdev(w)
    held = c.state.get("bb", False)
    if not held and x[k] < m - 2 * sd:
        held = True
    elif held and x[k] > m:
        held = False
    c.state["bb"] = held
    return {"BTCUSDT": 1.0} if held else {}


def s_defensive(c, k):
    x = c.px["BTCUSDT"]
    a = sma(x, 200, k)
    return s_hold_basket(c, k) if a and x[k] > a else {}


def monthly(f):
    def g(c, k):
        if k == 0 or c.dates[k][:7] != c.dates[k - 1][:7] or "m:" + f.__name__ not in c.state:
            c.state["m:" + f.__name__] = f(c, k)
        return c.state["m:" + f.__name__]
    g.__name__ = f.__name__
    return g


def mom(c, k, s, n=90):
    x = c.px[s]
    return x[k] / x[k - n] - 1 if k >= n and x[k - n] else None


def s_dual_momentum(c, k):
    sc = sorted(((mom(c, k, s), s) for s in c.avail(k) if mom(c, k, s) is not None), reverse=True)
    top = [s for m, s in sc[:3] if m > 0]
    return {s: 1 / len(top) for s in top} if top else {}


def s_inverse_vol(c, k, cap=None):
    v = {s: vol(c.ret[s], k) for s in c.avail(k)}
    v = {s: x for s, x in v.items() if x}
    if not v:
        return {}
    w = {s: 1 / x for s, x in v.items()}
    t = sum(w.values())
    w = {s: x / t for s, x in w.items()}
    if cap:
        for _ in range(10):
            over = {s: x - cap for s, x in w.items() if x > cap}
            if not over:
                break
            free = {s: x for s, x in w.items() if x < cap}
            ex = sum(over.values())
            w = {s: (cap if s in over else x + ex * x / sum(free.values())) for s, x in w.items()} if free else w
    return w


def s_risk_parity_capped(c, k):
    return s_inverse_vol(c, k, cap=0.25)


def scale_to(c, k, w, target):
    if not w:
        return w
    rs = [sum(w[s] * (c.ret[s][j] or 0) for s in w) for j in range(max(1, k - 62), k + 1)]
    v = st.pstdev(rs) * math.sqrt(365) if len(rs) > 20 else None
    f = min(1.0, target / v) if v else 1.0
    return {s: x * f for s, x in w.items()}


def s_vol_target_trend(c, k):
    up = [s for s in c.avail(k) if sma(c.px[s], 100, k) and c.px[s][k] > sma(c.px[s], 100, k)]
    return scale_to(c, k, {s: 1 / len(up) for s in up}, 0.40) if up else {}


def s_vol_target_momentum(c, k):
    return scale_to(c, k, s_dual_momentum(c, k), 0.50)


STRATS = [  # (id, name, quants.md source, universe, how it trades, function)
    ("hold_btc", "Hold BTC", None, "BTC", "Buy and hold Bitcoin (benchmark)", s_hold_btc),
    ("hold_basket", "Hold the 10-coin basket", None, "Basket", "Equal weights, rebalanced monthly (benchmark)", monthly(s_hold_basket)),
    ("trend_50_200", "SMA 50/200 trend", "W01 Daily SMA Trend", "BTC", "Hold BTC while its 50-day average is above its 200-day average, else cash", trend_cross(50, 200)),
    ("trend_20_50", "SMA 20/50 cross", "O01 / O18 SMA cross", "BTC", "Hold BTC while its 20-day average is above its 50-day average, else cash", trend_cross(20, 50)),
    ("macd", "MACD trend", "W22 MACD Trend", "BTC", "Hold BTC while MACD is above its signal line, else cash", s_macd),
    ("rsi_rev", "RSI reversion", "W03 RSI Reversion", "BTC", "Buy BTC when RSI(14) falls under 35, sell when it rises over 60", s_rsi_rev),
    ("bollinger", "Bollinger reversion", "W21 Bollinger Reversion", "BTC", "Buy BTC on a close under the lower band (20, 2), sell back at the middle band", s_boll),
    ("defensive", "Defensive switch", "W06 Signal Change Defensive", "Basket", "Hold the basket while BTC is above its 200-day average, else cash", s_defensive),
    ("dual_mom", "Dual momentum", "O17 / W04 momentum rotation", "Basket", "Each month, hold the 3 coins with the best 90-day return, only if positive; else cash", monthly(s_dual_momentum)),
    ("inv_vol", "Inverse volatility", "W05 Monthly Inverse Vol", "Basket", "Each month, weight the basket by 1 / 63-day volatility", monthly(s_inverse_vol)),
    ("risk_parity", "Risk parity, capped", "W20 Risk Parity Capped", "Basket", "Inverse-volatility weights with a 25% cap per coin, monthly", monthly(s_risk_parity_capped)),
    ("vt_trend", "Volatility-targeted trend", "W17 Vol Target Trend", "Basket", "Coins above their 100-day average, scaled to 40% yearly volatility", s_vol_target_trend),
    ("vt_mom", "Volatility-targeted momentum", "W18 Vol Target Momentum", "Basket", "Monthly top-3 momentum, scaled daily to 50% yearly volatility", monthly(s_vol_target_momentum)),
]


class Ctx:
    def __init__(self, dates, px):
        self.dates, self.px, self.state = dates, px, {}
        self.ret = {s: [None] + [(x[k] / x[k - 1] - 1) if x[k] and x[k - 1] else None for k in range(1, len(x))] for s, x in px.items()}
        self.first = {s: next((k for k, v in enumerate(x) if v), len(x)) for s, x in px.items()}
        self.rsi = {s: DV.rsi([v or 0 for v in x]) for s, x in px.items()}
        self.macd = {s: DV.macd([v or 0 for v in x])[:2] for s, x in px.items()}

    def avail(self, k):
        return [s for s in self.px if k - self.first[s] >= 200 and self.px[s][k]]


def run(c, f, k0):
    eq, w, out, inmkt, trades = 1.0, {}, [], 0, 0
    for k in range(k0, len(c.dates) - 1):
        nw = {s: x for s, x in f(c, k).items() if x > 1e-9}
        turn = sum(abs(nw.get(s, 0) - w.get(s, 0)) for s in set(nw) | set(w))
        if turn > 1e-9:
            trades += 1
        eq *= 1 - COST * turn
        w = nw
        r = sum(x * (c.ret[s][k + 1] or 0) for s, x in w.items())
        eq *= 1 + r
        inmkt += 1 if w else 0
        out.append((c.dates[k + 1], eq, r))
        # weights drift with prices until the next decision
        tot = sum(x * (1 + (c.ret[s][k + 1] or 0)) for s, x in w.items()) + (1 - sum(w.values()))
        w = {s: x * (1 + (c.ret[s][k + 1] or 0)) / tot for s, x in w.items()} if tot else w
    return out, inmkt, trades


def metrics(rows):
    if len(rows) < 60:
        return None
    e0, e1 = rows[0][1] / (1 + rows[0][2]), rows[-1][1]
    yrs = len(rows) / 365
    r = [x[2] for x in rows]
    sd = st.pstdev(r)
    peak, dd = 0, 0
    for _, e, _ in rows:
        peak = max(peak, e)
        dd = min(dd, e / peak - 1)
    return {"cagr": round(100 * ((e1 / e0) ** (1 / yrs) - 1), 1), "vol": round(100 * sd * math.sqrt(365), 1),
            "sharpe": round(st.mean(r) / sd * math.sqrt(365), 2) if sd else None, "max_dd": round(100 * dd, 1),
            "total": round(100 * (e1 / e0 - 1), 1), "days": len(rows)}


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_env.HERE, "..", "data", "binance.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    t0 = time.time()
    dates = [d for (d,) in con.execute("SELECT DISTINCT date FROM daily WHERE symbol='BTCUSDT' AND date >= ? ORDER BY date", ("2017-06-01",))]
    idx = {d: k for k, d in enumerate(dates)}
    px = {}
    for s in BASKET:
        x = [None] * len(dates)
        for d, cl in con.execute("SELECT date, close FROM daily WHERE symbol=? ORDER BY date", (s,)):
            if d in idx:
                x[idx[d]] = cl
        px[s] = x
    c = Ctx(dates, px)
    k0 = next(k for k, d in enumerate(dates) if d >= START)
    res = {}
    for sid, name, src, uni, how, f in STRATS:
        c.state = {}
        rows, inmkt, trades = run(c, f, k0)
        res[sid] = {"id": sid, "name": name, "source": src, "universe": uni, "rules": how,
                    "full": metrics(rows), "pre": metrics([x for x in rows if x[0] < SPLIT]), "oos": metrics([x for x in rows if x[0] >= SPLIT]),
                    "in_market_pct": round(100 * inmkt / max(1, len(rows)), 0), "trades_per_year": round(trades / (len(rows) / 365), 1),
                    "curve": [[d, round(e, 4)] for d, e, _ in rows[::7]] + [[rows[-1][0], round(rows[-1][1], 4)]]}
    for sid, r in res.items():
        b = res["hold_btc" if r["universe"] == "BTC" else "hold_basket"]
        r["benchmark"] = b["id"]
        if sid.startswith("hold_"):
            r["verdict"] = "benchmark"
            continue
        p, o, bp, bo = r["pre"], r["oos"], b["pre"], b["oos"]
        better = all(x and y and x["sharpe"] is not None and y["sharpe"] is not None and x["sharpe"] >= y["sharpe"] + 0.05 for x, y in ((p, bp), (o, bo)))
        safer = r["full"]["max_dd"] > b["full"]["max_dd"]
        calmer = r["full"]["max_dd"] >= b["full"]["max_dd"] + 15 and o and bo and o["sharpe"] is not None and o["sharpe"] > bo["sharpe"]
        r["verdict"] = "works" if better and safer else "defensive" if calmer else "no edge"
    out = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "from": dates[k0], "to": dates[-1], "split": SPLIT, "cost_pct": 100 * COST,
           "basket": BASKET, "source": "Strategy rules from quants.md (QuantJourney, Apache-2.0), rebuilt for crypto and tested on Binance daily data",
           "strategies": list(res.values())}
    paths = sys.argv[2:3] or (os.path.join(_env.HERE, "..", "data", "strategy_lab.json"), os.path.join(_env.HERE, "..", "web_export", "strategies.json"))
    for path in paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as fh:
            json.dump(out, fh, separators=(",", ":"))
        os.replace(path + ".tmp", path)
    print(f"# strategy lab {out['from']} to {out['to']}, split {SPLIT}, cost {100 * COST:.2f}% a side ({time.time() - t0:.0f}s)")
    print(f"{'strategy':30s} {'univ':6s} | {'CAGR':>6s} {'Sharpe':>6s} {'maxDD':>6s} | pre: {'CAGR':>6s} {'Sh':>5s} | 2022+: {'CAGR':>6s} {'Sh':>5s} {'DD':>6s} | in mkt  verdict")
    for r in res.values():
        f_, p, o = r["full"], r["pre"] or {}, r["oos"] or {}
        print(f"{r['name']:30s} {r['universe']:6s} | {f_['cagr']:6.1f} {f_['sharpe']:6.2f} {f_['max_dd']:6.1f} | pre: {p.get('cagr', 0):6.1f} {p.get('sharpe', 0):5.2f} | "
              f"2022+: {o.get('cagr', 0):6.1f} {o.get('sharpe', 0):5.2f} {o.get('max_dd', 0):6.1f} | {r['in_market_pct']:4.0f}%  {r['verdict']}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
