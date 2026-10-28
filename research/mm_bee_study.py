"""
MM-Bee study on a year of ZIG 1-hour candles (Bilal, 29 Oct 2026): v1 (daily base, 12-hour revision) against v2
(base follows each 4-hour close, staggered), with and without accumulation, and a grid of settings looking for a
better ZIG market maker. Design half: Nov 2025 - Jun 2026; check half: Jul - Oct 2026.

    python3 research/mm_bee_study.py CANDLES.json > out.txt      (CANDLES: [[open_s, o, h, l, c], ...] or a site h1 shard)
"""
import itertools
import json
import sys
from multiprocessing import Pool

import _env  # noqa: F401
import mm_bee_backtest as BT

WINDOWS = [("year", "2025-11-01T00:00:00Z", None), ("15d", "2026-10-13T00:00:00Z", None), ("5d", "2026-10-24T00:00:00Z", None)]
KEYS = ("rounds", "profit_usdt", "vs_hold_usdt", "change_usdt", "hold_change_usdt", "sells", "buys", "ladders", "worst_dip_vs_hold_usdt", "coins", "start_price", "end_price")
ROWS = None


def load(path):
    d = json.load(open(path))
    if isinstance(d, dict):
        d = d["pairs"]["ZIGUSDT"]
    return [tuple(r[:5]) for r in d]


def one(args):
    name, settings = args
    out = {"name": name, "settings": settings}
    for w, a, z in WINDOWS:
        r = BT.run(ROWS, start=a, end=z, settings=settings)
        out[w] = {k: r[k] for k in KEYS}
    return out


def init(rows):
    global ROWS
    ROWS = rows


def main():
    rows = load(sys.argv[1])
    named = [("v1 (daily base, 12 h revision)", {"rebase_hours": 0}),
             ("v2 (4h rebase)", {}),
             ("v2 + accumulation 10 units", {"acc_units": 10}),
             ("v2 + accumulation 20 units", {"acc_units": 20}),
             ("v2, daily rebase", {"rebase_hours": 24}),
             ("v2, 1h rebase", {"rebase_hours": 1})]
    grid = []
    for first, gap, rebuy, resell in itertools.product((0.02, 0.03, 0.04), (0.01, 0.015, 0.02), (0.02, 0.031, 0.045), (0.02, 0.03, 0.045)):
        grid.append((f"grid first {first} gap {gap} rebuy {rebuy} resell {resell}", {"first": first, "gap": gap, "rebuy": rebuy, "resell": resell}))
    with Pool(initializer=init, initargs=(rows,)) as p:
        res = p.map(one, named + grid, chunksize=2)
    json.dump(res, sys.stdout, indent=0)


if __name__ == "__main__":
    main()
