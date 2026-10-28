"""
MM-Bee study, round 4 (29 Oct 2026, overnight): the MM-Bee+ settings grid (first sell, gap, buy-back, resell) on the
real ZIG year (down 51%), its mirror image (same moves, up) and the trend-removed path (flat). Which settings win in
each kind of market, and is there one that holds up in all three?

    python3 research/mm_bee_study4.py ZIG_SHARD.json > out.json
"""
import itertools
import json
import sys
from multiprocessing import Pool

import _env  # noqa: F401
import mm_bee_backtest as BT
from mm_bee_study2 import mirror, flat

P = None
KEYS = ("rounds", "profit_usdt", "vs_hold_usdt", "change_usdt", "coins", "worst_dip_vs_hold_usdt")


def one(args):
    name, st = args
    out = {"name": name, "settings": st}
    for path, rows in P.items():
        r = BT.run(rows, start="2025-11-01T00:00:00Z", settings=st)
        out[path] = {k: r[k] for k in KEYS}
    return out


def init(p):
    global P
    P = p


def main():
    d = json.load(open(sys.argv[1]))
    z = [tuple(r[:5]) for r in d["pairs"]["ZIGUSDT"]]
    paths = {"down": z, "up": mirror(z), "flat": flat(z)}
    jobs = []
    for rec in ("ladder", "breakeven"):
        for first, gap, rebuy, resell in itertools.product((0.02, 0.03, 0.04), (0.01, 0.02, 0.03), (0.02, 0.031, 0.045), (0.02, 0.03, 0.045)):
            st = {"recycle": rec, "vol_fast": 0, "btc_guard": 0, "first": first, "gap": gap, "rebuy": rebuy, "resell": resell}
            jobs.append((f"{rec} first {first} gap {gap} rebuy {rebuy} resell {resell}", st))
    with Pool(initializer=init, initargs=(paths,)) as p:
        res = p.map(one, jobs, chunksize=4)
    json.dump(res, sys.stdout, indent=0)


if __name__ == "__main__":
    main()
