#!/usr/bin/env python3
"""
Builds the online app's data package from binance.db  ->  ~/BinanceData/web_export/

  overview.json     freshness, coverage, health (runs, audits), key numbers, dictionary
  screener.json     `latest` rows for the filtered universe (every metric; cold values are null)
  highlights.json   KEY highlights only (see key_highlights), last 90 days; the full log stays in highlight_log
  listings.json     new listings / stopped trading (last 180 days), universe enter/leave log
  weekly.json       weekly_report for the last closed week
  watch.json        watchlist hits, last 365 days
  signals.json      warm events: 1d last 30 days, 1w and 1M last 26 weeks
  pairs/index.json  symbol -> shard file
  pairs/sNN.json    per pair: 1d last 730 days, 1w last 156 weeks, 1M all months
                    columns: t o h l c v e21 e50 e200 rsi macd sig rv bbu bbl st sa sb x
                    x = bit flags: 1 complete, 2 ema21 warm, 4 ema50 warm, 8 ema200 warm, 16 rsi warm,
                        32 macd warm, 64 supertrend warm, 128 cloud warm
Numbers are rounded to 7 significant digits for display (the database keeps full precision).
"""
import json, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signals_lib
import analytics
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(ROOT, "data", "binance.db")
OUT = os.path.join(ROOT, "web_export")
SHARDS = 24
DAILY_DAYS, WEEKS = 730, 156


def r(x, sig=7):
    if x is None or isinstance(x, (int, str)):
        return x
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return float(f"{x:.{sig}g}")


def table(con, sql, args=()):
    cur = con.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return {"columns": cols, "rows": [[r(v) for v in row] for row in cur]}


def exists(con, name):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"), allow_nan=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- key highlights (what the dashboard shows)
KEY_TOP_LIQUID = 30        # only the 30 most liquid pairs (20-day average volume) on that day
KEY_MIN_N, KEY_MIN_SPLIT, KEY_MIN_EDGE = 100, 30, 3.0   # evidence: cases, cases per period, |30-day edge| in %
KEY_PER_DAY = 5            # market events per day (bot and BTC-filter events are not capped)
# Only major, tradable trend events qualify (short-term oscillator flips never do).
KEY_MAJOR = {"ema50_x_ema200", "sma50_x_sma200", "reclaim_sma200", "reclaim_ema200", "lose_sma200", "lose_ema200",
             "new_ath", "new_1y_high", "new_1y_low", "price_x_dc55", "squeeze_breakout"}
KEY_NEED_VOLUME = {"new_ath", "new_1y_high", "new_1y_low", "price_x_dc55", "squeeze_breakout",
                   "reclaim_sma200", "reclaim_ema200", "lose_sma200", "lose_ema200"}   # crosses of slow averages excepted
KEY_MIN_HISTORY = 365      # days of continuous history on the pair (evidence from crypto history must apply)
KEY_EXCLUDE = {"PAXG", "XAUT", "WBTC", "WBETH", "BETH", "BNSOL", "WSTETH", "STETH"}   # pegged / wrapped assets
KEY_REPEAT_DAYS = 10       # the same signal on the same pair within this many days is shown once
ALWAYS = {"bot_buy": 400, "bot_sell": 400, "bot_partial": 380, "bot_signal": 350, "btc_regime": 500}
SIGNAL_KINDS = set().union(*analytics.HIGHLIGHT_KINDS.values())


