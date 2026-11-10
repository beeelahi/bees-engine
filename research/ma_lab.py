"""
Moving-average lab (Python 3.9 stdlib only): which moving averages have worked best on the weekly, daily and 4-hour
charts, for the owner's picks and the dashboard's 100 most traded pairs.

    python3 research/ma_lab.py [DB] [OUT]       (server: ops ma-lab, read with ma-lab-log)

Tested: SMA and EMA at 5, 8, 9, 10, 12, 13, 20, 21, 26, 30, 34, 50, 55, 89, 100, 144, 200 (34 averages) on every coin,
on completed candles only (weekly bars are built from daily candles, Monday to Sunday, UTC).

Two jobs an average does, each measured against a fair baseline:

1. REACTION (support in an uptrend, resistance in a downtrend). A test = the last 3 closes were on one side of the
   average and this candle's low (high) comes back to within 0.25 ATR of it, for the first time. It HELD when price
   then moved 1.5 ATR away from the average (high >= MA + 1.5 ATR for support) before a candle CLOSED 0.5 ATR through
   it; it BROKE when that close came first (a close through on the touching candle itself counts as broke; if both
   happen on the same later candle it counts as broke); neither within H candles = not held.
   Baseline: the same rule with the same geometry from EVERY candle in the same window: its low as the level (price
   going 1.5 ATR above the low before a close 0.5 ATR below it), its high for resistance. Edge = held rate minus that base rate, in points. An average with no edge is
   just a line that price crosses; a positive edge means price really reacts to it.

2. TREND. (a) Long-or-flat: hold while the candle closed above the average, flat below; 0.1% cost per switch;
   compared with simply holding over the same window: risk-adjusted return (Sharpe, annualised) minus holding's,
   switches per year. (b) Trend split: the average forward return over H candles when price closed above the average
   minus when it closed below, in % (does "above" really mean the next candles go better?).

Windows: daily and 4h are TESTED on the last 365 days and CHECKED on the 730 days before that; weekly is tested on
the last 3 years (one year is only 52 weekly candles) and checked on the years before. The picks need the check
period to agree (reaction edge and trend split both positive there too).

Top 3 per timeframe: each average is ranked across the 34 on reaction edge (40%), Sharpe advantage (30%) and trend
split (30%), pooled over every coin in the test window; only averages with at least MIN_TESTS tests count, and the
check period must agree; the three are spread fast / medium / slow (each at least 1.5x its neighbour's length, so
they are not three copies of one line); a tie goes to the shorter average (it reacts faster).
Per coin, the same ranking on that coin alone (at least 4 tests) gives its own best three, shown with where price
stands against the timeframe's top three today.

Output: web_export/ma_lab.json (the Moving averages page under Ideas). Research, not advice.
"""
import json
import math
import os
import sqlite3
import statistics as st
import sys
import time
from datetime import datetime, timedelta, timezone

LENGTHS = [5, 8, 9, 10, 12, 13, 20, 21, 26, 30, 34, 50, 55, 89, 100, 144, 200]
KINDS = ["EMA", "SMA"]
PICKS = ["BTC", "ETH", "SOL", "MORPHO", "ONDO", "CAKE", "UNI", "JUP", "PENGU", "FLOKI", "PEPE", "DOGE", "NPC", "TAO",
         "SUI", "APT", "ZIG"]
TOP_N = 100
TF = {   # bars a year, test window (days), check window (days before the test), forward horizon H (bars)
    "1w": {"bars_y": 52.18, "test_d": 3 * 365, "check_d": 3 * 365, "H": 8, "label": "Weekly", "use": "to form your views"},
    "1d": {"bars_y": 365.0, "test_d": 365, "check_d": 730, "H": 15, "label": "Daily", "use": "for the current trend"},
    "4h": {"bars_y": 2190.0, "test_d": 365, "check_d": 730, "H": 18, "label": "4-hour", "use": "for trading ideas"},
}
TOUCH, HOLD_ATR, BREAK_ATR, COST = 0.25, 1.5, 0.5, 0.001
MIN_TESTS = {"1w": 60, "1d": 300, "4h": 600}
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------- indicators
def sma(x, n):
    out, s = [None] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x, n):
    out, a = [None] * len(x), 2 / (n + 1)
    if len(x) < n:
        return out
    e = sum(x[:n]) / n
    out[n - 1] = e
    for i in range(n, len(x)):
        e = a * x[i] + (1 - a) * e
        out[i] = e
    return out


