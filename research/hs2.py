"""Head & Shoulders research (daily). No look-ahead: pivots are confirmed R bars after the swing; the pattern
exists only once the right shoulder is confirmed; entry is the next open after the first close beyond the neckline.
Top H&S -> short (and as a 'possible top' signal); inverse H&S -> long.
Trade: stop beyond the right shoulder (+0.25 ATR), target = neckline -/+ pattern height (measured move),
time stop 60 bars. Costs: fee 0.1%/side + slippage by liquidity. Results in R and %."""
import sys, statistics as stt
from collections import defaultdict
import _env
import load, bot_sim as S
H4 = '--4h' in sys.argv
if H4: sys.argv.remove('--4h')
from bot_core import day_num, excluded_tokens, segments

PIV = int(sys.argv[1]) if len(sys.argv) > 1 else 5        # swing definition (bars each side)
TOPN = int(sys.argv[2]) if len(sys.argv) > 2 else 100
SPLIT = day_num("2022-01-01")
FEE = 0.001

if H4:
    import load4h
    data, reg, tok, rank, SPLIT = load4h.get(topn=TOPN)
    excl = set()
else:
    rows, tok = load.load(_env.path("daily_v2.csv.gz"), _env.path("pairs_v2.csv"))
    data, reg = S.prepare(rows, tok)
    excl = excluded_tokens(set(tok.values()))
    byday = defaultdict(list)
    for s, F in data.items():
        if tok.get(s) in excl: continue
        for i, d in enumerate(F["dn"]):
            v = F["vavg20i"][i]
            if v and v >= 2e6 and F["seg_bar"][i] >= 200: byday[d].append((v, s))
    rank = {}
    for d, L in byday.items():
        L.sort(reverse=True)
        for k, (v, s) in enumerate(L[:TOPN]): rank[(s, d)] = k + 1


def pivots(x, L, R, high):
    out = []
    for p in range(L, len(x) - R):
        a, b = x[p - L:p], x[p + 1:p + R + 1]
        v = x[p]
        if high and all(v > z for z in a) and all(v >= z for z in b): out.append(p)
        if not high and all(v < z for z in a) and all(v <= z for z in b): out.append(p)
    return out


def find(F, a, b, top, P):
    """Yield (confirm_bar_of_RS, ls, t1, head, t2, rs) pivot indices for top (or inverse) H&S in segment [a,b)."""
    hi, lo = F["h"][a:b], F["l"][a:b]
    ext = hi if top else lo
    opp = lo if top else hi
    ph = pivots(ext, P, P, top)
    pl = pivots(opp, P, P, not top)
    sg = 1 if top else -1
    out = []
    for j in range(2, len(ph)):
        ls, hd, rs = ph[j - 2], ph[j - 1], ph[j]
        # troughs between shoulders and head
        t1c = [t for t in pl if ls < t < hd]
        t2c = [t for t in pl if hd < t < rs]
        if not t1c or not t2c: continue
        # neckline points: for a top the lowest lows between, for an inverse the highest highs between
        t1 = min(t1c, key=lambda t: opp[t]) if top else max(t1c, key=lambda t: opp[t])
        t2 = min(t2c, key=lambda t: opp[t]) if top else max(t2c, key=lambda t: opp[t])
        H, Ls, Rs = ext[hd], ext[ls], ext[rs]
        if not (sg * (H - Ls) > 0 and sg * (H - Rs) > 0): continue
        neck_mid = (opp[t1] + opp[t2]) / 2
        height = sg * (H - neck_mid)
        if height <= 0: continue
        # shoulders: similar height (within 35% of the pattern height) and clearly below the head (>= 20%)
        if abs(Ls - Rs) > 0.35 * height: continue
        gap = (H - max(Ls, Rs)) if top else (min(Ls, Rs) - H)
        if gap < 0.2 * height: continue
        # time symmetry
        w1, w2 = hd - ls, rs - hd
        if not (0.4 <= w1 / w2 <= 2.5): continue
        if rs - ls > 120: continue
        # neckline not too steep: troughs within 50% of height of each other
        if abs(opp[t1] - opp[t2]) > 0.5 * height: continue
        # prior trend: for a top, price rose into the pattern (low 30 bars before LS at least 1 height below neckline)
        pre = max(0, ls - 30)
        if top and not (min(lo[pre:ls + 1]) < neck_mid - 0.5 * height): continue
        if not top and not (max(hi[pre:ls + 1]) > neck_mid + 0.5 * height): continue
        out.append((rs + P, ls, t1, hd, t2, rs, height))
    return out


def feats(F, a, pat, top, k_break):
    conf, ls, t1, hd, t2, rs, height = pat
    v, rsi, c = F["v"], F["rsi14"], F["c"]
    avgv = lambda x, y: sum(v[a + x:a + y + 1]) / max(1, (y - x + 1))
    vol_ls, vol_rs = avgv(ls - 2, ls + 2), avgv(rs - 2, rs + 2)
    i = a + k_break
    va = F["vavg20"][i] or 0
    r_ls, r_hd, r_rs = rsi[a + ls], rsi[a + hd], rsi[a + rs]
    div = None if None in (r_ls, r_hd) else ((r_hd < r_ls) if top else (r_hd > r_ls))
    pre = max(0, ls - 90)
    seg_c = c[a + pre:a + ls + 1]
    move = (c[a + hd] / min(seg_c) - 1) if top else (1 - c[a + hd] / max(seg_c))
    return {"vol_dry": vol_rs < vol_ls, "brk_vol": va > 0 and v[i] >= 1.5 * va, "div": bool(div), "move": move,
            "rs_div": None not in (r_hd, r_rs) and ((r_rs < r_hd) if top else (r_rs > r_hd))}


