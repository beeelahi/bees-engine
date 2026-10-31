"""
Leading setups: possible tops, bottoms and accumulation breakouts (Python 3.9 stdlib only).

Unlike the lagging trend signals, these look for exhaustion: momentum fading while price makes a new extreme
(RSI divergence at a range high or low), on the daily and the weekly chart, plus breakouts from a contained
accumulation range on heavy volume. Only the 100 most liquid pairs count (20-day average volume >= $2M,
200+ days of history), so every setup is on a coin that matters.

No look-ahead: a divergence needs a confirmed swing (5 days after a daily swing, 2 weeks after a weekly one);
the setup is stamped on the day it became knowable. Results are measured from the NEXT day's open.
The full-history track record of each setup is recomputed on every run from the same code.
"""
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from bot_core import day_num, excluded_tokens, segments, _rsi, _atr, _prev_max, _prev_min

TOP_N, MIN_ADV, MIN_BARS = 100, 2e6, 200
DEDUP_DAYS = 10                       # one setup of a kind per pair per 10 days
MAX_LATE_ATR = 3.0                    # skip a divergence once price is already > 3 ATR away from the swing (too late)
HORIZONS = (14, 30, 60)
CLUSTER_N = 5                         # bottom breakouts within 14 days that count as a cluster (breadth)
SPLIT = "2022-01-01"
SHOW_DAYS = 30
EPOCH = datetime(2000, 1, 1)
EPOCH_DN = day_num("2000-01-01")

KINDS = {
    "top_w_div": {"side": "short", "tf": "1w", "label": "Possible major top (weekly)",
                  "text": "Weekly RSI bearish divergence at a 26-week high: price made a higher high while weekly RSI, "
                          "overbought (above 70) on the earlier peak, made a lower high."},
    "top_d_div": {"side": "short", "tf": "1d", "label": "Possible top (daily)",
                  "text": "Daily RSI bearish divergence at a 90-day high: a new high on weaker momentum, the earlier peak overbought (RSI above 70)."},
    "bottom_d_div": {"side": "long", "tf": "1d", "label": "Possible bottom (daily)",
                     "text": "Daily RSI bullish divergence at a 90-day low: a new low on stronger momentum, the earlier low oversold (RSI below 30)."},
    "hs_top": {"side": "short", "tf": "1d", "label": "Head and shoulders top (daily)",
               "text": "A completed head-and-shoulders top: three swing highs with the middle one highest, shoulders of similar height, "
                       "after a rise into the pattern; the setup is the first daily close below the neckline (within 30 days of the right "
                       "shoulder). Invalid above the right shoulder; measured target = neckline minus the pattern height."},
    "bottom_breakout": {"side": "long", "tf": "1d", "label": "Bottom accumulation breakout",
                        "text": "After a fall of 80%+ from the 2-year high, a tight 60-day base (high no more than 1.5x the low), then the "
                                "first close above the base on at least 2x normal volume. On its own close to a coin flip since 2022; "
                                "strong when many coins do it at once (see the 14-day count)."},
    "accum_breakout": {"side": "long", "tf": "1d", "label": "Accumulation breakout",
                       "text": "First close above a contained 90-day range (high no more than 2x the low) on at least 2x normal volume."},
}


def iso(dn):
    return (EPOCH + timedelta(days=dn - EPOCH_DN)).strftime("%Y-%m-%d")


def _pivots(x, L, R, high):
    out = []
    for p in range(L, len(x) - R):
        v = x[p]
        if v is None:
            continue
        a, b = x[p - L:p], x[p + 1:p + R + 1]
        if None in a or None in b:
            continue
        if high and all(v > z for z in a) and all(v >= z for z in b):
            out.append(p)
        if not high and all(v < z for z in a) and all(v <= z for z in b):
            out.append(p)
    return out


def _load(con):
    from binance_sync import STABLE_BASES
    tokens = dict(con.execute("SELECT symbol, token FROM pairs"))
    from binance_sync import stock_tokens
    excl = excluded_tokens(set(tokens.values())) | set(STABLE_BASES) | stock_tokens(con)    # bStocks: not crypto
    ser = defaultdict(list)
    for s, d, o, h, l, c, v in con.execute("SELECT symbol, date, open, high, low, close, volume_usdt FROM daily ORDER BY symbol, date"):
        if tokens.get(s) not in excl:
            ser[s].append((day_num(d), o, h, l, c, v or 0.0))
    return ser


