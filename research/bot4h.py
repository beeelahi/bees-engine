"""Candidate 4-hour paper bots, tested on the 4h candle export (Oct 2023 on). No look-ahead: signals on a closed
candle, entry at the next open; fees 0.1% a side plus 0.05% slippage; a stop is filled at the stop or the open if
it gapped through. Results split in two halves (first half = design, second = unseen).

  breakout4h  long: close above the highest high of the previous 120 candles (20 days) on 2x the 30-candle average
              volume, coin among the TOPN most liquid that day, BTC above its 4h EMA-200; stop = entry - 2.5 x ATR(14),
              trailing exit at the lowest low of the last 60 candles (10 days); max 60 days.
  fade4h      short: a 4h candle closed 15%+ above the previous close (liquid coins); entry next open, stop above that
              candle's high (+0.5 ATR), exit after 18 candles (3 days) or at the stop.
Usage: python3 research/bot4h.py [TOPN]"""
import csv
import gzip
import statistics as st
import sys
from collections import defaultdict

import _env

TOPN = int(sys.argv[1]) if len(sys.argv) > 1 else 20
COST = 0.0015
D = defaultdict(list)
with gzip.open(_env.path("candles_4h.csv.gz"), "rt") as f:
    rd = csv.reader(f)
    next(rd)
    for s, t, o, h, l, c, v in rd:
        D[s].append((int(t), float(o), float(h), float(l), float(c), float(v or 0)))
STABLE = ("USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "XUSD", "USDE", "BFUSD", "USD1")
for s in list(D):
    if s.startswith(STABLE) or s.split("USDT")[0] in ("PAXG", "XAUT", "WBTC", "WBETH"):
        del D[s]
    else:
        D[s].sort()


def ema(x, n):
    k, out, e = 2 / (n + 1), [], None
    for v in x:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def atr(X, n=14):
    out, a = [], None
    for k in range(len(X)):
        tr = X[k][2] - X[k][3] if k == 0 else max(X[k][2], X[k - 1][4]) - min(X[k][3], X[k - 1][4])
        a = tr if a is None else a + (tr - a) / n
        out.append(a)
    return out


btc = D["BTCUSDT"]
btc_ok = {}
e200 = ema([x[4] for x in btc], 200)
for k, x in enumerate(btc):
    btc_ok[x[0]] = k >= 200 and x[4] > e200[k]
# liquidity rank per candle time: 30-candle average volume
vol = defaultdict(dict)
for s, X in D.items():
    run = 0.0
    for k, x in enumerate(X):
        run += x[5]
        if k >= 30:
            run -= X[k - 30][5]
            vol[x[0]][s] = run / 30
rank = {}
for t, m in vol.items():
    rank[t] = {s: i + 1 for i, (s, _) in enumerate(sorted(m.items(), key=lambda z: -z[1]))}
times = sorted(vol)
half = times[len(times) // 2]

trades = defaultdict(list)
for s, X in D.items():
    A = atr(X)
    n = len(X)
    k = 125
    while k < n - 2:
        t, o, h, l, c, v = X[k]
        vavg = sum(x[5] for x in X[k - 30:k]) / 30
        r = rank.get(t, {}).get(s, 999)
        # breakout4h
        if r <= TOPN and btc_ok.get(t) and c > max(x[2] for x in X[k - 120:k]) and vavg and v >= 2 * vavg:
            ent = X[k + 1][1] * (1 + COST)
            stop = ent - 2.5 * A[k]
            out, j = None, k + 1
            while j < min(n, k + 361):
                lo = X[j][3]
                trail = min(x[3] for x in X[j - 60:j])
                lvl = max(stop, trail)
                if lo <= lvl:
                    out = min(X[j][1], lvl) * (1 - COST)
                    break
                j += 1
            if out is None:
                j = min(n - 1, k + 360)
                out = X[j][4] * (1 - COST)
            trades["breakout4h"].append((t, s, (out / ent - 1) * 100, (out - ent) / (ent - stop)))
            k = j + 1
            continue
        # fade4h
        prev = X[k - 1][4]
        if prev > 0 and c / prev - 1 >= 0.15 and vavg * 6 >= 2e6:
            ent = X[k + 1][1] * (1 - COST)
            stop = h + 0.5 * A[k]
            out, j = None, k + 1
            for j in range(k + 1, min(n, k + 19)):
                if X[j][2] >= stop:
                    out = max(X[j][1], stop) * (1 + COST)
                    break
            if out is None:
                out = X[min(n - 1, k + 18)][4] * (1 + COST)
            trades["fade4h"].append((t, s, (ent / out - 1) * 100, (ent - out) / (stop - ent)))
            k = j + 1
            continue
        k += 1


def summ(L):
    if len(L) < 5:
        return f"n={len(L)}"
    pnl = [x[2] for x in L]
    R = [x[3] for x in L]
    return (f"n={len(L):4d} win {100 * sum(p > 0 for p in pnl) / len(L):3.0f}%  avg {st.mean(pnl):+5.2f}%  med {st.median(pnl):+5.2f}%  "
            f"avgR {st.mean(R):+.2f}  PF {sum(p for p in pnl if p > 0) / max(1e-9, -sum(p for p in pnl if p < 0)):.2f}")


for name, L in trades.items():
    print(f"{name:11s} first half  {summ([x for x in L if x[0] < half])}")
    print(f"{name:11s} second half {summ([x for x in L if x[0] >= half])}")


def jsum(L):
    pnl, R = [x[2] for x in L], [x[3] for x in L]
    return {"n": len(L), "win": round(100 * sum(p > 0 for p in pnl) / len(L)), "avg_pct": round(st.mean(pnl), 2),
            "med_pct": round(st.median(pnl), 2), "avg_r": round(st.mean(R), 2),
            "pf": round(sum(p for p in pnl if p > 0) / max(1e-9, -sum(p for p in pnl if p < 0)), 2)}


if trades["fade4h"]:
    import json
    from datetime import datetime, timezone
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")   # noqa: E731
    L = trades["fade4h"]
    out = {"first_half": jsum([x for x in L if x[0] < half]), "second_half": jsum([x for x in L if x[0] >= half]),
           "from": iso(times[0]), "split": iso(half), "to": iso(times[-1]),
           "rejected": {"breakout4h": {"first_half": jsum([x for x in trades["breakout4h"] if x[0] < half]),
                                       "second_half": jsum([x for x in trades["breakout4h"] if x[0] >= half])}}}
    with open(_env.path("paper4h_stats.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
