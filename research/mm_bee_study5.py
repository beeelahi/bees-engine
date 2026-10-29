"""
MM-Bee study, round 5 (29 Oct 2026, overnight): the most robust MM-Bee+ settings from round 4, with and without
regime switching (recycling off in a flat market, on in a trend), on the down / up / flat paths, plus both halves of
the real year (design Nov-Jun, check Jul-Oct) and the last 15 and 5 days.

    python3 research/mm_bee_study5.py ZIG_SHARD.json > out.jsonl
"""
import json
import sys
from multiprocessing import Pool

import _env  # noqa: F401
import mm_bee_backtest as BT
from mm_bee_study2 import mirror, flat

P = None
WIN = [("year", "2025-11-01T00:00:00Z", None), ("design", "2025-11-01T00:00:00Z", "2026-07-01T00:00:00Z"),
       ("check", "2026-07-01T00:00:00Z", None), ("15d", "2026-10-13T00:00:00Z", None), ("5d", "2026-10-24T00:00:00Z", None)]
KEYS = ("rounds", "profit_usdt", "vs_hold_usdt", "change_usdt", "coins")


def one(args):
    name, st = args
    out = {"name": name, "settings": st}
    for path, rows in P.items():
        for w, a, z in WIN if path == "down" else WIN[:1]:
            r = BT.run(rows, start=a, end=z, settings=st)
            out[f"{path}:{w}"] = {k: r[k] for k in KEYS}
    return out


def init(p):
    global P
    P = p


def main():
    d = json.load(open(sys.argv[1]))
    z = [tuple(r[:5]) for r in d["pairs"]["ZIGUSDT"]]
    paths = {"down": z, "up": mirror(z), "flat": flat(z)}
    g = {"vol_fast": 0, "btc_guard": 0}
    A = dict(g, first=0.04, gap=0.01, rebuy=0.031, resell=0.045)
    B = dict(g, first=0.04, gap=0.01, rebuy=0.045, resell=0.045)
    bil = dict(g)
    jobs = [("Bilal spacing, recycle ladder", dict(bil, recycle="ladder")),
            ("Bilal spacing, recycle off", dict(bil, recycle="off")),
            ("A +4%/1%/-3.1%/+4.5%, ladder", dict(A, recycle="ladder")),
            ("B +4%/1%/-4.5%/+4.5%, ladder", dict(B, recycle="ladder"))]
    for nm, base in (("Bilal", bil), ("A", A), ("B", B)):
        for ema, band in ((30, 0.03), (30, 0.05), (60, 0.05), (90, 0.08)):
            jobs.append((f"{nm} regime ema {ema} band {band}: flat off, trend ladder",
                         dict(base, recycle="ladder", regime_ema=ema, regime_band=band,
                              regimes={"flat": {"recycle": "off"}, "up": {"recycle": "ladder"}, "down": {"recycle": "ladder"}})))
    with Pool(initializer=init, initargs=(paths,)) as p:
        for r in p.imap_unordered(one, jobs):
            print(json.dumps(r), flush=True)


if __name__ == "__main__":
    main()