def scan(con):
    ser = _load(con)
    # liquidity rank per day (20-day average incl. today, within a continuous series)
    byday = defaultdict(list)
    P = {}
    for s, rows in ser.items():
        dn = [r[0] for r in rows]
        o, h, l, c, v = ([r[k] for r in rows] for k in range(1, 6))
        segs = [(a, b) for a, b in segments(dn, c) if b - a >= MIN_BARS]
        P[s] = (dn, o, h, l, c, v, segs)
        for a, b in segs:
            acc = 0.0
            for k in range(a, b):
                acc += v[k]
                if k - a >= 20:
                    acc -= v[k - 20]
                if k - a >= MIN_BARS and acc / 20 >= MIN_ADV:
                    byday[dn[k]].append((acc / 20, s))
    rank = {}
    for d, lst in byday.items():
        lst.sort(reverse=True)
        for k, (_, s) in enumerate(lst[:TOP_N]):
            rank[(s, d)] = k + 1

    events = []
    for s, (dn, o, h, l, c, v, segs) in P.items():
        for a, b in segs:
            cs, hs, ls, vs = c[a:b], h[a:b], l[a:b], v[a:b]
            rsi, atr = _rsi(cs, 14), _atr(hs, ls, cs, 20)
            hh90, ll90 = _prev_max(hs, 90), _prev_min(ls, 90)
            hh60, ll60, h2y = _prev_max(hs, 60), _prev_min(ls, 60), _prev_max(hs, 730)

            def add(k, kind, ref_level, **extra):
                d = dn[a + k]
                if kind not in ("accum_breakout", "hs_top", "bottom_breakout") and (atr[k] is None or abs(cs[k] - ref_level) > MAX_LATE_ATR * atr[k]):
                    return
                if (s, d) in rank:
                    events.append(dict(symbol=s, dn=d, kind=kind, i=a + k, level=ref_level, atr=atr[k], liq_rank=rank[(s, d)], **extra))

            # daily RSI divergences at 90-day extremes, confirmed 5 days after the swing
            for high in (False, True):
                prev = None
                for p in _pivots(rsi, 5, 5, high):
                    k = p + 5
                    if prev is not None and 5 <= (k - 1) - (prev + 5) <= 60 and prev >= 30 and k < len(cs):
                        if not high and rsi[p] > rsi[prev] and ls[p] < ls[prev] and rsi[prev] < 30 and ll90[p] and ls[p] <= ll90[p]:
                            add(k, "bottom_d_div", ls[p])
                        if high and rsi[p] < rsi[prev] and hs[p] > hs[prev] and rsi[prev] > 70 and hh90[p] and hs[p] >= hh90[p]:
                            add(k, "top_d_div", hs[p])
                    prev = p
            # weekly bars: 7-day blocks of the continuous series; RSI divergence at a 26-week high
            wk = []
            for k in range(len(cs)):
                key = dn[a + k] // 7
                if wk and wk[-1][0] == key:
                    w = wk[-1]
                    w[2], w[3], w[4], w[5] = max(w[2], hs[k]), min(w[3], ls[k]), cs[k], k
                else:
                    wk.append([key, k, hs[k], ls[k], cs[k], k])
            if wk and (dn[a + wk[-1][5]] + 1) // 7 == wk[-1][0]:
                wk = wk[1:-1]          # the last block is still open: drop it (and the partial first one)
            else:
                wk = wk[1:]
            if len(wk) > 60:
                wc, wh = [w[4] for w in wk], [w[2] for w in wk]
                wr, whh = _rsi(wc, 14), _prev_max(wh, 26)
                prev = None
                for p in _pivots(wr, 3, 2, True):
                    q = p + 2
                    if prev is not None and 4 <= q - prev <= 40 and prev >= 20 and q < len(wk):
                        if wr[p] < wr[prev] and wh[p] > wh[prev] and wr[prev] > 70 and whh[p] and wh[p] >= whh[p]:
                            add(wk[q][5], "top_w_div", wh[p])
                    prev = p
            # head-and-shoulders tops (same rules as the research test: daily, 5-bar swings, no look-ahead)
            ph, pl = _pivots(hs, 5, 5, True), _pivots(ls, 5, 5, False)
            for j in range(2, len(ph)):
                lsh, hd, rsh = ph[j - 2], ph[j - 1], ph[j]
                t1c = [t for t in pl if lsh < t < hd]
                t2c = [t for t in pl if hd < t < rsh]
                if not t1c or not t2c or rsh - lsh > 120:
                    continue
                t1, t2 = min(t1c, key=lambda t: ls[t]), min(t2c, key=lambda t: ls[t])
                H, L_, R_ = hs[hd], hs[lsh], hs[rsh]
                if not (H > L_ and H > R_):
                    continue
                neck_mid = (ls[t1] + ls[t2]) / 2
                height = H - neck_mid
                if height <= 0 or abs(L_ - R_) > 0.35 * height or H - max(L_, R_) < 0.2 * height:
                    continue
                if not (0.4 <= (hd - lsh) / (rsh - hd) <= 2.5) or abs(ls[t1] - ls[t2]) > 0.5 * height:
                    continue
                pre = max(0, lsh - 30)
                if not min(ls[pre:lsh + 1]) < neck_mid - 0.5 * height:
                    continue
                slope = (ls[t2] - ls[t1]) / (t2 - t1)
                for k in range(rsh + 5, min(rsh + 35, len(cs))):
                    if hs[k] > R_:
                        break                               # right shoulder taken out first: pattern failed
                    neck = ls[t2] + slope * (k - t2)
                    if cs[k] < neck:
                        add(k, "hs_top", R_, neck=neck, target=neck - height,
                            pattern={"ls": [iso(dn[a + lsh]), hs[lsh]], "head": [iso(dn[a + hd]), hs[hd]], "rs": [iso(dn[a + rsh]), hs[rsh]],
                                     "neck": [[iso(dn[a + t1]), ls[t1]], [iso(dn[a + k]), neck]]})
                        break
            # bottom accumulation breakout: deep drawdown, tight 60-day base, volume breakout
            for k in range(MIN_BARS, len(cs)):
                if hh60[k] and ll60[k] and h2y[k] and cs[k] > hh60[k] and cs[k - 1] <= hh60[k] and hh60[k] / ll60[k] <= 1.5 \
                        and ll60[k] <= 0.2 * h2y[k]:
                    pv = sum(vs[k - 20:k]) / 20
                    if pv > 0 and vs[k] / pv >= 2.0:
                        add(k, "bottom_breakout", ll60[k])
            # accumulation breakout
            for k in range(MIN_BARS, len(cs)):
                if hh90[k] and ll90[k] and cs[k] > hh90[k] and cs[k - 1] <= hh90[k] and hh90[k] / ll90[k] <= 2.0:
                    pv = sum(vs[k - 20:k]) / 20
                    if pv > 0 and vs[k] / pv >= 2.0:
                        add(k, "accum_breakout", hh90[k])

    # de-duplicate and measure
    events.sort(key=lambda e: (e["symbol"], e["kind"], e["dn"]))
    kept, last = [], {}
    for e in events:
        key = (e["symbol"], e["kind"])
        if key in last and e["dn"] - last[key] < DEDUP_DAYS:
            continue
        last[key] = e["dn"]
        kept.append(e)
    for e in kept:
        dn, o, h, l, c, v, _ = P[e["symbol"]]
        i, sgn = e["i"], (1 if KINDS[e["kind"]]["side"] == "long" else -1)
        e["fwd"] = {}
        if i + 1 < len(c) and dn[i + 1] - dn[i] <= 3:
            ent = o[i + 1]
            for H in HORIZONS:
                j = i + H
                e["fwd"][H] = sgn * (c[j] / ent - 1) if j < len(c) and dn[j] - dn[i] <= H + 5 else None
    return kept, P


