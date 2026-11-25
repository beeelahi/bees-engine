"""Trace reference patterns through the STF HNS rules on fresh candles (read-only).

For each case: every left shoulder / head / right shoulder combination around the given head time (swing points of
2, 3, 5 and 10 candles; up to 6 swing points back for the left shoulder and 4 forward for the right), the neckline
through the lowest points between them, and each rule it fails under the live rules and the spike-head variant.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stf_lab as L  # noqa: E402

S, HS = L.S, L.HS
CASES = [("SOL", "15m", "2026-11-23 12:00", "2026-11-23 16:00", "top"),
         ("SOL", "1h", "2026-11-23 12:00", "2026-11-23 16:00", "top")]


def fails(p, h, l, c):
    sg, H, rs = p["sg"], p["height"], p["rsh"]
    X, Y = (h, l) if sg == 1 else (l, h)
    nk = lambda k: p["m"] * k + p["b"]  # noqa: E731
    out = []
    if rs - p["lsh"] < S.WIDTH_MIN or p["hd"] - p["lsh"] < S.SIDE_MIN or rs - p["hd"] < S.SIDE_MIN:
        out.append(f"width {rs - p['lsh']}")
    dl, dr = sg * (X[p["lsh"]] - nk(p["lsh"])), sg * (X[rs] - nk(rs))
    if min(dl, dr) < S.SHOULDER_MIN * H:
        out.append(f"shoulders {dl / H:.2f}/{dr / H:.2f}H")
    d = sg * (nk(rs) - nk(p["lsh"])) / H
    if d < -S.NECK_AGAINST or d > S.NECK_WITH:
        out.append(f"neckline {'down' if sg * d < 0 else 'up'} {abs(d):.2f}H")
    if H - max(dl, dr) < S.HEAD_MIN * H:
        out.append(f"head {(H - max(dl, dr)) / H:.2f}H over shoulder")
    if abs(dl - dr) > S.SYM_MAX * H:
        out.append(f"uneven shoulders {abs(dl - dr) / H:.2f}H")
    r = (rs - p["hd"]) / (p["hd"] - p["lsh"])
    if not S.TIME_RATIO[0] <= r <= S.TIME_RATIO[1]:
        out.append(f"time ratio {r:.2f}")
    zone = lambda k: max(sg * c[j] for j in range(max(0, k - 2), min(len(c), k + 3)))  # noqa: E731
    if S.HEAD_CLOSE and zone(p["hd"]) <= max(zone(p["lsh"]), zone(rs)):
        out.append("head not higher on closes")
    tr = S.typical_range(h, l, c, p["hd"])
    if H < S.ATR_MIN * tr:
        out.append(f"size {H / tr:.1f}x candle")
    pre = max(0, p["lsh"] - 30)
    if min(sg * Y[k] for k in range(pre, p["lsh"] + 1)) > sg * nk(p["hd"]) - S.PRIOR_MIN * H:
        out.append("no prior move")
    if rs - p["lsh"] > S.SPAN:
        out.append(f"wider than {S.SPAN}")
    return out


def trace(code, tf, a, b, kind):
    rows, name = L.history(code, tf, log=lambda *x: None)
    t, o, h, l, c = S._series(rows)
    print(f"== {code} {tf} ({name}) {L.utc(t[0])} .. {L.utc(t[-1])}, last close {c[-1]}")
    sg = 1 if kind == "top" else -1
    X, Y = (h, l) if sg == 1 else (l, h)
    seen = set()
    for P in (2, 3, 5, 10):
        pk = HS.pivots(X, P, sg == 1)
        for j, hd in enumerate(pk):
            if not a <= L.utc(t[hd]) <= b:
                continue
            for lsh in pk[max(0, j - 6):j]:
                for rsh in pk[j + 1:j + 5]:
                    if (lsh, hd, rsh) in seen or max(sg * X[k] for k in range(lsh, rsh + 1)) > sg * X[hd]:
                        continue
                    seen.add((lsh, hd, rsh))
                    lo = (lambda k: Y[k]) if sg == 1 else (lambda k: -Y[k])
                    t1, t2 = min(range(lsh + 1, hd), key=lo), min(range(hd + 1, rsh), key=lo)
                    m = (Y[t2] - Y[t1]) / (t2 - t1)
                    H = sg * (X[hd] - (Y[t1] + m * (hd - t1)))
                    if H <= 0:
                        continue
                    p = {"sg": sg, "lsh": lsh, "hd": hd, "rsh": rsh, "m": m, "b": Y[t1] - m * t1, "height": H}
                    res = []
                    for var in ("live", "spike_head"):
                        for k, v in L.LIVE.items():
                            setattr(S, k, v)
                        for k, v in dict(next(x for x in L.VARIANTS if x[0] == var)[2]).items():
                            setattr(S, k, v)
                        f = fails(p, h, l, c)
                        res.append(f"{var}: {'PASSES' if not f else '; '.join(f)}")
                    print(f"P{P} LS {L.utc(t[lsh])[5:]} {X[lsh]} | H {L.utc(t[hd])[5:]} {X[hd]} | RS {L.utc(t[rsh])[5:]} {X[rsh]} | "
                          f"neck {Y[t1]}@{L.utc(t[t1])[5:]} -> {Y[t2]}@{L.utc(t[t2])[5:]} | H {H:.2f}\n    " + "\n    ".join(res))
    for k, v in L.LIVE.items():
        setattr(S, k, v)
    for var in ("live", "spike_head"):
        for k, v in dict(next(x for x in L.VARIANTS if x[0] == var)[2]).items():
            setattr(S, k, v)
        br = [(x["kind"], L.utc(x["head_ms"]), L.utc(x["bar_ms"])) for x in L.direct_breaks(rows) if x["bar_ms"] >= t[-400]]
        print(f"  break alerts in the last 400 candles, {var}: {br}")
        for k, v in L.LIVE.items():
            setattr(S, k, v)


if __name__ == "__main__":
    for case in CASES:
        trace(*case)
