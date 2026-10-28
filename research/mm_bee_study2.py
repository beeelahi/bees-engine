"""
MM-Bee study, round 2 (29 Oct 2026). Round 1 (mm_bee_study.py) showed that over a full year a unit bought back on
the way down gets stuck: its +3% resell sits far above a falling price, so activity dies. And ZIG fell 51% that
year, so settings that end up net-sold look good for reasons of direction, not market making.
Here: a few candidate settings x recycle (off / breakeven / ladder), each on three price paths built from the real
ZIG candles: the real one (down 51%), the mirror image (the same moves, up instead of down) and the real one with
its trend removed (flat). A setting worth trading must hold up on all three.

    python3 research/mm_bee_study2.py CANDLES.json > out.json
"""
import json
import math
import sys
from multiprocessing import Pool

import _env  # noqa: F401
import mm_bee_backtest as BT
from mm_bee_study import WINDOWS, KEYS, load

PATHS = None


def mirror(rows):
    k = rows[0][1] ** 2
    return [(t, k / o, k / l, k / h, k / c) for t, o, h, l, c in rows]


def flat(rows):
    t0, t1 = rows[0][0], rows[-1][0]
    g = math.log(rows[-1][4] / rows[0][1]) / (t1 - t0)
    f = lambda t: math.exp(-g * (t - t0))                       # noqa: E731
    return [(t, o * f(t), h * f(t), l * f(t), c * f(t)) for t, o, h, l, c in rows]


def one(args):
    name, settings, path = args
    out = {"name": name, "path": path, "settings": settings}
    for w, a, z in WINDOWS:
        r = BT.run(PATHS[path], start=a, end=z, settings=settings)
        out[w] = {k: r[k] for k in KEYS}
    return out


def init(p):
    global PATHS
    PATHS = p


CANDS = [("Bilal: +3% / 2% / -3.1% / +3%", {}),
         ("dense: +2% / 1% / -3.1% / +2%", {"first": 0.02, "gap": 0.01, "resell": 0.02}),
         ("dense, wider buy-back: +2% / 1% / -4.5% / +2%", {"first": 0.02, "gap": 0.01, "rebuy": 0.045, "resell": 0.02}),
         ("mid: +2% / 1.5% / -3.1% / +3%", {"first": 0.02, "gap": 0.015, "resell": 0.03})]


def main():
    rows = load(sys.argv[1])
    paths = {"real": rows, "mirror": mirror(rows), "flat": flat(rows)}
    jobs = []
    for name, st in CANDS[:2]:
        for rec in ("off", "breakeven", "ladder"):
            for path in ("real", "mirror"):
                jobs.append((f"{name} | recycle {rec}", dict(st, recycle=rec), path))
    with Pool(initializer=init, initargs=(paths,)) as p:
        res = p.map(one, jobs, chunksize=2)
    json.dump(res, sys.stdout, indent=0)


if __name__ == "__main__":
    main()