def atr(h, l, c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


# ---------------------------------------------------------------- data
def load(con, sym, tf, now):
    """[(t, o, h, l, c)] completed candles, oldest first; t = ISO date (1d, 1w) or open ms (4h)."""
    if tf == "4h":
        now_ms = int(now.timestamp() * 1000)
        return [r for r in con.execute("SELECT open_ms, open, high, low, close FROM candles_4h WHERE symbol=? AND close_ms < ? ORDER BY open_ms",
                                       (sym, now_ms))]
    today = now.strftime("%Y-%m-%d")
    d = [r for r in con.execute("SELECT date, open, high, low, close FROM daily WHERE symbol=? AND date < ? ORDER BY date", (sym, today))]
    if tf == "1d":
        return d
    wk = {}
    for t, o, h, l, c in d:                                    # Monday-to-Sunday weeks; only complete ones
        dt = datetime.strptime(t, "%Y-%m-%d")
        m = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
        w = wk.get(m)
        wk[m] = [m, o, h, l, c, 1] if w is None else [m, w[1], max(w[2], h), min(w[3], l), c, w[5] + 1]
    last_monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    return [tuple(w[:5]) for m, w in sorted(wk.items()) if m < last_monday and w[5] == 7]


def bar_day(t, tf):
    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d") if tf == "4h" else t


# ---------------------------------------------------------------- one coin, one timeframe, one average
def reaction(o, h, l, c, m, a, i0, i1, H):
    """Tests in [i0, i1): (tests, held, expected_held_by_chance, last_test) using the precomputed base rates."""
    tests = held = 0
    last = None
    out = []
    for k in range(max(i0, 4), i1):
        mk, ak = m[k], a[k - 1]
        if mk is None or m[k - 4] is None or ak is None or ak <= 0:
            continue
        band = TOUCH * ak
        up = all(c[j] > m[j] for j in range(k - 3, k))
        dn = all(c[j] < m[j] for j in range(k - 3, k))
        if up and l[k] <= mk + band and l[k - 1] > m[k - 1] + band:
            side = 1
        elif dn and h[k] >= mk - band and h[k - 1] < m[k - 1] - band:
            side = -1
        else:
            continue
        res = "faded"
        for j in range(k, min(len(c), k + H + 1)):
            if m[j] is None:
                break
            if (side > 0 and c[j] < m[j] - BREAK_ATR * ak) or (side < 0 and c[j] > m[j] + BREAK_ATR * ak):
                res = "broke"
                break
            if j > k and ((side > 0 and h[j] >= mk + HOLD_ATR * ak) or (side < 0 and l[j] <= mk - HOLD_ATR * ak)):
                res = "held"
                break
        tests += 1
        held += res == "held"
        out.append(side)
        last = (k, side, res)
    return tests, held, out, last


def base_rate(h, l, c, a, i0, i1, H):
    """Chance of the same outcome with the same geometry from ANY candle: its low as the 'level' for support (up 1.5 ATR
    from the low before a close 0.5 ATR below it), its high for resistance (the mirror). A test is triggered by the low
    (high) touching the average, so the baseline must start from the low (high) too, not the close."""
    nu = nd = n = 0
    for k in range(max(i0, 1), i1):
        ak = a[k - 1]
        if ak is None or ak <= 0:
            continue
        ru, rd = None, None
        for j in range(k, min(len(c), k + H + 1)):
            if ru is None:
                if c[j] < l[k] - BREAK_ATR * ak:
                    ru = 0
                elif j > k and h[j] >= l[k] + HOLD_ATR * ak:
                    ru = 1
            if rd is None:
                if c[j] > h[k] + BREAK_ATR * ak:
                    rd = 0
                elif j > k and l[j] <= h[k] - HOLD_ATR * ak:
                    rd = 1
            if ru is not None and rd is not None:
                break
        n += 1
        nu += ru or 0
        nd += rd or 0
    return (nu / n, nd / n) if n else (None, None)


def trend(c, m, i0, i1, H, bars_y):
    """Long-or-flat vs holding over [i0, i1): (sharpe_strategy, sharpe_hold, return_strategy, return_hold, switches/yr,
    split = mean H-bar forward return above minus below)."""
    rs, rh, pos, sw = [], [], None, 0
    fa, fb = [], []
    for k in range(i0, i1 - 1):
        if m[k] is None:
            continue
        p = 1 if c[k] > m[k] else 0
        r = c[k + 1] / c[k] - 1
        cost = COST if pos is not None and p != pos else 0.0
        if pos is not None and p != pos:
            sw += 1
        pos = p
        rs.append(p * r - cost)
        rh.append(r)
        if k + H < len(c):
            (fa if p else fb).append(c[k + H] / c[k] - 1)

    def sharpe(x):
        if len(x) < 20:
            return None
        sd = st.pstdev(x)
        return st.mean(x) / sd * math.sqrt(bars_y) if sd > 0 else None

    def tot(x):
        g = 1.0
        for v in x:
            g *= 1 + v
        return g - 1
    yrs = len(rs) / bars_y if rs else 0
    split = 100 * (st.mean(fa) - st.mean(fb)) if fa and fb else None
    return sharpe(rs), sharpe(rh), tot(rs), tot(rh), (sw / yrs if yrs else None), split


def analyse(bars, tf, now):
    cfg = TF[tf]
    t = [b[0] for b in bars]
    o, h, l, c = ([b[i] for b in bars] for i in (1, 2, 3, 4))
    a = atr(h, l, c)
    end_day = now.strftime("%Y-%m-%d")
    t0 = (now - timedelta(days=cfg["test_d"])).strftime("%Y-%m-%d")
    t1 = (now - timedelta(days=cfg["test_d"] + cfg["check_d"])).strftime("%Y-%m-%d")
    days = [bar_day(x, tf) for x in t]
    i_test = next((i for i, d in enumerate(days) if d >= t0), len(c))
    i_chk = next((i for i, d in enumerate(days) if d >= t1), len(c))
    win = {"test": (i_test, len(c)), "check": (i_chk, i_test)}
    base = {w: base_rate(h, l, c, a, i0, i1, cfg["H"]) for w, (i0, i1) in win.items()}
    res = {}
    for kind in KINDS:
        for n in LENGTHS:
            m = (ema if kind == "EMA" else sma)(c, n)
            row = {}
            for w, (i0, i1) in win.items():
                if i1 - i0 < 30 or base[w][0] is None:
                    continue
                tests, held, sides, last = reaction(o, h, l, c, m, a, i0, i1, cfg["H"])
                exp = sum(base[w][0] if s > 0 else base[w][1] for s in sides)
                ss, sh, rs, rh, swy, split = trend(c, m, i0, i1, cfg["H"], cfg["bars_y"])
                row[w] = {"tests": tests, "held": held, "exp": exp, "ss": ss, "sh": sh, "rs": rs, "rh": rh, "swy": swy, "split": split,
                          "last": (days[last[0]], "support" if last[1] > 0 else "resistance", last[2]) if last and w == "test" else None}
            if m[-1] is not None and a[-1]:
                k = len(c) - 1
                row["now"] = {"ma": m[k], "dist": 100 * (c[k] / m[k] - 1), "atr_dist": (c[k] - m[k]) / a[k],
                              "slope": 100 * (m[k] / m[k - 5] - 1) if k >= 5 and m[k - 5] else None}
            res[f"{kind}{n}"] = row
    return res, {"close": c[-1] if c else None, "last_bar": days[-1] if days else None, "bars": len(c)}


# ---------------------------------------------------------------- pooling and ranking
def pool(per_coin, ma, w):
    T = Hd = E = 0
    adv, spl, sws, beat = [], [], [], 0
    nco = 0
    for res in per_coin.values():
        r = res.get(ma, {}).get(w)
        if not r:
            continue
        nco += 1
        T, Hd, E = T + r["tests"], Hd + r["held"], E + r["exp"]
        if r["ss"] is not None and r["sh"] is not None:
            adv.append(r["ss"] - r["sh"])
            beat += r["ss"] > r["sh"]
        if r["split"] is not None:
            spl.append(r["split"])
        if r["swy"] is not None:
            sws.append(r["swy"])
    med = lambda x: st.median(x) if x else None  # noqa: E731
    return {"coins": nco, "tests": T, "held_pct": 100 * Hd / T if T else None, "base_pct": 100 * E / T if T else None,
            "edge": 100 * (Hd - E) / T if T else None, "sharpe_adv": med(adv), "beat_hold_pct": 100 * beat / len(adv) if adv else None,
            "split": med(spl), "switches_y": med(sws)}


def rank_pick(rows, min_tests, need_check=True):
    """rows: [(ma, test_stats, check_stats)] -> rows sorted by composite, each with 'score' and 'passes'."""
    ok = [r for r in rows if r[1]["tests"] >= min_tests and r[1]["edge"] is not None and r[1]["sharpe_adv"] is not None and r[1]["split"] is not None]

    def pct_rank(key):
        v = sorted(x[1][key] for x in ok)
        return {x[0]: (v.index(x[1][key]) / (len(v) - 1) if len(v) > 1 else 1.0) for x in ok}
    re_, sa, sp = pct_rank("edge"), pct_rank("sharpe_adv"), pct_rank("split")
    out = []
    for ma, ts, cs in ok:
        score = 0.4 * re_[ma] + 0.3 * sa[ma] + 0.3 * sp[ma]
        passes = ts["edge"] > 0 and ts["split"] > 0 and (not need_check or (cs and (cs.get("edge") or -1) > 0 and (cs.get("split") or -1) > 0))
        out.append({"ma": ma, "score": round(100 * score, 1), "passes": bool(passes)})
    n_of = lambda m: int("".join(ch for ch in m if ch.isdigit()))  # noqa: E731
    out.sort(key=lambda x: (-x["score"], n_of(x["ma"])))
    return out


SPREAD = 1.5      # the three picks are fast / medium / slow: each at least 1.5x the length of its neighbour


def top3(ranked):
    n_of = lambda m: int("".join(ch for ch in m if ch.isdigit()))  # noqa: E731
    pick, used = [], set()
    for r in [x for x in ranked if x["passes"]] + [x for x in ranked if not x["passes"]]:
        n = n_of(r["ma"])
        if any(max(n, u) / min(n, u) < SPREAD for u in used):
            continue
        pick.append(r)
        used.add(n)
        if len(pick) == 3:
            break
    return pick


def r2(x, n=2):
    return None if x is None else round(x, n)


# ---------------------------------------------------------------- main
def universe(con, now):
    since = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    vol = dict(con.execute("SELECT symbol, AVG(volume_usdt) FROM daily WHERE date >= ? GROUP BY symbol", (since,)))
    uni = [s for (s,) in con.execute("SELECT symbol FROM latest WHERE in_universe=1")]
    ranked = sorted((s for s in uni if vol.get(s)), key=lambda s: -vol[s])
    top = ranked[:TOP_N]
    allsyms = {s for (s,) in con.execute("SELECT symbol FROM pairs")}
    picks, missing = [], []
    for p in PICKS:
        s = p + "USDT"
        (picks if s in allsyms and con.execute("SELECT 1 FROM daily WHERE symbol=? LIMIT 1", (s,)).fetchone() else missing).append(s if s in allsyms else p)
    order = picks + [s for s in top if s not in picks]
    rank = {s: i + 1 for i, s in enumerate(ranked)}
    return order, set(picks), missing, rank


def main(db, out_path, log=print):
    t_start = time.time()
    now = datetime.now(timezone.utc)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=120)
    syms, picks, missing, liq = universe(con, now)
    log(f"ma lab: {len(syms)} coins ({len(picks)} picks, {len(syms) - len(picks)} from the top {TOP_N}); picks without data: {missing or 'none'}")
    out = {"generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "method": {
        "averages": [f"{k}{n}" for k in KINDS for n in LENGTHS], "touch_atr": TOUCH, "held_atr": HOLD_ATR, "break_atr": BREAK_ATR, "cost": COST,
        "windows": {tf: {"test_days": c["test_d"], "check_days": c["check_d"], "horizon_bars": c["H"]} for tf, c in TF.items()},
        "min_tests": MIN_TESTS}, "picks": sorted(picks), "picks_missing": missing, "timeframes": {}, "coins": []}
    per_tf = {}
    for tf in TF:
        per_coin, meta = {}, {}
        for s in syms:
            bars = load(con, s, tf, now)
            if len(bars) < 60:
                continue
            per_coin[s], meta[s] = analyse(bars, tf, now)
        per_tf[tf] = (per_coin, meta)
        mas = out["method"]["averages"]
        rows = [(ma, pool(per_coin, ma, "test"), pool(per_coin, ma, "check")) for ma in mas]
        ranked = rank_pick(rows, MIN_TESTS[tf])
        best = top3(ranked)
        sc = {r["ma"]: r for r in ranked}
        table = []
        for ma, ts, cs in rows:
            table.append({"ma": ma, "score": sc.get(ma, {}).get("score"), "passes": sc.get(ma, {}).get("passes", False),
                          "test": {k: r2(v, 3) if isinstance(v, float) else v for k, v in ts.items()},
                          "check": {k: r2(v, 3) if isinstance(v, float) else v for k, v in cs.items()}})
        table.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
        out["timeframes"][tf] = {"label": TF[tf]["label"], "use": TF[tf]["use"], "top3": [b["ma"] for b in best],
                                 "coins": len(per_coin), "table": table}
        log(f"ma lab {tf}: {len(per_coin)} coins; top 3 {[b['ma'] for b in best]} "
            + "; ".join(f"{r['ma']} edge {r['test']['edge']} split {r['test']['split']} sharpe+ {r['test']['sharpe_adv']} tests {r['test']['tests']}"
                        for r in table[:5]) + f" ({time.time() - t_start:.0f}s)")
    for s in syms:
        cdata = {"symbol": s, "pick": s in picks, "liq_rank": liq.get(s), "tf": {}}
        for tf in TF:
            per_coin, meta = per_tf[tf]
            if s not in per_coin:
                continue
            res = per_coin[s]
            rows = [(ma, {"tests": r["test"]["tests"], "edge": 100 * (r["test"]["held"] - r["test"]["exp"]) / r["test"]["tests"] if r["test"]["tests"] else None,
                          "held_pct": 100 * r["test"]["held"] / r["test"]["tests"] if r["test"]["tests"] else None,
                          "sharpe_adv": (r["test"]["ss"] - r["test"]["sh"]) if r["test"]["ss"] is not None and r["test"]["sh"] is not None else None,
                          "split": r["test"]["split"]}, None)
                    for ma, r in res.items() if "test" in r]
            own = top3(rank_pick(rows, 4, need_check=False))
            own_rows = []
            for x in own:
                tr = res[x["ma"]]["test"]
                own_rows.append([x["ma"], tr["tests"], r2(100 * tr["held"] / tr["tests"] if tr["tests"] else None, 0),
                                 r2(100 * (tr["held"] - tr["exp"]) / tr["tests"] if tr["tests"] else None, 0),
                                 r2(tr["ss"] - tr["sh"] if tr["ss"] is not None and tr["sh"] is not None else None, 2), r2(tr["split"], 2)])
            state = []
            for ma in out["timeframes"][tf]["top3"]:
                nw, tr = res.get(ma, {}).get("now"), res.get(ma, {}).get("test")
                if nw:
                    state.append([ma, r2(nw["ma"], 8), r2(nw["dist"], 2), r2(nw["atr_dist"], 2), r2(nw["slope"], 2), tr["last"] if tr else None])
            cdata["tf"][tf] = {"best": own_rows, "state": state, "close": meta[s]["close"], "last_bar": meta[s]["last_bar"]}
        if cdata["tf"]:
            out["coins"].append(cdata)
    near = []                                                  # testing a top-3 average right now (within 0.5 ATR)
    for cd in out["coins"]:
        for tf, d in cd["tf"].items():
            for ma, val, dist, ad, slope, last in d["state"]:
                if ad is not None and abs(ad) <= 0.5:
                    near.append([cd["symbol"], tf, ma, dist, ad, slope])
    out["testing_now"] = sorted(near, key=lambda x: (x[1] != "4h", abs(x[4])))
    out["runtime_s"] = round(time.time() - t_start)
    con.close()
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(tmp, out_path)
    log(f"ma lab: wrote {out_path} ({os.path.getsize(out_path) // 1024} KB) in {out['runtime_s']}s; {len(near)} coin-timeframes testing a top average now")
    return out


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "binance.db")
    outp = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "web_export", "ma_lab.json")
    main(db, outp)
