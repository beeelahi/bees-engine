"""
Bee's Bot - strategies and portfolio simulator (Python 3.9 stdlib only).
Used both for backtests and, day by day, for the live paper account.
"""
import math
from bot_core import build_features, excluded_tokens, score_parts

# ---------------------------------------------------------------- costs and risk (defaults)
DEFAULTS = dict(
    start_equity=10000.0,
    risk=0.01,            # equity risked per trade (distance entry -> initial stop)
    max_pos=10,           # simultaneous positions
    max_pos_frac=0.20,    # max notional per position, fraction of equity
    max_gross=1.0,        # max total notional (long + short), fraction of equity -> no leverage
    max_adv_frac=0.005,   # max notional per position, fraction of 20-day average daily volume
    min_vol=5e6,          # min 20-day average daily volume (USDT) to open a trade
    max_vol=0,            # if > 0: max 20-day average daily volume (small/mid-cap bots)
    top_n=0,              # if > 0: only the top-N pairs by 20-day average volume that day
    rs=False,             # longs must beat BTC over 90 days, shorts must lag it
    rank="adv",           # "adv": top_n by liquidity; "score": top_n by indicator score (see bot_core.SCORE_PARTS)
    pool=0,               # with rank="score": first keep the `pool` most liquid pairs (0 = every pair above min_vol)
    min_score=0,          # with rank="score": minimum score to be tradable
    fee=0.001,            # per side (Binance base rate)
    stop_slip=0.001,      # extra slippage on stop exits
    short_carry=0.0002,   # per day on short notional (funding/borrow, conservative)
)


def slippage(adv):
    if adv >= 20e6:
        return 0.0005
    if adv >= 5e6:
        return 0.0015
    if adv >= 1e6:
        return 0.003
    return 0.006                       # thin small caps fill worse


# ---------------------------------------------------------------- strategies
class Strategy:
    """signal(F, i) -> (side, priority) or None, decided at close of bar i.
    init_stop(F, i, side, px) -> stop price.  manage(pos, F, i) -> (new_stop, exit_next_open)."""
    name = "base"
    sides = ("long", "short")

    def __init__(self, **p):
        self.p = dict(self.defaults)
        self.p.update(p)

    def label(self):
        return self.name + "(" + ",".join(f"{k}={v}" for k, v in sorted(self.p.items())) + ")"


def _ok(*vals):
    return all(v is not None for v in vals)


class Breakout(Strategy):
    """Donchian/Turtle-style trend breakout with trend filter, ATR stop and Donchian/chandelier trail."""
    name = "breakout"
    defaults = dict(n=55, exit_n=20, k=2.0, trail=3.0, trend=True, rvol=0.0, sides="ls", ttl=0,
                    be=0.0, pyr=0, pyr_step=1.0, exit_off=False, tp_r=0.0, tp_frac=0.5, be_after_tp=True,
                    max_ext=0.0)
    # max_ext: skip entries whose close is more than max_ext x ATR above the 20-day EMA (0 = off)
    # tp_r / tp_frac: take `tp_frac` of the position off at entry + tp_r x initial risk (limit order, intraday);
    #   be_after_tp: then move the stop on the rest to break-even
    # be: move the stop to break-even once the best close is be x initial risk above entry (0 = off)
    # pyr / pyr_step: add up to `pyr` extra units, one each time the close rises pyr_step x ATR above the last add
    # exit_off: no Donchian exit (trailing stop only)

    def signal(self, F, i, regime):
        p = self.p
        c = F["c"][i]
        hh, ll = F["hh%d" % p["n"]][i], F["ll%d" % p["n"]][i]
        s200, atr = F["sma200"][i], F["atr20"][i]
        if not _ok(hh, ll, s200, atr):
            return None
        if p["rvol"]:
            va = F["vavg20"][i]
            if not va or F["v"][i] / va < p["rvol"]:
                return None
        if p["max_ext"] and F["ema20"][i] is not None and (c - F["ema20"][i]) > p["max_ext"] * atr:
            return None
        if "l" in p["sides"] and regime == 1 and c > hh and (not p["trend"] or c > s200):
            return ("long", F["ret90"][i] or 0.0)
        if "s" in p["sides"] and regime == -1 and c < ll and (not p["trend"] or c < s200):
            return ("short", -(F["ret90"][i] or 0.0))
        return None

    def init_stop(self, F, i, side, px):
        a = F["atr20"][i] * self.p["k"]
        return px - a if side == "long" else px + a

    def manage(self, pos, F, i):
        p = self.p
        c, atr = F["c"][i], F["atr20"][i]
        stop = pos["stop"]
        if pos["side"] == "long":
            if p["trail"] and atr:
                stop = max(stop, pos["best"] - p["trail"] * atr)
            if p["be"] and pos["best"] >= pos["entry"] + p["be"] * (pos["entry"] - pos["stop0"]):
                stop = max(stop, pos["entry"])
            ex = None if p["exit_off"] else F["ll%d" % p["exit_n"]][i]
            out = ex is not None and c < ex
        else:
            if p["trail"] and atr:
                stop = min(stop, pos["best"] + p["trail"] * atr)
            ex = F["hh%d" % p["exit_n"]][i]
            out = ex is not None and c > ex
        if p["ttl"] and pos["bars"] >= p["ttl"]:
            out = True
        return stop, out