def key_highlights(con, days=90):
    """Curate highlight_log into the few items worth attention. Everything stays in highlight_log.
    A market signal is shown only if (1) the pair was among the KEY_TOP_LIQUID most liquid that day,
    (2) the signal has a proven, consistent 30-day edge (>= KEY_MIN_N cases, >= KEY_MIN_SPLIT before and after
    2022-01-01, same sign overall/early/late, |edge| >= KEY_MIN_EDGE %), using the volume-confirmed record when
    the candle was volume-confirmed; (3) it is the strongest such signal for that pair that day (others listed
    as `also`); (4) it ranks in the day's top KEY_PER_DAY by |edge|."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = con.execute("""SELECT first_seen_utc, symbol, tf, period_start, kind, direction, detail FROM highlight_log
                         WHERE first_seen_utc >= ? ORDER BY first_seen_utc""", (since,)).fetchall()
    stats = {}
    if exists(con, "signal_stats"):
        for sig, tf, d, var, n, w30, e30, ee, el, ne, nl in con.execute(
                "SELECT signal, tf, direction, variant, n, win_30d, edge_30d, edge_30d_early, edge_30d_late, n_early, n_late"
                " FROM signal_stats WHERE n IS NOT NULL"):
            ok = (n >= KEY_MIN_N and (ne or 0) >= KEY_MIN_SPLIT and (nl or 0) >= KEY_MIN_SPLIT and e30 is not None
                  and ee is not None and el is not None and abs(e30) >= KEY_MIN_EDGE and e30 * ee > 0 and e30 * el > 0)
            stats[(sig, tf, d, var)] = (ok, n, w30, e30)
    # liquidity rank per day: 20-day average USDT volume, all pairs with daily data
    dates = sorted({r[3] for r in raw})
    rank = {}
    if dates:
        lo = (datetime.strptime(dates[0], "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
        series = {}
        for s, d, v in con.execute("SELECT symbol, date, volume_usdt FROM daily WHERE date >= ? ORDER BY symbol, date", (lo,)):
            series.setdefault(s, []).append((d, v or 0.0))
        want = set(dates)
        per_day = {}
        for s, xs in series.items():
            for i, (d, _) in enumerate(xs):
                if d in want and i >= 19:
                    per_day.setdefault(d, []).append((sum(v for _, v in xs[i - 19:i + 1]) / 20, s))
        for d, lst in per_day.items():
            lst.sort(reverse=True)
            rank[d] = {s: k + 1 for k, (_, s) in enumerate(lst)}
    universe = {s for (s,) in con.execute("SELECT symbol FROM v_universe")}
    token = dict(con.execute("SELECT symbol, token FROM pairs"))
    from binance_sync import STABLE_BASES
    excluded = KEY_EXCLUDE | set(STABLE_BASES)
    last_shown = {}
    start = dict(con.execute("SELECT symbol, series_start FROM series_info")) if exists(con, "series_info") else {}
    try:
        from bot import BOTS as _BOTS
        active_bots = {x["id"] for x in _BOTS}
    except Exception:
        active_bots = None
    best = {}      # (symbol, period_start) -> chosen item
    also = {}
    for seen, sym, tf, ps, kind, d, detail in raw:
        try:
            det = json.loads(detail) if detail else {}
        except ValueError:
            det = {}
        base_kind = kind.split(":")[0]          # bot actions are tagged per bot: "bot_buy:leaders"
        if ":" in kind and active_bots is not None and kind.split(":", 1)[1] not in active_bots:
            continue                                # a retired bot's actions stay in highlight_log only
        if base_kind in ALWAYS:
            item = dict(first_seen_utc=seen, symbol=sym, tf=tf, period_start=ps, kind=base_kind, direction=d,
                        detail=detail, score=ALWAYS[base_kind], edge_30d=None, n=None, win_30d=None, variant=None,
                        liq_rank=rank.get(ps, {}).get(sym))
            best[(sym, ps, kind)] = item          # never merged with market signals
            continue
        if kind not in SIGNAL_KINDS or kind not in KEY_MAJOR:   # everything else stays in the Signals tab
            continue
        if kind in KEY_NEED_VOLUME and det.get("confirmed") != 1:
            continue
        lr = rank.get(ps, {}).get(sym)
        if lr is None or lr > KEY_TOP_LIQUID or token.get(sym) in excluded:
            continue
        prev = last_shown.get((sym, kind, d))
        if prev and (datetime.strptime(ps, "%Y-%m-%d") - datetime.strptime(prev, "%Y-%m-%d")).days <= KEY_REPEAT_DAYS:
            continue
        ss = start.get(sym)
        if not ss or (datetime.strptime(ps, "%Y-%m-%d") - datetime.strptime(ss, "%Y-%m-%d")).days < KEY_MIN_HISTORY:
            continue
        ev, var = None, "all"
        if det.get("confirmed") == 1:
            ev, var = stats.get((kind, tf, d, "confirmed")), "confirmed"
        if not ev or not ev[0]:
            ev, var = stats.get((kind, tf, d, "all")), "all"
        if not ev or not ev[0]:
            continue
        ok, n, w30, e30 = ev
        last_shown[(sym, kind, d)] = ps
        item = dict(first_seen_utc=seen, symbol=sym, tf=tf, period_start=ps, kind=kind, signal_direction=d,
                    direction="bullish" if e30 > 0 else "bearish", detail=detail, score=abs(e30), edge_30d=e30,
                    n=n, win_30d=w30, variant=var, liq_rank=lr)
        k = (sym, ps, "market")
        if k not in best or item["score"] > best[k]["score"]:
            if k in best:
                also.setdefault(k, []).append(best[k]["kind"])
            best[k] = item
        else:
            also.setdefault(k, []).append(kind)
    out = []
    by_day = {}
    for k, it in best.items():
        it["also"] = sorted(set(also.get(k, [])) - {it["kind"]})
        it["in_universe"] = 1 if it["symbol"] in universe else 0
        if k[2] == "market":
            by_day.setdefault(it["period_start"], []).append(it)
        else:
            out.append(it)
    for d, lst in by_day.items():
        lst.sort(key=lambda x: (-x["score"], x["symbol"]))
        out += lst[:KEY_PER_DAY]
    out.sort(key=lambda x: (x["period_start"], x["score"]), reverse=True)
    cols = ["first_seen_utc", "symbol", "tf", "period_start", "kind", "direction", "signal_direction", "detail", "score",
            "edge_30d", "n", "win_30d", "variant", "liq_rank", "also", "in_universe"]
    raw_by_day = {}
    for r_ in raw:
        raw_by_day[r_[3]] = raw_by_day.get(r_[3], 0) + 1
    return {"columns": cols, "rows": [[r(it.get(c)) if c != "also" else it["also"] for c in cols] for it in out],
            "rules": {"top_liquid": KEY_TOP_LIQUID, "min_cases": KEY_MIN_N, "min_cases_per_period": KEY_MIN_SPLIT,
                      "min_edge_30d_pct": KEY_MIN_EDGE, "per_day": KEY_PER_DAY, "window_days": days,
                      "min_history_days": KEY_MIN_HISTORY, "excluded_tokens": sorted(KEY_EXCLUDE), "repeat_days": KEY_REPEAT_DAYS,
                      "major_kinds": sorted(KEY_MAJOR), "volume_confirmed_kinds": sorted(KEY_NEED_VOLUME)},
            "logged_per_day": raw_by_day}


# ---------------------------------------------------------------- market snapshot (Overview)
def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    k = len(xs)
    return None if not k else (xs[k // 2] if k % 2 else (xs[k // 2 - 1] + xs[k // 2]) / 2)


def market_snapshot(con, universe, last):
    """Compact market context: Fear & Greed, BTC trend, breadth (1 year), advancers/decliners, new highs/lows,
    typical returns, volatility and volume vs normal. Current universe only (display, not research)."""
    m = {"as_of": last}
    if exists(con, "fear_greed"):
        fg = con.execute("SELECT date, value, label FROM fear_greed WHERE date >= ? ORDER BY date",
                         ((datetime.strptime(last, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d"),)).fetchall()
        if fg:
            byd = {d: v for d, v, _ in fg}
            ago = lambda n: byd.get((datetime.strptime(fg[-1][0], "%Y-%m-%d") - timedelta(days=n)).strftime("%Y-%m-%d"))
            m["fear_greed"] = {"date": fg[-1][0], "value": fg[-1][1], "label": fg[-1][2], "week_ago": ago(7), "month_ago": ago(30),
                               "series": [[d, v] for d, v, _ in fg], "source": "alternative.me"}
    b = con.execute("""SELECT close, sma200, ema50 FROM indicators WHERE symbol='BTCUSDT' AND tf='1d' ORDER BY period_start DESC LIMIT 1""").fetchone()
    bl = con.execute("SELECT ret_1d, ret_7d, ret_30d FROM latest WHERE symbol='BTCUSDT'").fetchone()
    if b:
        m["btc"] = {"close": r(b[0]), "sma200": r(b[1]), "above_sma200": None if b[1] is None else int(b[0] > b[1]),
                    "dist_sma200_pct": None if not b[1] else r(100 * (b[0] / b[1] - 1)),
                    "ret_1d": r(bl[0]) if bl else None, "ret_7d": r(bl[1]) if bl else None, "ret_30d": r(bl[2]) if bl else None}
    u = set(universe)
    since = (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
    ser = {}
    for sym, ps, c, e50, e200, w200 in con.execute(
            "SELECT symbol, period_start, close, ema50, ema200, ema200_warm FROM indicators WHERE tf='1d' AND period_start >= ?", (since,)):
        if sym not in u:
            continue
        x = ser.setdefault(ps, [0, 0, 0, 0])
        if e50 is not None:
            x[0] += 1
            x[1] += c > e50
        if e200 is not None and w200:
            x[2] += 1
            x[3] += c > e200
    br = [[d, r(100 * v[1] / v[0], 4) if v[0] else None, r(100 * v[3] / v[2], 4) if v[2] else None] for d, v in sorted(ser.items())]
    if br:
        now = br[-1]
        def back(n):
            d = (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=n)).strftime("%Y-%m-%d")
            return next((x for x in reversed(br) if x[0] <= d), None)
        m["breadth"] = {"above_ema50_pct": now[1], "above_ema200_pct": now[2],
                        "above_ema200_30d_ago": (back(30) or [None, None, None])[2], "series": br}
    prev = (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    adv = dec = 0
    for sym, c0, c1 in con.execute("""SELECT a.symbol, a.close, b.close FROM daily a JOIN daily b ON b.symbol = a.symbol AND b.date = ?
                                      WHERE a.date = ?""", (last, prev)):
        if sym in u:
            adv += c1 > c0
            dec += c1 < c0
    m["day"] = {"advancers": adv, "decliners": dec}
    hl = dict(con.execute("""SELECT signal, COUNT(*) FROM signals WHERE tf='1d' AND period_start=? AND warm=1
                             AND signal IN ('new_1y_high','new_1y_low') GROUP BY signal""", (last,)).fetchall()) if exists(con, "signals") else {}
    m["day"].update(new_1y_highs=hl.get("new_1y_high", 0), new_1y_lows=hl.get("new_1y_low", 0))
    lt = con.execute("SELECT ret_7d, ret_30d, d_atr14_pct FROM latest WHERE in_universe=1").fetchall()
    m["typical"] = {"ret_7d_median": r(_median([x[0] for x in lt])), "ret_30d_median": r(_median([x[1] for x in lt]))}
    days90 = (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")
    vol_by_day, atr_by_day = {}, {}
    for sym, ps, ap in con.execute("SELECT symbol, period_start, atr14_pct FROM indicators WHERE tf='1d' AND period_start >= ?", (days90,)):
        if sym in u and ap is not None:
            atr_by_day.setdefault(ps, []).append(ap)
    for sym, d, v in con.execute("SELECT symbol, date, volume_usdt FROM daily WHERE date >= ?", (days90,)):
        if sym in u:
            vol_by_day[d] = vol_by_day.get(d, 0.0) + (v or 0.0)
    atr_med = {d: _median(v) for d, v in atr_by_day.items()}
    if atr_med.get(last) is not None:
        m["volatility"] = {"atr_pct_median": r(atr_med[last]), "atr_pct_median_90d": r(_median(list(atr_med.values())))}
    if last in vol_by_day:
        prior = [v for d, v in sorted(vol_by_day.items()) if d < last][-30:]
        m["volume"] = {"total_usd": r(vol_by_day[last]), "avg_30d_usd": r(sum(prior) / len(prior)) if prior else None}
    return m


# ---------------------------------------------------------------- trade ideas (for Bilal's own decisions)
IDEA_TOP_LIQUID, IDEA_MIN_EDGE, IDEA_DAYS = 50, 2.0, 30
IDEA_SCORE_DAYS = 90      # the scorecard follows every idea of the last 90 days; the table shows the last IDEA_DAYS
IDEA_MAX_LONG, IDEA_MAX_SHORT, IDEA_STOP_ATR = 6, 6, 1.5   # long and short treated equally, whatever the market mood


def liquidity_ranks(con, dates):
    """{date: {symbol: rank}} by 20-day average USDT volume (rank 1 = most liquid)."""
    rank = {}
    if not dates:
        return rank
    lo = (datetime.strptime(min(dates), "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    series = {}
    for s, d, v in con.execute("SELECT symbol, date, volume_usdt FROM daily WHERE date >= ? ORDER BY symbol, date", (lo,)):
        series.setdefault(s, []).append((d, v or 0.0))
    want, per_day = set(dates), {}
    for s, xs in series.items():
        for i, (d, _) in enumerate(xs):
            if d in want and i >= 19:
                per_day.setdefault(d, []).append((sum(v for _, v in xs[i - 19:i + 1]) / 20, s))
    for d, lst in per_day.items():
        lst.sort(reverse=True)
        rank[d] = {s: k + 1 for k, (_, s) in enumerate(lst)}
    return rank



def idea_outcomes(con, ideas, horizon=30):
    """How each past idea has done so far, measured honestly: entry at the NEXT day's open (the first price
    anyone could act on), exit at the reference stop if any later candle touched it (at the open if the candle
    gapped through it), otherwise marked at the latest close. Fees and slippage are not included.
    Adds out_status ("open"/"stopped"/"pending"), out_pct (side-adjusted %), out_days, out_best_pct to each idea
    and returns a scorecard over the ideas that have been live for at least one day."""
    done = []
    for x in ideas:
        x.update(out_status="pending", out_pct=None, out_days=0, out_best_pct=None)
        bars = con.execute("SELECT date, open, high, low, close FROM daily WHERE symbol=? AND date>? ORDER BY date",
                           (x["symbol"], x["date"])).fetchall()[:horizon]
        if not bars or not x.get("stop"):
            continue
        long_ = x["side"] == "long"
        entry, stop = bars[0][1], x["stop"]
        if not entry or entry <= 0 or (long_ and stop >= entry) or (not long_ and stop <= entry):
            x["out_status"] = "skipped"          # opened beyond the stop: no trade
            continue
        sgn = 1 if long_ else -1
        best, status, px, held = 0.0, ("closed" if len(bars) >= horizon else "open"), bars[-1][4], len(bars)
        for k, (d, o, h, l, c) in enumerate(bars):
            if (long_ and l <= stop) or (not long_ and h >= stop):
                px = min(o, stop) if long_ else max(o, stop)
                status, held = "stopped", k + 1
                break
            best = max(best, sgn * ((h if long_ else l) / entry - 1) * 100)
        x.update(out_status=status, out_pct=r(sgn * (px / entry - 1) * 100), out_days=held, out_best_pct=r(best))
        done.append(x)
    if not done:
        return {"n": 0}
    res = [x["out_pct"] for x in done]
    side = lambda s: [x["out_pct"] for x in done if x["side"] == s]
    summ = lambda v: {"n": len(v), "win_pct": r(100.0 * sum(1 for z in v if z > 0) / len(v)) if v else None,
                      "avg_pct": r(sum(v) / len(v)) if v else None}
    return dict(summ(res), stopped=sum(1 for x in done if x["out_status"] == "stopped"),
                long=summ(side("long")), short=summ(side("short")), method="next open entry, reference stop, no fees")

def trade_ideas(con, days=IDEA_DAYS, score_days=IDEA_SCORE_DAYS):
    """Daily trade ideas from every indicator signal in the database. For each liquid pair and day, the proven
    signals that fired (>= KEY_MIN_N cases, >= KEY_MIN_SPLIT before and after 2022, same sign overall/early/late,
    |30-day edge| >= IDEA_MIN_EDGE %, using the volume-confirmed record when the candle was confirmed) are combined
    into one idea: LONG if they point up, SHORT if they point down (shown equally, whatever the market mood),
    flagged when they conflict. Ranked by how many
    proven signals agree, then by the strongest edge. Reference entry = signal close; suggested stop =
    close -/+ IDEA_STOP_ATR x ATR(14) for longs/shorts."""
    if not exists(con, "signal_stats") or not exists(con, "signals"):
        return {"rows": [], "rules": {}}
    last = con.execute("SELECT MAX(period_start) FROM signals WHERE tf='1d'").fetchone()[0]
    since = (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=max(days, score_days) - 1)).strftime("%Y-%m-%d")
    shown = (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    stats = {}
    for sig, tf, d, var, n, w30, e30, ee, el, ne, nl in con.execute(
            "SELECT signal, tf, direction, variant, n, win_30d, edge_30d, edge_30d_early, edge_30d_late, n_early, n_late"
            " FROM signal_stats WHERE n IS NOT NULL AND tf='1d'"):
        if (n >= KEY_MIN_N and (ne or 0) >= KEY_MIN_SPLIT and (nl or 0) >= KEY_MIN_SPLIT and None not in (e30, ee, el)
                and abs(e30) >= IDEA_MIN_EDGE and e30 * ee > 0 and e30 * el > 0):
            stats[(sig, d, var)] = (n, w30, e30)
    from binance_sync import STABLE_BASES
    excluded = KEY_EXCLUDE | set(STABLE_BASES)
    token = dict(con.execute("SELECT symbol, token FROM pairs"))
    start = dict(con.execute("SELECT symbol, series_start FROM series_info")) if exists(con, "series_info") else {}
    supply = dict(con.execute("SELECT token, circulating FROM supply")) if exists(con, "supply") else {}
    by = {}
    for sym, ps, sig, d, conf, rv, close in con.execute(
            "SELECT symbol, period_start, signal, direction, confirmed, rvol, close FROM signals"
            " WHERE tf='1d' AND warm=1 AND provisional=0 AND period_start >= ?", (since,)):
        ev = stats.get((sig, d, "confirmed")) if conf == 1 else None
        var = "confirmed" if ev else "all"
        ev = ev or stats.get((sig, d, "all"))
        if ev:
            by.setdefault((sym, ps), []).append(dict(signal=sig, direction=d, variant=var, n=ev[0], win_30d=ev[1],
                                                     edge_30d=ev[2], close=close, rvol=rv))
    rank = liquidity_ranks(con, sorted({ps for _, ps in by}))
    out, per_day = [], {}
    for (sym, ps), sigs in by.items():
        lr = rank.get(ps, {}).get(sym)
        if lr is None or lr > IDEA_TOP_LIQUID or token.get(sym) in excluded:
            continue
        ss = start.get(sym)
        if not ss or (datetime.strptime(ps, "%Y-%m-%d") - datetime.strptime(ss, "%Y-%m-%d")).days < KEY_MIN_HISTORY:
            continue
        up = sorted([x for x in sigs if x["edge_30d"] > 0], key=lambda x: -x["edge_30d"])
        dn = sorted([x for x in sigs if x["edge_30d"] < 0], key=lambda x: x["edge_30d"])
        side = "long" if (len(up), max([x["edge_30d"] for x in up] or [0])) >= (len(dn), max([-x["edge_30d"] for x in dn] or [0])) else "short"
        main, other = (up, dn) if side == "long" else (dn, up)
        ind = con.execute("SELECT close, atr14, bull_score, sma200, rsi14, rvol20 FROM indicators WHERE symbol=? AND tf='1d' AND period_start=?",
                          (sym, ps)).fetchone() or (None,) * 6
        close, atr, score, sma200, rsi, rvol = ind
        idea = {"date": ps, "symbol": sym, "side": side, "agree": len(main), "conflict": len(other),
                "best_edge_30d": main[0]["edge_30d"], "best_win_30d": main[0]["win_30d"], "best_n": main[0]["n"],
                "signals": [[x["signal"], x["direction"], round(x["edge_30d"], 2), x["n"], x["variant"]] for x in main],
                "against": [[x["signal"], x["direction"], round(x["edge_30d"], 2)] for x in other],
                "close": close,
                "stop": ((close - IDEA_STOP_ATR * atr) if side == "long" else (close + IDEA_STOP_ATR * atr)) if (close and atr) else None,
                "stop_pct": (IDEA_STOP_ATR * atr / close * 100) if (close and atr) else None,
                "confluence": score, "above_sma200": (None if sma200 is None or close is None else int(close > sma200)),
                "rsi14": rsi, "rvol20": rvol, "liq_rank": lr,
                "mcap_est": (close * supply[token[sym]]) if close and token.get(sym) in supply else None}
        per_day.setdefault((ps, side), []).append(idea)
    for (ps, side), lst in per_day.items():
        lst.sort(key=lambda x: (-x["agree"], -abs(x["best_edge_30d"]), x["liq_rank"]))
        out += lst[:IDEA_MAX_LONG if side == "long" else IDEA_MAX_SHORT]
    out.sort(key=lambda x: (x["date"], x["side"] == "long", x["agree"], abs(x["best_edge_30d"])), reverse=True)
    score = idea_outcomes(con, out)
    score["days"] = score_days
    out = [x for x in out if x["date"] >= shown]
    for x in out:
        for k in ("best_edge_30d", "best_win_30d", "close", "stop", "stop_pct", "confluence", "rsi14", "rvol20", "mcap_est"):
            x[k] = r(x[k])
    return {"rows": out, "scorecard": score, "rules": {"top_liquid": IDEA_TOP_LIQUID, "min_edge_30d_pct": IDEA_MIN_EDGE, "days": days,
                                    "max_long_per_day": IDEA_MAX_LONG, "max_short_per_day": IDEA_MAX_SHORT,
                                    "stop_atr": IDEA_STOP_ATR, "min_cases": KEY_MIN_N, "min_cases_per_period": KEY_MIN_SPLIT,
                                    "min_history_days": KEY_MIN_HISTORY}}


def run(con=None):
    t0 = time.time()
    own = con is None
    if own:
        con = sqlite3.connect(DB, timeout=60)
    if not exists(con, "latest") or not exists(con, "dictionary"):
        print("export_web: analytics v2 not built yet, skipping")
        return
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    last = con.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    status = {}
    sp = os.path.join(ROOT, "status.json")
    if os.path.exists(sp):
        with open(sp) as f:
            status = json.load(f)
    site = {}
    sp2 = os.path.join(ROOT, "site_status.json")
    if os.path.exists(sp2):
        try:
            with open(sp2) as f:
                site = {k: v for k, v in json.load(f).items() if k in ("published_utc", "token_expires_utc")}
        except ValueError:
            site = {}
    universe = [s for (s,) in con.execute("SELECT symbol FROM v_universe ORDER BY symbol")]
    ago = lambda days: (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")

    write(os.path.join(OUT, "overview.json"), {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "synced_through": meta.get("synced_through"),
        "analytics_built_utc": meta.get("analytics_built_utc"),
        "history_start": meta.get("history_start"),
        "last_date": last,
        "status": status,
        "site": site,
        "supply_as_of": (con.execute("SELECT MAX(as_of) FROM supply").fetchone()[0] if exists(con, "supply") else None),
        "market": market_snapshot(con, universe, last),
        "counts": {
            "pairs_tracked": con.execute("SELECT COUNT(*) FROM pairs").fetchone()[0],
            "pairs_trading": con.execute("SELECT COUNT(*) FROM pairs WHERE status='TRADING'").fetchone()[0],
            "pairs_filtered": len(universe),
            "daily_rows": con.execute("SELECT COUNT(*) FROM daily").fetchone()[0],
            "first_date": con.execute("SELECT MIN(date) FROM daily").fetchone()[0],
            "gaps": con.execute("SELECT COUNT(*) FROM gaps").fetchone()[0],
            "anomalies": con.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0],
            "series_breaks": con.execute("SELECT COALESCE(SUM(breaks),0) FROM series_info").fetchone()[0],
        },
        "dictionary": table(con, "SELECT col, family, label, description, warm_from_bar_1d, warm_from_bar_1w, warm_from_bar_1M FROM dictionary"),
        "watch_stats": table(con, "SELECT * FROM watch_stats ORDER BY horizon, cohort DESC"),
        "watch_yearly": table(con, """SELECT substr(date,1,4) AS year, COUNT(*) AS n, AVG(MIN(fwd_ret_7d, 150)) AS avg_7d,
              100.0*SUM(CASE WHEN fwd_ret_7d>0 THEN 1 ELSE 0 END)/COUNT(fwd_ret_7d) AS win_7d, AVG(MIN(fwd_ret_30d, 300)) AS avg_30d,
              100.0*SUM(CASE WHEN fwd_ret_30d>0 THEN 1 ELSE 0 END)/COUNT(fwd_ret_30d) AS win_30d
              FROM watch_hits GROUP BY year ORDER BY year"""),
        "baseline_yearly": table(con, """SELECT substr(f.date,1,4) AS year, COUNT(f.fwd_ret_7d) AS n, AVG(MIN(f.fwd_ret_7d, 150)) AS avg_7d,
              100.0*SUM(CASE WHEN f.fwd_ret_7d>0 THEN 1 ELSE 0 END)/COUNT(f.fwd_ret_7d) AS win_7d, AVG(MIN(f.fwd_ret_30d, 300)) AS avg_30d,
              100.0*SUM(CASE WHEN f.fwd_ret_30d>0 THEN 1 ELSE 0 END)/COUNT(f.fwd_ret_30d) AS win_30d
              FROM forward f JOIN daily d ON d.symbol=f.symbol AND d.date=f.date
              WHERE d.volume_usdt >= 100000 GROUP BY year ORDER BY year"""),
        "runs": table(con, "SELECT run_utc, ok, synced_through, rows_added, failures, seconds FROM runs ORDER BY run_utc DESC LIMIT 14"),
        "audits": table(con, "SELECT * FROM audits ORDER BY run_utc DESC LIMIT 10") if exists(con, "audits") else None,
        "signal_stats": table(con, "SELECT * FROM signal_stats") if exists(con, "signal_stats") else None,
        "catalog": signals_lib.CATALOG,
        "signal_params": {"pivot_left": signals_lib.PIVOT_L, "pivot_right": signals_lib.PIVOT_R,
                          "divergence_range": signals_lib.DIV_RANGE, "confirm_rvol": signals_lib.CONFIRM_RVOL},
    })

    if exists(con, "supply"):
        # ESTIMATED market cap = last close x today's circulating supply (Binance). Approximate by design.
        write(os.path.join(OUT, "screener.json"), table(con, """SELECT l.*, l.last_close * s.circulating AS mcap_est
              FROM latest l LEFT JOIN supply s ON s.token = l.token WHERE l.in_universe=1 ORDER BY l.symbol"""))
    else:
        write(os.path.join(OUT, "screener.json"), table(con, "SELECT * FROM latest WHERE in_universe=1 ORDER BY symbol"))
    hl = key_highlights(con)
    hl["ideas"] = trade_ideas(con)
    try:
        import setups
        hl["setups"] = setups.run(con)
    except Exception as e:                       # a research add-on must never block the export
        print("export_web: setups skipped:", repr(e))
        hl["setups"] = {"rows": [], "stats": {}, "kinds": {}, "rules": {}}
    try:                                         # parallel channels, daily + 4h (research add-on, never blocks the export)
        import chan
        hl["channels"] = chan.run(con)
    except Exception as e:
        print("export_web: channels skipped:", repr(e))
        hl["channels"] = {"rows": [], "stats": {}, "labels": {}, "rules": {}}
    write(os.path.join(OUT, "highlights.json"), hl)
    try:                                         # futures context: funding, open interest, long/short (public data)
        import derivatives
        snap = derivatives.snapshot(con, universe)
        mk = {}
        if snap:
            top = [x for x in snap.values() if x.get("funding_apr") is not None]
            mk = {"pairs": len(snap), "median_funding_apr": r(sorted(x["funding_apr"] for x in top)[len(top) // 2]) if top else None,
                  "hot": sum(1 for x in top if x["funding_apr"] >= 30), "negative": sum(1 for x in top if x["funding_apr"] < 0)}
        write(os.path.join(OUT, "derivs.json"), {"as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "market": mk,
                                                 "pairs": {k: {kk: r(vv) for kk, vv in v.items()} for k, v in snap.items()}})
    except Exception as e:
        print("export_web: derivatives skipped:", repr(e))
    write(os.path.join(OUT, "listings.json"), {
        "listings": table(con, """SELECT p.symbol, p.token, p.status, p.first_candle, p.delisted_on,
              (SELECT close FROM daily d WHERE d.symbol=p.symbol ORDER BY date DESC LIMIT 1) AS last_close,
              (SELECT quote_volume_24h FROM ticker_snapshots t WHERE t.symbol=p.symbol ORDER BY snap_date DESC LIMIT 1) AS vol_24h_usdt
              FROM pairs p WHERE p.first_candle >= ? OR p.delisted_on >= ? ORDER BY COALESCE(p.delisted_on, p.first_candle) DESC""",
                          (ago(180), ago(180))),
        "universe_log": table(con, "SELECT * FROM universe_log WHERE date >= ? ORDER BY date DESC", (ago(180),)),
    })
    write(os.path.join(OUT, "weekly.json"), table(con, "SELECT * FROM weekly_report"))
    write(os.path.join(OUT, "watch.json"), table(con, "SELECT * FROM watch_hits WHERE date >= ? ORDER BY date DESC, rvol DESC", (ago(365),)))
    write(os.path.join(OUT, "signals.json"), table(con, """
        SELECT tf, period_start, symbol, signal, direction, provisional, close, confirmed, rvol FROM signals
        WHERE warm=1 AND symbol IN (SELECT symbol FROM v_universe)
          AND ((tf='1d' AND period_start >= ?) OR (tf IN ('1w','1M') AND period_start >= ?))
        ORDER BY period_start DESC, tf, signal, symbol""", (ago(30), ago(182))))

    d_from = ago(DAILY_DAYS - 1)
    w_from = (datetime.strptime(last, "%Y-%m-%d") - timedelta(weeks=WEEKS)).strftime("%Y-%m-%d")
    wd = {r_[0]: r_[1:] for r_ in con.execute("SELECT col, warm_from_bar_1d, warm_from_bar_1w, warm_from_bar_1M FROM dictionary")}
    tfi = {"1d": 0, "1w": 1, "1M": 2}
    cols = ["t", "o", "h", "l", "c", "v", "e21", "e50", "e200", "rsi", "macd", "sig", "rv", "bbu", "bbl", "st", "sa", "sb", "x"]
    bits = [("ema21", 2), ("ema50", 4), ("ema200", 8), ("rsi14", 16), ("macd", 32), ("supertrend", 64), ("ichi_span_b", 128)]
    index, shards = {}, [dict() for _ in range(SHARDS)]
    for i, sym in enumerate(universe):
        k = i % SHARDS
        index[sym] = f"s{k:02d}.json"
        entry = {}
        for tf, frm in (("1d", d_from), ("1w", w_from), ("1M", "0000")):
            rows = con.execute("""
                SELECT c.period_start, c.open, c.high, c.low, c.close, c.volume_usdt, i.ema21, i.ema50, i.ema200,
                       i.rsi14, i.macd, i.macd_signal, i.rvol20, i.bb_up, i.bb_lo, i.supertrend, i.ichi_span_a,
                       i.ichi_span_b, c.complete, i.bar
                FROM candles c JOIN indicators i USING(symbol, tf, period_start)
                WHERE c.symbol=? AND c.tf=? AND c.period_start >= ? ORDER BY c.period_start""", (sym, tf, frm)).fetchall()
            out = []
            for row in rows:
                bar = row[19]
                x = row[18] + sum(b for col, b in bits if bar >= wd[col][tfi[tf]])
                out.append([row[0]] + [r(v) for v in row[1:18]] + [x])
            entry[tf] = out
        shards[k][sym] = entry
    for k, sh in enumerate(shards):
        write(os.path.join(OUT, "pairs", f"s{k:02d}.json"), {"columns": cols, "pairs": sh})
    write(os.path.join(OUT, "pairs", "index.json"), index)
    try:                                         # 4-hour chart data (once 4h candles are downloaded)
        import h4_export
        n4 = h4_export.write(con, universe, OUT, int(time.time() * 1000))
        print(f"export_web: 4h charts for {n4} pairs")
    except Exception as e:
        print("export_web: 4h charts skipped:", repr(e))
    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(OUT) for f in fs)
    print(f"export_web: {len(universe)} pairs, {size / 1e6:.1f} MB, {time.time() - t0:.0f}s")
    if own:
        con.close()


if __name__ == "__main__":
    run()
