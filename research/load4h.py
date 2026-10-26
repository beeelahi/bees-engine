"""4-hour data for the research scripts, in the same shape as bot_sim.prepare (day numbers -> 4h bar numbers).
dn = open_ms // 4h, so a gap check of '> 3' means a gap longer than 12 hours. vavg20i is a daily-equivalent
20-day average volume (last 120 bars x 6) so the same liquidity rank thresholds apply."""
import csv, gzip, sys
from collections import defaultdict
import _env
import load, bot_sim as S
from bot_core import day_num, excluded_tokens, _atr, _rsi
from datetime import datetime, timezone
H4 = 14_400_000


def bar_of_day(d):            # 4h bar number of a 'YYYY-MM-DD' date's first bar
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) // H4


def get(path=None, topn=150):
    path = path or _env.path("candles_4h.csv.gz")
    rows, tok = load.load(_env.path("daily_v2.csv.gz"), _env.path("pairs_v2.csv"))
    _, dreg = S.prepare(rows, tok)            # BTC 200-day regime by day number
    excl = excluded_tokens(set(tok.values()))
    ser = defaultdict(list)
    with gzip.open(path, "rt") as f:
        rd = csv.reader(f); next(rd)
        for s, t, o, h, l, c, v in rd:
            if tok.get(s) in excl: continue
            ser[s].append((int(t) // H4, float(o), float(h), float(l), float(c), float(v or 0)))
    data = {}
    for s, R in ser.items():
        R.sort()
        dn = [r[0] for r in R]; o, h, l, c, v = ([r[k] for r in R] for k in range(1, 6))
        seg, k0 = [], 0
        for i in range(len(dn)):
            if i and dn[i] - dn[i - 1] > 42: k0 = i          # 7-day pause restarts the series
            seg.append(i - k0)
        va = [None] * len(v); vi = [None] * len(v); acc = 0.0
        for i in range(len(v)):
            acc += v[i]
            if i >= 120: acc -= v[i - 120]
            vi[i] = acc / min(i + 1, 120) * 6
            if i >= 20: va[i] = sum(v[i - 20:i]) / 20
        data[s] = dict(dn=dn, o=o, h=h, l=l, c=c, v=v, atr20=_atr(h, l, c, 20), rsi14=_rsi(c, 14), vavg20=va, vavg20i=vi, seg_bar=seg)
    # regime per 4h bar: the previous daily close's regime
    reg = {}
    dates = sorted(dreg)
    for F in data.values():
        for b in F["dn"]:
            if b not in reg:
                d = (b * H4) // 86_400_000 - 1          # previous day, in epoch days
                reg[b] = dreg.get(d + day_num("1970-01-01"), 0)
    byday = defaultdict(list)
    for s, F in data.items():
        for i, b in enumerate(F["dn"]):
            if F["vavg20i"][i] >= 2e6 and F["seg_bar"][i] >= 300: byday[b].append((F["vavg20i"][i], s))
    rank = {}
    for b, L in byday.items():
        L.sort(reverse=True)
        for k, (_, s) in enumerate(L[:topn]): rank[(s, b)] = k + 1
    return data, reg, tok, rank, bar_of_day("2022-01-01")