class MeanRev(Strategy):
    """Connors RSI(2) pullback in the direction of the long-term trend."""
    name = "meanrev"
    defaults = dict(lo=10, hi=90, k=2.5, ttl=10, sides="ls")

    def signal(self, F, i, regime):
        p = self.p
        c, r2, s200, atr = F["c"][i], F["rsi2"][i], F["sma200"][i], F["atr20"][i]
        if not _ok(r2, s200, atr, F["sma5"][i]):
            return None
        if "l" in p["sides"] and regime == 1 and c > s200 and r2 < p["lo"]:
            return ("long", -r2)
        if "s" in p["sides"] and regime == -1 and c < s200 and r2 > p["hi"]:
            return ("short", r2 - 100)
        return None

    def init_stop(self, F, i, side, px):
        a = F["atr20"][i] * self.p["k"]
        return px - a if side == "long" else px + a

    def manage(self, pos, F, i):
        c, s5 = F["c"][i], F["sma5"][i]
        out = (c > s5) if pos["side"] == "long" else (c < s5)
        if pos["bars"] >= self.p["ttl"]:
            out = True
        return pos["stop"], out


class Pullback(Strategy):
    """Trend pullback: EMA20 > EMA50 > SMA200 (uptrend), price dips to EMA20 then closes back above it."""
    name = "pullback"
    defaults = dict(k=2.0, trail=3.0, sides="ls", ttl=0)

    def signal(self, F, i, regime):
        p = self.p
        if i < 1:
            return None
        c, l, h = F["c"][i], F["l"][i], F["h"][i]
        e20, e50, s200, atr = F["ema20"][i], F["ema50"][i], F["sma200"][i], F["atr20"][i]
        if not _ok(e20, e50, s200, atr, F["ret90"][i]):
            return None
        if "l" in p["sides"] and regime == 1 and e20 > e50 > s200 and l <= e20 < c and c > F["o"][i]:
            return ("long", F["ret90"][i])
        if "s" in p["sides"] and regime == -1 and e20 < e50 < s200 and h >= e20 > c and c < F["o"][i]:
            return ("short", -F["ret90"][i])
        return None

    def init_stop(self, F, i, side, px):
        a = F["atr20"][i] * self.p["k"]
        return px - a if side == "long" else px + a

    def manage(self, pos, F, i):
        p = self.p
        c, atr, e50 = F["c"][i], F["atr20"][i], F["ema50"][i]
        stop = pos["stop"]
        if pos["side"] == "long":
            stop = max(stop, pos["best"] - p["trail"] * atr)
            out = c < e50
        else:
            stop = min(stop, pos["best"] + p["trail"] * atr)
            out = c > e50
        if p["ttl"] and pos["bars"] >= p["ttl"]:
            out = True
        return stop, out