def _summ(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    cap = [max(-1.0, min(1.0, x)) for x in xs]
    return {"n": len(xs), "median_pct": round(100 * statistics.median(xs), 2), "avg_capped_pct": round(100 * sum(cap) / len(cap), 2),
            "win_pct": round(100.0 * sum(1 for x in xs if x > 0) / len(xs), 1)}


REGIMES = {"up": "Uptrend: BTC above its 200-day average and at least half of the liquid coins above theirs",
           "down": "Downtrend: BTC below its 200-day average and fewer than 40% of the liquid coins above theirs",
           "mixed": "Mixed: everything in between"}


def regimes(P, btc="BTCUSDT"):
    """Market regime per day (no look-ahead: uses closes up to that day). Breadth = share of pairs with 200+ days
    of history closing above their own 200-day EMA."""
    from bot_core import _ema
    up, tot = defaultdict(int), defaultdict(int)
    for s, (dn, o, h, l, c, v, segs) in P.items():
        for a, b in segs:
            e = _ema(c[a:b], 200)
            for k in range(a + 199, b):
                tot[dn[k]] += 1
                if c[k] > e[k - a]:
                    up[dn[k]] += 1
    out = {}
    if btc not in P:
        return out
    dn, o, h, l, c, v, segs = P[btc]
    for a, b in segs:
        sma = [None] * (b - a)
        acc = 0.0
        for k in range(a, b):
            acc += c[k]
            if k - a >= 200:
                acc -= c[k - 200]
            if k - a >= 199:
                sma[k - a] = acc / 200
        for k in range(a, b):
            d = dn[k]
            if sma[k - a] is None or tot[d] < 20:
                continue
            br = up[d] / tot[d]
            above = c[k] > sma[k - a]
            out[d] = "up" if (above and br >= 0.5) else "down" if ((not above) and br < 0.4) else "mixed"
    return out


def run(con, stop_atr=1.5):
    kept, P = scan(con)
    split = day_num(SPLIT)
    reg = regimes(P)
    stats = {}
    for kind in KINDS:
        ev = [e for e in kept if e["kind"] == kind]
        stats[kind] = {"early": {H: _summ([e["fwd"].get(H) for e in ev if e["dn"] < split]) for H in HORIZONS},
                       "late": {H: _summ([e["fwd"].get(H) for e in ev if e["dn"] >= split]) for H in HORIZONS},
                       "regime": {g: {H: _summ([e["fwd"].get(H) for e in ev if reg.get(e["dn"]) == g]) for H in HORIZONS}
                                  for g in REGIMES}}
    # bottom-breakout clusters: how many fired in the 14 days up to each one (breadth of bottoms breaking out)
    bb = sorted(e["dn"] for e in kept if e["kind"] == "bottom_breakout")
    import bisect
    for e in kept:
        if e["kind"] == "bottom_breakout":
            e["cluster"] = bisect.bisect_right(bb, e["dn"]) - bisect.bisect_left(bb, e["dn"] - 13)
    evb = [e for e in kept if e["kind"] == "bottom_breakout" and e["dn"] >= split]
    data_last0 = max(P[s][0][-1] for s in P) if P else None
    cluster = {"window_days": 14, "threshold": CLUSTER_N,
               "now": (bisect.bisect_right(bb, data_last0) - bisect.bisect_left(bb, data_last0 - 13)) if data_last0 else 0,
               "clustered": {H: _summ([e["fwd"].get(H) for e in evb if e["cluster"] >= CLUSTER_N]) for H in HORIZONS},
               "alone": {H: _summ([e["fwd"].get(H) for e in evb if e["cluster"] < CLUSTER_N]) for H in HORIZONS}}
    reg_now = reg.get(max(reg)) if reg else None
    data_last = max(P[s][0][-1] for s in P) if P else None
    rows = []
    for e in kept:
        if data_last is None or e["dn"] < data_last - SHOW_DAYS + 1:
            continue
        dn, o, h, l, c, v, _ = P[e["symbol"]]
        i, k = e["i"], KINDS[e["kind"]]
        close, atr = c[i], e["atr"]
        long_ = k["side"] == "long"
        # invalidation: beyond the swing extreme (divergences) or back inside the range (breakouts), with an ATR buffer
        stop = (e["level"] - 0.5 * atr if long_ else e["level"] + 0.5 * atr) if e["kind"] != "accum_breakout" else close - stop_atr * atr
        row = {"date": iso(e["dn"]), "symbol": e["symbol"], "kind": e["kind"], "side": k["side"], "tf": k["tf"],
               "close": close, "level": e["level"], "stop": stop, "stop_pct": abs(stop / close - 1) * 100 if close else None,
               "liq_rank": e["liq_rank"]}
        if e["kind"] == "hs_top":
            row.update(neck=e["neck"], target=e["target"], target_pct=abs(e["target"] / close - 1) * 100 if close else None)
            row["pattern"] = e["pattern"]
        # outcome so far: entry next open, out at the stop, else marked at the latest close (closed after 30 days)
        bars = [(dn[j], o[j], h[j], l[j], c[j]) for j in range(i + 1, min(len(c), i + 31))]
        if not bars:
            row.update(out_status="pending", out_pct=None, out_days=0)
        else:
            ent = bars[0][1]
            if (long_ and stop >= ent) or (not long_ and stop <= ent):
                row.update(out_status="skipped", out_pct=None, out_days=0)
            else:
                status, px, held = ("closed" if len(bars) >= 30 else "open"), bars[-1][4], len(bars)
                for m, (d, oo, hh, ll, cc) in enumerate(bars):
                    if (long_ and ll <= stop) or (not long_ and hh >= stop):
                        status, px, held = "stopped", (min(oo, stop) if long_ else max(oo, stop)), m + 1
                        break
                sgn = 1 if long_ else -1
                row.update(out_status=status, out_pct=round(100 * sgn * (px / ent - 1), 2), out_days=held)
        rows.append(row)
    rows.sort(key=lambda x: (x["date"], x["tf"] == "1w", -x["liq_rank"]), reverse=True)
    for x in rows:
        for f in ("close", "level", "stop", "stop_pct", "neck", "target", "target_pct"):
            if x.get(f) is not None:
                x[f] = float(f"{x[f]:.7g}")
    return {"rows": rows, "stats": stats, "bottom_cluster": cluster, "regime_now": reg_now, "regimes": REGIMES,
            "regime_days": {g: sum(1 for x in reg.values() if x == g) for g in REGIMES},
            "kinds": {k: {f: v[f] for f in ("side", "tf", "label", "text")} for k, v in KINDS.items()},
            "rules": {"top_n": TOP_N, "min_adv": MIN_ADV, "dedup_days": DEDUP_DAYS, "show_days": SHOW_DAYS,
                      "split": SPLIT, "horizons": list(HORIZONS), "stop_atr_breakout": stop_atr, "max_late_atr": MAX_LATE_ATR}}