def trade(F, a, pat, top):
    """Wait for the first close beyond the neckline (extrapolated) within 30 bars of confirmation; enter next open."""
    conf, ls, t1, hd, t2, rs, height = pat
    c, o, h, l, atr = F["c"], F["o"], F["h"], F["l"], F["atr20"]
    opp = l if top else h
    y1, y2 = opp[a + t1], opp[a + t2]
    slope = (y2 - y1) / (t2 - t1)
    neck = lambda k: y2 + slope * (k - t2)
    rs_ext = (h if top else l)[a + rs]
    n = len(c)
    for k in range(conf, min(conf + 30, n - a - 1)):
        i = a + k
        if F["dn"][i + 1] - F["dn"][i] > 3: return None
        # invalidated if price takes out the right shoulder before breaking the neck
        if (top and h[i] > rs_ext) or ((not top) and l[i] < rs_ext): return None
        if (top and c[i] < neck(k)) or ((not top) and c[i] > neck(k)):
            if atr[i] is None: return None
            sg = -1 if top else 1                     # trade direction: short for a top
            ent = o[i + 1]
            stop = rs_ext + (0.25 * atr[i] if top else -0.25 * atr[i])
            tgt = neck(k) + sg * height
            risk = abs(stop - ent)
            if risk <= 0 or sg * (tgt - ent) <= 0: return None
            adv = F["vavg20i"][i] or 0
            slip = S.slippage(adv)
            fx = feats(F, a, pat, top, k)
            for m in range(i + 1, min(i + 61, n)):
                if F["dn"][m] - F["dn"][m - 1] > 3: break
                hit_stop = (sg == -1 and h[m] >= stop) or (sg == 1 and l[m] <= stop)
                hit_tgt = (sg == -1 and l[m] <= tgt) or (sg == 1 and h[m] >= tgt)
                if hit_stop:
                    px = max(o[m], stop) if sg == -1 else min(o[m], stop)
                    return dict(finish(F, i, ent, px, risk, sg, "stop", m - i, slip, rr=abs(tgt - ent) / risk), **fx)
                if hit_tgt:
                    px = tgt
                    return dict(finish(F, i, ent, px, risk, sg, "target", m - i, slip, rr=abs(tgt - ent) / risk), **fx)
            m = min(i + 60, n - 1)
            return dict(finish(F, i, ent, c[m], risk, sg, "time", m - i, slip, rr=abs(tgt - ent) / risk), **fx)
    return None


def finish(F, i, ent, px, risk, sg, why, bars, slip, rr):
    gross = sg * (px - ent)
    cost = (ent + px) * (FEE + slip)
    pnl = gross - cost
    return dict(reg=reg.get(F["dn"][i], 0), day=F["dn"][i], r=pnl / risk, pct=100 * pnl / ent, why=why, bars=bars, rr=rr)


res = defaultdict(list)
for s, F in data.items():
    if tok.get(s) in excl: continue
    for a, b in segments(F["dn"], F["c"]):
        if b - a < 260: continue
        for top in (True, False):
            for pat in find(F, a, b, top, PIV):
                i = a + pat[0]
                if i >= len(F["c"]) or (s, F["dn"][i]) not in rank: continue
                t = trade(F, a, pat, top)
                if t: res[("top" if top else "inverse")].append(t)

def show(name, lst):
    name = name[:18]
    for per, sel in (("<=2021", [t for t in lst if t["day"] < SPLIT]), ("2022+", [t for t in lst if t["day"] >= SPLIT])):
        if not sel: print(f"{name:18s} {per:7s} n=0"); continue
        R = [t["r"] for t in sel]; win = sum(1 for t in sel if t["r"] > 0)
        gw = sum(r for r in R if r > 0); gl = -sum(r for r in R if r <= 0)
        tg = sum(1 for t in sel if t["why"] == "target")
        print(f"{name:18s} {per:7s} n={len(sel):4d} win={100*win/len(sel):5.1f}% avgR={sum(R)/len(R):+.2f} medR={stt.median(R):+.2f} PF={gw/gl if gl else 99:.2f} "
              f"target hit={100*tg/len(sel):4.1f}% avg planned RR={sum(t['rr'] for t in sel)/len(sel):.2f} avg bars={sum(t['bars'] for t in sel)/len(sel):.0f}")
print(f"pivot={PIV} top{TOPN}")
for k in ("top", "inverse"):
    L = res[k]
    show(k, L)
    for name, f in (("volDry", lambda t: t["vol_dry"]), ("brkVol", lambda t: t["brk_vol"]), ("divLS-H", lambda t: t["div"]),
                    ("divH-RS", lambda t: t["rs_div"]), ("bigMove", lambda t: t["move"] >= 0.5), ("noBigMove", lambda t: t["move"] < 0.5),
                    ("div+vol", lambda t: t["div"] and t["brk_vol"]), ("dry+brk", lambda t: t["vol_dry"] and t["brk_vol"])):
        show(f"{k} {name}", [t for t in L if f(t)])