class Accumulation(Strategy):
    """Accumulation pre-breakout (experimental): volume building (20-day average >= vol_x times the 60-day
    average) while price sits in the top of a contained 60-day range, before it breaks out. No BTC filter."""
    name = "accumulation"
    defaults = dict(vol_x=2.0, pos=0.7, max_range=2.5, k=2.0, ttl=20, exit_n=10)

    def signal(self, F, i, regime):
        p = self.p
        c, hh, ll, atr = F["c"][i], F["hh60"][i], F["ll60"][i], F["atr20"][i]
        v20, v60 = F["vavg20i"][i], F["vavg60i"][i]
        if not _ok(hh, ll, atr, v20, v60) or ll <= 0 or v60 <= 0 or hh <= ll:
            return None
        if (v20 / v60 >= p["vol_x"] and (c - ll) / (hh - ll) >= p["pos"] and c <= hh
                and hh / ll <= p["max_range"]):
            return ("long", v20 / v60)
        return None

    def init_stop(self, F, i, side, px):
        return px - F["atr20"][i] * self.p["k"]

    def manage(self, pos, F, i):
        ex = F["ll%d" % self.p["exit_n"]][i]
        out = (ex is not None and F["c"][i] < ex) or pos["bars"] >= self.p["ttl"]
        return pos["stop"], out


STRATS = {"accumulation": Accumulation, "breakout": Breakout, "meanrev": MeanRev, "pullback": Pullback}


# ---------------------------------------------------------------- data
def prepare(daily_rows, tokens):
    """daily_rows: iterable of (symbol, date, o, h, l, c, vol_usdt) sorted by symbol, date.
    tokens: symbol -> base token.  Returns {symbol: F}, and the BTC regime by day number."""
    bad = excluded_tokens(tokens.values())
    by = {}
    cur, buf = None, []
    for r in daily_rows:
        if r[0] != cur:
            if buf and tokens.get(cur) not in bad:
                by[cur] = buf
            cur, buf = r[0], []
        buf.append(r[1:])
    if buf and tokens.get(cur) not in bad:
        by[cur] = buf
    data = {}
    for sym, rows in by.items():
        last = [r_[4] for r_ in rows[-120:]]
        if len(last) >= 60 and all(0.97 <= x <= 1.03 for x in last):
            continue                          # behaves like a dollar stablecoin: never tradable
        F = build_features(rows)
        F["idx"] = {d: i for i, d in enumerate(F["dn"])}
        data[sym] = F
    regime = {}
    B = data.get("BTCUSDT")
    for i, d in enumerate(B["dn"]):
        s = B["sma200"][i]
        regime[d] = 0 if s is None else (1 if B["c"][i] > s else -1)
    return data, regime


