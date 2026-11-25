"""Bilal's reference trades, checked against the watcher's current rules (per timeframe, TF_RULES) on Binance candles.

For each case: the highest high (top) in the head window, whether the watcher's own rules produce a break alert for a
head there, and the closest-to-passing layouts with every rule they fail. Read-only. Usage: python3 research/stf_cases.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stf_lab as L  # noqa: E402
import stf_ref as R  # noqa: E402

S, HS = L.S, L.HS
# (market, timeframe, head window from, to (UTC), what Bilal showed)
CASES = [
    ("XAG", "15m", "2026-11-23 08:00", "2026-11-24 04:00", "silver 22-25 Nov, rising neckline ~41.6, his trade"),
    ("XAG", "15m", "2026-10-05 09:00", "2026-10-05 20:00", "silver 5 Oct, rising neckline from 09:30"),
    ("XAG", "15m", "2026-10-06 12:00", "2026-10-07 01:00", "silver 6-7 Oct, rising neckline, broke ~01:30 7 Oct"),
    ("XAG", "15m", "2026-10-10 00:00", "2026-10-12 23:00", "silver ~10-13 Oct, rising neckline"),
    ("XAU", "15m", "2026-10-08 12:00", "2026-10-10 06:00", "gold (OANDA) head ~4,148 between the 8th and 10th"),
    ("XAU", "15m", "2026-09-08 12:00", "2026-09-10 06:00", "gold (OANDA) same chart if it was September"),
]


# Bilal's drawn layouts: (market, tf, left-shoulder window, head window, right-shoulder window, label); each point is
# the extreme candle in its window, the neckline runs through the lowest points between them (as the watcher draws it)
LAYOUTS = [
    ("XAG", "15m", ("2026-11-23 00:00", "2026-11-23 08:00"), ("2026-11-23 10:00", "2026-11-23 18:00"), ("2026-11-24 08:00", "2026-11-24 15:00"), "silver 22-25 Nov as drawn"),
    ("XAG", "15m", ("2026-10-05 05:00", "2026-10-05 09:00"), ("2026-10-05 10:00", "2026-10-05 12:00"), ("2026-10-05 12:15", "2026-10-05 14:00"), "silver 5 Oct"),
    ("XAG", "15m", ("2026-10-10 12:00", "2026-10-11 12:00"), ("2026-10-11 12:00", "2026-10-12 12:00"), ("2026-10-12 12:00", "2026-10-13 10:00"), "silver 10-13 Oct"),
    ("XAU", "15m", ("2026-11-09 00:00", "2026-11-09 06:00"), ("2026-11-09 14:00", "2026-11-10 01:00"), ("2026-11-10 03:00", "2026-11-10 08:00"), "gold 9-10 Nov (OANDA chart; the weekend is squeezed out)"),
]


def layout(rows, tf, lw, hw, rw, what, P=5):
    t, o, h, l, c = S._series(rows)
    pick = lambda w, f: f([k for k in range(len(t)) if w[0] <= L.utc(t[k]) <= w[1]], key=lambda k: h[k])  # noqa: E731
    lsh, hd, rsh = pick(lw, max), pick(hw, max), pick(rw, max)
    t1, t2 = min(range(lsh + 1, hd), key=lambda k: l[k]), min(range(hd + 1, rsh), key=lambda k: l[k])
    m = (l[t2] - l[t1]) / (t2 - t1)
    H = h[hd] - (l[t1] + m * (hd - t1))
    p = {"kind": "top", "sg": 1, "P": P, "lsh": lsh, "hd": hd, "rsh": rsh, "t1": t1, "t2": t2, "m": m, "b": l[t1] - m * t1,
         "height": H, "known": rsh + P, "context": "reversal"}
    with S.rules(tf):
        f = R.fails(p, h, l, c)
    swing = max(k for k in range(1, 30) if all(h[rsh] >= h[j] for j in range(max(0, rsh - k), min(len(h), rsh + k + 1))))
    print(f"\n== {what}: LS {L.utc(t[lsh])[5:]} {h[lsh]} | H {L.utc(t[hd])[5:]} {h[hd]} | RS {L.utc(t[rsh])[5:]} {h[rsh]} | neck {l[t1]}@{L.utc(t[t1])[5:]} -> {l[t2]}@{L.utc(t[t2])[5:]} | height {H:.4g}")
    print(f"   shape: {'; '.join(f) or 'passes'}; the right shoulder is a {swing}-candle swing (the watcher uses 5 and 10)")
    for i in range(p["known"], min(len(c), rsh + HS.MAX_WAIT + 2)):
        st = HS.state(p, o, h, l, c, i)
        if st is None:
            print(f"   {L.utc(t[i])}: no longer valid (right shoulder taken out, or expired)")
            break
        if st["brk"] is not None:
            b = {"n": i, "kind": "top", "sg": 1, "target": st["target"], "stop_shoulder": h[rsh],
                 "stop_neck": st["neck"] + HS.FAIL * H}
            tr = S.trade(rows, b, "stop_shoulder")
            print(f"   close through the neckline {L.utc(t[st['brk']])}, alert would be {L.utc(t[i])} ({st['status']}), target {st['target']:.4g}"
                  f" -> {tr and (tr['exit'], tr['r'])}")
            break


def main():
    cache = {}
    for code, tf, a, b, what in CASES:
        if (code, tf) not in cache:
            cache[(code, tf)] = L.history(code, tf, log=lambda *x: None)[0]
        rows = cache[(code, tf)]
        t, o, h, l, c = S._series(rows)
        idx = [k for k in range(len(t)) if a <= L.utc(t[k]) <= b]
        print(f"\n== {code} {tf}: {what}")
        if not idx:
            print("   no candles in that window")
            continue
        top = max(idx, key=lambda k: h[k])
        print(f"   highest high {h[top]} at {L.utc(t[top])}; window low {min(l[k] for k in idx)}")
        with S.rules(tf):
            br = [x for x in L.direct_breaks(rows) if a <= L.utc(x["head_ms"]) <= b]
            for x in br:
                tr = S.trade(rows, x, "stop_shoulder")
                print(f"   CAUGHT {x['kind']} head {L.utc(x['head_ms'])} break alert {L.utc(x['bar_ms'])} -> {tr and (tr['exit'], tr['r'])}")
            if not br:
                cands = []
                for P in (2, 3, 5, 10):
                    for sg, X, Y in ((1, h, l), (-1, l, h)):
                        pk = HS.pivots(X, P, sg == 1)
                        for j, hd in enumerate(pk):
                            if not a <= L.utc(t[hd]) <= b:
                                continue
                            for lsh in pk[max(0, j - 4):j]:
                                for rsh in pk[j + 1:j + 4]:
                                    if max(sg * X[k] for k in range(lsh, rsh + 1)) > sg * X[hd] or hd - lsh < 2 or rsh - hd < 2:
                                        continue
                                    lo = (lambda k: Y[k]) if sg == 1 else (lambda k: -Y[k])
                                    t1, t2 = min(range(lsh + 1, hd), key=lo), min(range(hd + 1, rsh), key=lo)
                                    m = (Y[t2] - Y[t1]) / (t2 - t1)
                                    H = sg * (X[hd] - (Y[t1] + m * (hd - t1)))
                                    if H <= 0:
                                        continue
                                    p = {"sg": sg, "lsh": lsh, "hd": hd, "rsh": rsh, "m": m, "b": Y[t1] - m * t1, "height": H}
                                    f = R.fails(p, h, l, c)
                                    cands.append((len(f), P, sg, lsh, hd, rsh, t1, t2, H, f))
                seen = set()
                for n, P, sg, lsh, hd, rsh, t1, t2, H, f in sorted(cands, key=lambda x: x[0]):
                    if (lsh, hd, rsh) in seen:
                        continue
                    seen.add((lsh, hd, rsh))
                    X, Y = (h, l) if sg == 1 else (l, h)
                    print(f"   P{P} {'top' if sg == 1 else 'inverse'} LS {L.utc(t[lsh])[5:]} {X[lsh]} H {L.utc(t[hd])[5:]} {X[hd]} RS {L.utc(t[rsh])[5:]} {X[rsh]}"
                          f" neck {Y[t1]}@{L.utc(t[t1])[5:]}->{Y[t2]}@{L.utc(t[t2])[5:]} H {H:.3g}: {'; '.join(f) or 'passes shape rules'}")
                    if len(seen) >= 5:
                        break


if __name__ == "__main__":
    main()
    for code, tf, lw, hw, rw, what in LAYOUTS:
        layout(L.history(code, tf, log=lambda *x: None)[0], tf, lw, hw, rw, what)
