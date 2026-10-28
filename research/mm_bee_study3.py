"""
MM-Bee study, round 3 (29 Oct 2026): the BTC guard (pull the sells for 8 h when BTC jumps 2%+ in ~2 h and ZIG has
not moved) and the volatility mode (base follows every 1h close while ZIG has moved 10%+ in the running 4h / day
candle), on and off, for v2 and MM-Bee+ (recycling), on the real year of ZIG and BTC candles and its mirror image.

    python3 research/mm_bee_study3.py ZIG_SHARD.json BTC_SHARD.json > out.json
"""
import json
import sys
from multiprocessing import Pool

import _env  # noqa: F401
import mm_bee_backtest as BT
from mm_bee_study import WINDOWS, KEYS
from mm_bee_study2 import mirror

P = None


def series(path, sym):
    d = json.load(open(path))
    return [tuple(r[:5]) for r in d["pairs"][sym]]


def one(args):
    name, st, path = args
    z, b = P[path]
    out = {"name": name, "path": path}
    for w, a, e in WINDOWS:
        r = BT.run(z, start=a, end=e, settings=st, btc=b)
        out[w] = {k: r[k] for k in KEYS}
    return out


def init(p):
    global P
    P = p


def main():
    z, b = series(sys.argv[1], "ZIGUSDT"), series(sys.argv[2], "BTCUSDT")
    paths = {"real": (z, b), "mirror": (mirror(z), mirror(b))}
    off = {"btc_guard": 0, "vol_fast": 0}
    jobs = []
    for base_name, base in (("v2", {}), ("plus", {"recycle": "ladder"})):
        for gname, g in (("no guards", off), ("BTC guard", {"vol_fast": 0}), ("vol mode", {"btc_guard": 0}), ("both", {})):
            for path in paths:
                jobs.append((f"{base_name} | {gname}", dict(base, **g), path))
    with Pool(initializer=init, initargs=(paths,)) as p:
        res = p.map(one, jobs)
    json.dump(res, sys.stdout, indent=0)


if __name__ == "__main__":
    main()
