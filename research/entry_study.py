"""
Entry and exit study for the daily breakout bots (Bilal, 29 Oct 2026): does entering at once / on a retracement /
on a stop order during the day, or taking profit earlier and moving the stop to break-even, beat the current rules?
Same simulator as the live paper bots (bin/bot_sim.py), all pairs including delisted ones, costs included.

    RESEARCH_DATA=/path python3 research/entry_study.py [leaders|majors|turbo] > out.txt
"""
import csv
import gzip
import json
import sys
import time

import _env  # noqa: F401
import bot_sim as S
import bot as BOT
from bot_core import day_num


def load():
    tokens = {r["symbol"]: r["token"] for r in csv.DictReader(open(_env.path("pairs_v2.csv")))}
    rows = []
    with gzip.open(_env.path("daily_v2.csv.gz"), "rt") as f:
        rd = csv.reader(f)
        next(rd)
        for s, d, o, h, l, c, v, *_ in rd:
            rows.append((s, d, float(o), float(h), float(l), float(c), float(v or 0)))
    rows.sort()
    return S.prepare(rows, tokens)


EXITS = {
    "now: 1/3 at +2R, then BE": {},
    "1/3 at +1R, then BE": dict(tp_r=1.0),
    "1/2 at +1R, then BE": dict(tp_r=1.0, tp_frac=0.5),
    "1/3 at +1.5R, then BE": dict(tp_r=1.5),
    "BE at +1R close, no partial": dict(tp_r=0.0, be=1.0),
    "no partial, no BE": dict(tp_r=0.0),
}
ENTRIES = {
    "now: next open after the close": dict(entry="open"),
    "limit 0.5 ATR under the close, 3 days": dict(entry="limit", entry_atr=0.5, entry_ttl=3),
    "limit 1 ATR under the close, 5 days": dict(entry="limit", entry_atr=1.0, entry_ttl=5),
    "stop order at the 20-day high, during the day": dict(entry="stop"),
}


def run(data, regime, bot, strat_over, cfg_over, last):
    p = dict(bot["strategy"][1], **strat_over)
    strat = S.STRATS[bot["strategy"][0]](**p)
    cfg = dict(start_equity=bot["capital"], **bot.get("config", {}), **cfg_over)
    b0, b1, b2 = (day_num(x) for x in BOT.BACKTEST)
    st = S.simulate(data, regime, strat, b0, last, cfg)
    f = lambda a, z: S.metrics(st, a, z)                                     # noqa: E731
    return {"full": f(b0, last), "design": f(b0, b1), "unseen": f(b2, last)}


def fmt(m):
    return (f"{m.get('trades', 0):4d} tr  win {100 * m.get('win', 0):4.0f}%  CAGR {100 * m.get('cagr', 0):+6.1f}%  "
            f"maxDD {100 * m.get('maxdd', 0):6.1f}%  PF {m.get('pf', 0):4.2f}  avgR {m.get('avg_r', 0):+5.2f}")


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "leaders"
    bot = next(b for b in BOT.BOTS if b["id"] == which)
    t = time.time()
    data, regime = load()
    last = max(F["dn"][-1] for F in data.values())
    print(f"# {bot['name']} {bot['version']}: data loaded in {time.time() - t:.0f}s, {len(data)} pairs, through day {last}")
    out = {}
    for en, ec in ENTRIES.items():
        for xn, xs in EXITS.items():
            if en != "now: next open after the close" and xn not in ("now: 1/3 at +2R, then BE", "1/3 at +1R, then BE"):
                continue
            r = run(data, regime, bot, xs, ec, last)
            out[f"{en} | {xn}"] = r
            print(f"{en:46s} | {xn:30s}\n   design 2018-21: {fmt(r['design'])}\n   unseen 2022-26: {fmt(r['unseen'])}", flush=True)
    json.dump(out, open(f"entry_study_{which}.json", "w"), indent=1, default=float)


if __name__ == "__main__":
    main()