# ---------------------------------------------------------------- simulator
def simulate(data, regime, strat, start, end, cfg=None, state=None, log_signals=False):
    """Day-by-day portfolio simulation from day number `start` to `end` (inclusive).
    Returns dict(trades, equity_curve, positions, pending, cash_equity)."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    st = state or {"equity": c["start_equity"], "positions": [], "pending": [], "trades": [], "curve": []}
    realized = st["equity"]
    positions, pending, trades, curve = st["positions"], st["pending"], st["trades"], st["curve"]

    def mtm(d):
        u = 0.0
        for p in positions:
            F = data[p["sym"]]
            j = p["last_i"]
            px = F["c"][j]
            u += p["qty"] * (px - p["entry"]) if p["side"] == "long" else p["qty"] * (p["entry"] - px)
        return realized + u

    def close(p, px, d, reason, slip, partial=False):
        nonlocal realized
        fill = px * (1 - slip) if p["side"] == "long" else px * (1 + slip)
        gross = p["qty"] * (fill - p["entry"]) if p["side"] == "long" else p["qty"] * (p["entry"] - fill)
        fee = p["qty"] * fill * c["fee"]
        pnl = gross - fee - p["cost"]
        realized += gross - fee   # entry fee and carry already deducted when incurred
        t = dict(sym=p["sym"], side=p["side"], entry_day=p["entry_day"], exit_day=d, entry=p["entry"],
                 exit=fill, qty=p["qty"], stop0=p["stop0"], risk_amt=p["risk_amt"], pnl=pnl,
                 r=pnl / p["risk_amt"] if p["risk_amt"] else 0.0, reason=reason, bars=p["bars"],
                 cid=(p["sym"], p["entry_day"], p.get("unit", 0)), partial=partial)
        trades.append(t)

    for d in range(start, end + 1):
        eq_prev = mtm(d)
        # 1) fill pending entries at today's open
        new_pending = []
        for o in pending:
            F = data[o["sym"]]
            i = F["idx"].get(d)
            held_now = any(p["sym"] == o["sym"] for p in positions)
            if i is None or o["day"] != d - 1 or held_now != bool(o.get("add")):
                continue
            if not o.get("add") and len({p["sym"] for p in positions if not p["exit_flag"]}) >= c["max_pos"]:
                continue
            adv = F["vavg20i"][i - 1] or 0.0
            slip = slippage(adv)
            px = F["o"][i] * (1 + slip) if o["side"] == "long" else F["o"][i] * (1 - slip)
            stop = o["stop_dist"]
            stop = px - stop if o["side"] == "long" else px + stop
            dist = abs(px - stop)
            if dist <= 0:
                continue
            risk_amt = eq_prev * c["risk"]
            qty = risk_amt / dist
            gross_now = sum(p["qty"] * data[p["sym"]]["c"][p["last_i"]] for p in positions)
            cap = min(eq_prev * c["max_pos_frac"], adv * c["max_adv_frac"], eq_prev * c["max_gross"] - gross_now)
            if qty * px > cap:
                qty = cap / px
            if qty * px < eq_prev * 0.02:
                continue
            fee = qty * px * c["fee"]
            realized -= fee
            positions.append(dict(sym=o["sym"], side=o["side"], entry=px, qty=qty, stop=stop, stop0=stop,
                                  risk_amt=qty * dist, entry_day=d, bars=0, best=px, cost=fee,
                                  last_i=i, exit_flag=False, unit=o.get("unit", 0)))
            if o.get("add"):                      # all units share the tightest stop (Turtle rule)
                grp = [p for p in positions if p["sym"] == o["sym"]]
                g = max(p["stop"] for p in grp) if o["side"] == "long" else min(p["stop"] for p in grp)
                for p in grp:
                    p["stop"] = g
        pending = new_pending
        # 2) exits at today's open (decided yesterday), stops intraday, data gaps
        still = []
        for p in positions:
            F = data[p["sym"]]
            i = F["idx"].get(d)
            if i is None:
                if d - F["dn"][p["last_i"]] >= 7:
                    close(p, F["c"][p["last_i"]], d, "data_gap", 0.01)
                else:
                    still.append(p)
                continue
            adv = F["vavg20i"][p["last_i"]] or 0.0
            slip = slippage(adv)
            o_, h_, l_ = F["o"][i], F["h"][i], F["l"][i]
            if p["exit_flag"] and p["entry_day"] != d:
                p["last_i"] = i
                close(p, o_, d, "exit_rule", slip)
                continue
            if p["side"] == "long":
                if p["entry_day"] != d and o_ <= p["stop"]:
                    p["last_i"] = i
                    close(p, o_, d, "stop_gap", slip + c["stop_slip"])
                    continue
                if l_ <= p["stop"]:
                    p["last_i"] = i
                    close(p, p["stop"], d, "stop", slip + c["stop_slip"])
                    continue
            else:
                if p["entry_day"] != d and o_ >= p["stop"]:
                    p["last_i"] = i
                    close(p, o_, d, "stop_gap", slip + c["stop_slip"])
                    continue
                if h_ >= p["stop"]:
                    p["last_i"] = i
                    close(p, p["stop"], d, "stop", slip + c["stop_slip"])
                    continue
            p["last_i"] = i
            tp_r = strat.p.get("tp_r", 0)
            if tp_r and not p.get("tp_done"):
                r0 = abs(p["entry"] - p["stop0"])
                tp = p["entry"] + tp_r * r0 if p["side"] == "long" else p["entry"] - tp_r * r0
                hit = (h_ >= tp) if p["side"] == "long" else (l_ <= tp)
                if hit:
                    fillpx = max(tp, o_) if p["side"] == "long" and p["entry_day"] != d else tp
                    if p["side"] == "short" and p["entry_day"] != d:
                        fillpx = min(tp, o_)
                    part = dict(p)
                    part["qty"] = p["qty"] * strat.p["tp_frac"]
                    part["cost"] = p["cost"] * strat.p["tp_frac"]
                    part["risk_amt"] = 0.0
                    close(part, fillpx, d, "take_profit", 0.0005, partial=True)
                    p["qty"] -= part["qty"]
                    p["cost"] -= part["cost"]
                    p["tp_done"] = True
                    if strat.p.get("be_after_tp", True):
                        p["stop"] = max(p["stop"], p["entry"]) if p["side"] == "long" else min(p["stop"], p["entry"])
            still.append(p)
        positions[:] = still
        # 3) at today's close: carry, manage open positions
        for p in positions:
            F = data[p["sym"]]
            i = p["last_i"]
            if F["dn"][i] != d:
                continue
            p["bars"] += 1
            if p["side"] == "short":
                carry = p["qty"] * F["c"][i] * c["short_carry"]
                realized -= carry
                p["cost"] += carry
            p["best"] = max(p["best"], F["c"][i]) if p["side"] == "long" else min(p["best"], F["c"][i])
            p["stop"], p["exit_flag"] = strat.manage(p, F, i)
        # units of one pair move together: shared (tightest) stop, shared exit; schedule pyramid adds
        pyr = strat.p.get("pyr", 0)
        groups = {}
        for p in positions:
            groups.setdefault(p["sym"], []).append(p)
        for sym, grp in groups.items():
            if len(grp) > 1:
                lg = grp[0]["side"] == "long"
                g = max(p["stop"] for p in grp) if lg else min(p["stop"] for p in grp)
                ex = any(p["exit_flag"] for p in grp)
                for p in grp:
                    p["stop"], p["exit_flag"] = g, ex
            if pyr and not grp[0]["exit_flag"]:
                F = data[sym]
                i = grp[0]["last_i"]
                if F["dn"][i] == d and len(grp) <= pyr and F["atr20"][i]:
                    last = max(grp, key=lambda p: p["unit"])
                    up = F["c"][i] - last["entry"] if last["side"] == "long" else last["entry"] - F["c"][i]
                    if up >= strat.p["pyr_step"] * F["atr20"][i]:
                        dist = abs(F["c"][i] - strat.init_stop(F, i, last["side"], F["c"][i]))
                        pending.append(dict(sym=sym, side=last["side"], day=d, stop_dist=dist, add=True,
                                            unit=last["unit"] + 1))
        # 4) new signals at today's close -> orders for tomorrow's open
        rg = regime.get(d, 0)
        slots = c["max_pos"] - len({p["sym"] for p in positions if not p["exit_flag"]})
        if slots > 0 and (rg != 0 or getattr(strat, "name", "") == "accumulation"):
            held = {p["sym"] for p in positions}
            elig = []
            for sym, F in data.items():
                i = F["idx"].get(d)
                if i is None or F["seg_bar"][i] < c.get("min_bars", 200) or (F["vavg20i"][i] or 0) < c["min_vol"]:
                    continue
                if c["max_vol"] and (F["vavg20i"][i] or 0) > c["max_vol"]:
                    continue
                elig.append((-(F["vavg20i"][i]), sym, i))
            B = data["BTCUSDT"]
            bi = B["idx"].get(d)
            btc90 = B["ret90"][bi] if bi is not None else None
            elig.sort()
            if c["rank"] == "score":
                if c["pool"]:
                    elig = elig[:c["pool"]]
                sc = [(-sum(score_parts(data[sym], i, btc90)), adv, sym, i) for adv, sym, i in elig]
                sc.sort()
                sc = [x for x in sc if -x[0] >= c["min_score"]]
                elig = [(adv, sym, i) for _, adv, sym, i in (sc[:c["top_n"]] if c["top_n"] else sc)]
            elif c["top_n"]:
                elig = elig[:c["top_n"]]
            cands = []
            for _, sym, i in elig:
                F = data[sym]
                if sym in held:
                    continue
                s = strat.signal(F, i, rg)
                if s and c["rs"] and sym != "BTCUSDT":
                    r90 = F["ret90"][i]
                    if r90 is None or btc90 is None or (s[0] == "long" and r90 <= btc90) or (s[0] == "short" and r90 >= btc90):
                        s = None
                if s:
                    stop = strat.init_stop(F, i, s[0], F["c"][i])
                    cands.append((-s[1], sym, s[0], abs(F["c"][i] - stop)))
            cands.sort()
            for _, sym, side, dist in cands[:slots]:
                pending.append(dict(sym=sym, side=side, day=d, stop_dist=dist))
        curve.append((d, mtm(d)))
    st.update(equity=realized, positions=positions, pending=pending, trades=trades, curve=curve)
    return st


# ---------------------------------------------------------------- metrics
def campaigns(trades):
    """Merge partial exits into their parent trade: one row per position."""
    g = {}
    for t in trades:
        k = t.get("cid") or (t["sym"], t["entry_day"], 0)
        if k not in g:
            g[k] = dict(t)
            continue
        a = g[k]
        a["pnl"] += t["pnl"]
        a["risk_amt"] = max(a["risk_amt"], t["risk_amt"])
        if not t.get("partial"):
            for f in ("exit_day", "exit", "reason", "bars", "qty", "stop0", "entry"):
                a[f] = t[f]
        a["partial"] = False
    out = list(g.values())
    for a in out:
        a["r"] = a["pnl"] / a["risk_amt"] if a["risk_amt"] else 0.0
    return out


def metrics(st, start, end):
    tr = [t for t in campaigns(st["trades"]) if start <= t["entry_day"] <= end]
    curve = [(d, e) for d, e in st["curve"] if start <= d <= end]
    out = {"trades": len(tr)}
    if not curve:
        return out
    e0, e1 = curve[0][1], curve[-1][1]
    years = max((curve[-1][0] - curve[0][0]) / 365.25, 1e-9)
    peak, mdd = -1e18, 0.0
    rets = []
    prev = None
    for _, e in curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
        if prev:
            rets.append(e / prev - 1)
        prev = e
    mu = sum(rets) / len(rets) if rets else 0
    sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / max(len(rets) - 1, 1)) if rets else 0
    out.update(cagr=(e1 / e0) ** (1 / years) - 1 if e0 > 0 and e1 > 0 else -1, total=e1 / e0 - 1,
               maxdd=mdd, sharpe=(mu / sd * math.sqrt(365)) if sd else 0)
    if tr:
        wins = [t for t in tr if t["pnl"] > 0]
        loss = [t for t in tr if t["pnl"] <= 0]
        gw, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in loss)
        streak = worst = 0
        for t in sorted(tr, key=lambda t: t["exit_day"]):
            streak = streak + 1 if t["pnl"] <= 0 else 0
            worst = max(worst, streak)
        out.update(win=len(wins) / len(tr), avg_r=sum(t["r"] for t in tr) / len(tr),
                   avg_win_r=sum(t["r"] for t in wins) / len(wins) if wins else 0,
                   avg_loss_r=sum(t["r"] for t in loss) / len(loss) if loss else 0,
                   pf=gw / gl if gl else float("inf"), worst_streak=worst,
                   avg_bars=sum(t["bars"] for t in tr) / len(tr),
                   longs=sum(1 for t in tr if t["side"] == "long"))
    return out
