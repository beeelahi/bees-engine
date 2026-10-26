"""
Indicator math for BinanceData. Pure functions over Python lists; None = not defined yet.
Conventions follow TA-Lib / TradingView. Every function is validated against TA-Lib
(or, where TA-Lib has no equivalent, against an independent implementation) in tests.

Each function documents `warm_from(...)`: the first bar index at which the value no longer
depends materially (< WARM_TOL) on where the history starts. Before that index a value is
'cold': stored, but flagged so screens can ignore it.
"""
import math

WARM_TOL = 0.001


def k_needed(alpha):
    return math.ceil(math.log(WARM_TOL) / math.log(1 - alpha))


# ---------------------------------------------------------------- moving averages
def sma(xs, n):
    out = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def sma_opt(xs, n):
    """SMA over a list that may start with None values (skips the None prefix)."""
    start = next((i for i, x in enumerate(xs) if x is not None), len(xs))
    out = [None] * len(xs)
    tail = sma(xs[start:], n)
    out[start:] = tail
    return out


def ema(xs, n, start=0):
    """EMA alpha=2/(n+1), seeded with the SMA of the first n values from `start`."""
    out = [None] * len(xs)
    if len(xs) - start < n:
        return out
    a = 2.0 / (n + 1)
    v = sum(xs[start:start + n]) / n
    out[start + n - 1] = v
    for i in range(start + n, len(xs)):
        v = a * xs[i] + (1 - a) * v
        out[i] = v
    return out


def ema_warm(n):
    return (n - 1) + k_needed(2.0 / (n + 1))


def rma(xs, n, start=0):
    """Wilder smoothing (alpha=1/n), seeded with the SMA of the first n values from `start`."""
    out = [None] * len(xs)
    if len(xs) - start < n:
        return out
    v = sum(xs[start:start + n]) / n
    out[start + n - 1] = v
    for i in range(start + n, len(xs)):
        v = (v * (n - 1) + xs[i]) / n
        out[i] = v
    return out


# ---------------------------------------------------------------- momentum
def rsi(c, n):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = [0.0] + [max(c[i] - c[i - 1], 0.0) for i in range(1, len(c))]
    l = [0.0] + [max(c[i - 1] - c[i], 0.0) for i in range(1, len(c))]
    ag, al = rma(g, n, 1), rma(l, n, 1)
    for i in range(n, len(c)):
        a, b = ag[i], al[i]
        out[i] = 100.0 if b == 0 else (0.0 if a == 0 else 100.0 - 100.0 / (1 + a / b))
    return out


def rsi_warm(n):
    return n + k_needed(1.0 / n)


def macd(c, f, s, g):
    ef, es = ema(c, f), ema(c, s)
    line = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    sig = ema([0.0 if v is None else v for v in line], g, start=s - 1)
    hist = [None if (m is None or q is None) else m - q for m, q in zip(line, sig)]
    return line, sig, hist


def macd_warm(f, s, g):
    return max(ema_warm(f), ema_warm(s), (s - 1) + (g - 1)) + k_needed(2.0 / (g + 1))


def roc(c, n):
    return [None if i < n or c[i - n] == 0 else (c[i] / c[i - n] - 1) * 100 for i in range(len(c))]


def stoch(h, l, c, k=14, ks=3, ds=3):
    """Slow stochastic (TA-Lib STOCH, SMA smoothing): returns (%K, %D)."""
    n = len(c)
    raw = [None] * n
    HH, LL = rolling_max(h, k), rolling_min(l, k)
    for i in range(k - 1, n):
        hh, ll = HH[i], LL[i]
        raw[i] = 0.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100
    kk = sma_opt(raw, ks)
    dd = sma_opt(kk, ds)
    return kk, dd


def stoch_rsi(c, rn=14, sn=14, ks=3, ds=3):
    """TradingView Stoch RSI: stochastic of RSI over sn bars, %K = SMA(ks), %D = SMA(ds)."""
    r = rsi(c, rn)
    n = len(c)
    raw = [None] * n
    for i in range(n):
        if i >= rn + sn - 1:
            w = r[i - sn + 1:i + 1]
            hh, ll = max(w), min(w)
            raw[i] = 0.0 if hh == ll else (r[i] - ll) / (hh - ll) * 100
    kk = sma_opt(raw, ks)
    dd = sma_opt(kk, ds)
    return kk, dd


def stoch_rsi_warm(rn=14, sn=14, ks=3, ds=3):
    return rsi_warm(rn) + (sn - 1) + (ks - 1) + (ds - 1)


# ---------------------------------------------------------------- volatility
def true_range(h, l, c):
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]


def atr(h, l, c, n=14):
    """TA-Lib ATR: Wilder smoothing of TR, seeded with the SMA of TR[1..n]."""
    return rma(true_range(h, l, c), n, 1)


def atr_warm(n=14):
    return n + k_needed(1.0 / n)


def bbands(c, n=20, k=2.0):
    """Bollinger Bands, population standard deviation (TA-Lib / TradingView)."""
    mid = sma(c, n)
    up, lo = [None] * len(c), [None] * len(c)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(max(sum((x - m) ** 2 for x in w) / n, 0.0))
        up[i], lo[i] = m + k * sd, m - k * sd
    return mid, up, lo


def pct_rank(xs, n):
    """Percentile rank (0-100) of xs[i] within the last n values (inclusive)."""
    out = [None] * len(xs)
    for i in range(len(xs)):
        if i >= n - 1 and xs[i] is not None:
            w = [x for x in xs[i - n + 1:i + 1] if x is not None]
            if len(w) == n:
                out[i] = 100.0 * sum(1 for x in w if x <= xs[i]) / n
    return out


def donchian(h, l, n):
    """Channel of the PREVIOUS n bars (excludes the current bar, so a close above = breakout)."""
    return rolling_max(h, n, include_current=False), rolling_min(l, n, include_current=False)


def realized_vol(c, n, periods_per_year):
    """Annualized stdev (sample) of log returns over n bars, in %."""
    lr = [None] + [math.log(c[i] / c[i - 1]) if c[i - 1] > 0 and c[i] > 0 else None for i in range(1, len(c))]
    out = [None] * len(c)
    for i in range(n, len(c)):
        w = lr[i - n + 1:i + 1]
        if None in w:
            continue
        m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / (n - 1)) * math.sqrt(periods_per_year) * 100
    return out


# ---------------------------------------------------------------- trend
def adx(h, l, c, n=14):
    """Wilder +DI, -DI, ADX matching TA-Lib (PLUS_DI, MINUS_DI, ADX)."""
    m = len(c)
    pdi, mdi, adx_ = [None] * m, [None] * m, [None] * m
    if m <= 2 * n:
        return pdi, mdi, adx_
    pdm = [0.0] * m
    mdm = [0.0] * m
    tr = [0.0] * m
    for i in range(1, m):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    # TA-Lib: first smoothed value = sum of the first n-1 raw values (bars 1..n-1), then
    # S = S - S/n + x  from bar n onward; DI defined from bar n.
    sp, sm, st = sum(pdm[1:n]), sum(mdm[1:n]), sum(tr[1:n])
    dx = [None] * m
    for i in range(n, m):
        sp = sp - sp / n + pdm[i]
        sm = sm - sm / n + mdm[i]
        st = st - st / n + tr[i]
        if st > 0:
            pdi[i], mdi[i] = 100 * sp / st, 100 * sm / st
            s = pdi[i] + mdi[i]
            dx[i] = 0.0 if s == 0 else 100 * abs(pdi[i] - mdi[i]) / s
        else:
            pdi[i] = mdi[i] = dx[i] = 0.0
    # ADX: first value = mean of the first n DX values, then Wilder smoothing.
    first = n + n - 1
    if first < m:
        v = sum(dx[n:first + 1]) / n
        adx_[first] = v
        for i in range(first + 1, m):
            v = (v * (n - 1) + dx[i]) / n
            adx_[i] = v
    return pdi, mdi, adx_


def adx_warm(n=14):
    return 2 * n - 1 + k_needed(1.0 / n)


def supertrend(h, l, c, n=10, mult=3.0):
    """TradingView ta.supertrend(mult, n) with RMA ATR. Returns (line, direction) where
    direction = +1 uptrend (line below price), -1 downtrend."""
    m = len(c)
    a = rma(true_range(h, l, c), n, 0)       # TradingView ta.atr: RMA of TR, TR[0] = h-l
    line, dirn = [None] * m, [None] * m
    ub_prev = lb_prev = st_prev = None
    for i in range(m):
        if a[i] is None:
            continue
        hl2 = (h[i] + l[i]) / 2
        ub, lb = hl2 + mult * a[i], hl2 - mult * a[i]
        if lb_prev is not None:
            lb = lb if (lb > lb_prev or c[i - 1] < lb_prev) else lb_prev
            ub = ub if (ub < ub_prev or c[i - 1] > ub_prev) else ub_prev
        if st_prev is None:
            d = -1                             # Pine: na(atr[1]) -> downtrend on the first defined bar
        elif st_prev == ub_prev:
            d = 1 if c[i] > ub else -1
        else:
            d = -1 if c[i] < lb else 1
        st = lb if d == 1 else ub
        line[i], dirn[i] = st, d
        ub_prev, lb_prev, st_prev = ub, lb, st
    return line, dirn


def supertrend_warm(n=10):
    return n + k_needed(1.0 / n)


def ichimoku(h, l, t=9, k=26, s=52, disp=26):
    """Tenkan, Kijun, and the cloud (Senkou A/B) AS IT STANDS AT EACH BAR (i.e. values computed
    `disp` bars earlier, which is how the cloud is drawn under the current candle)."""
    m = len(h)

    def mid(n):
        hh, ll = rolling_max(h, n), rolling_min(l, n)
        return [None if a is None else (a + b) / 2 for a, b in zip(hh, ll)]
    tk, kj, sb_raw = mid(t), mid(k), mid(s)
    sa_raw = [None if (a is None or b is None) else (a + b) / 2 for a, b in zip(tk, kj)]
    sa = [None] * m
    sb = [None] * m
    for i in range(disp, m):
        sa[i], sb[i] = sa_raw[i - disp], sb_raw[i - disp]
    return tk, kj, sa, sb


# ---------------------------------------------------------------- volume
def mfi(h, l, c, v, n=14):
    """TA-Lib MFI over typical price and base volume."""
    m = len(c)
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(m)]
    pos = [0.0] * m
    neg = [0.0] * m
    for i in range(1, m):
        mf = tp[i] * v[i]
        if tp[i] > tp[i - 1]:
            pos[i] = mf
        elif tp[i] < tp[i - 1]:
            neg[i] = mf
    out = [None] * m
    for i in range(n, m):
        p, q = sum(pos[i - n + 1:i + 1]), sum(neg[i - n + 1:i + 1])
        out[i] = 100.0 if q == 0 else (0.0 if p == 0 else 100 - 100 / (1 + p / q))
    return out


def cmf(h, l, c, v, n=20):
    m = len(c)
    mfv = [0.0 if h[i] == l[i] else ((c[i] - l[i]) - (h[i] - c[i])) / (h[i] - l[i]) * v[i] for i in range(m)]
    out = [None] * m
    for i in range(n - 1, m):
        sv = sum(v[i - n + 1:i + 1])
        out[i] = None if sv == 0 else sum(mfv[i - n + 1:i + 1]) / sv
    return out


def obv(c, v):
    out, acc = [0.0] * len(c), 0.0
    for i in range(1, len(c)):
        acc += v[i] if c[i] > c[i - 1] else (-v[i] if c[i] < c[i - 1] else 0.0)
        out[i] = acc
    return out


def rvol(v, n):
    """Volume / average of the PREVIOUS n bars, plus z-score vs the same window."""
    m = len(v)
    base, rv, z = [None] * m, [None] * m, [None] * m
    for i in range(n, m):
        w = v[i - n:i]
        mu = sum(w) / n
        base[i] = mu
        if mu > 0:
            rv[i] = v[i] / mu
        sd = math.sqrt(sum((x - mu) ** 2 for x in w) / n)
        if sd > 0:
            z[i] = (v[i] - mu) / sd
    return base, rv, z


# ---------------------------------------------------------------- position
def running_high(h):
    out, mx, since, idx = [None] * len(h), -math.inf, [None] * len(h), 0
    for i, x in enumerate(h):
        if x >= mx:
            mx, idx = x, i
        out[i], since[i] = mx, i - idx
    return out, since


def window_high_low(h, l, n):
    return rolling_max(h, n), rolling_min(l, n)


def streak(c):
    """Consecutive higher (+) / lower (-) closes ending at each bar."""
    out = [0] * len(c)
    for i in range(1, len(c)):
        if c[i] > c[i - 1]:
            out[i] = out[i - 1] + 1 if out[i - 1] > 0 else 1
        elif c[i] < c[i - 1]:
            out[i] = out[i - 1] - 1 if out[i - 1] < 0 else -1
    return out


# ---------------------------------------------------------------- fast rolling windows
from collections import deque
from bisect import bisect_right, insort, bisect_left


def rolling_max(xs, n, include_current=True):
    """Max of the last n values (ending at i, or at i-1 when include_current=False). O(len)."""
    out, dq = [None] * len(xs), deque()
    for i, x in enumerate(xs):
        j = i if include_current else i - 1
        if j >= 0:
            y = xs[j]
            while dq and xs[dq[-1]] <= y:
                dq.pop()
            dq.append(j)
            while dq[0] <= j - n:
                dq.popleft()
            if j >= n - 1:
                out[i] = xs[dq[0]]
    return out


def rolling_min(xs, n, include_current=True):
    neg = rolling_max([-x for x in xs], n, include_current)
    return [None if v is None else -v for v in neg]


def rolling_pct_rank(xs, n):
    """Percentile rank (0-100, share of window <= current) of xs[i] in the last n values; None-safe."""
    out, win, srt = [None] * len(xs), deque(), []
    for i, x in enumerate(xs):
        win.append(x)
        if x is not None:
            insort(srt, x)
        if len(win) > n:
            old = win.popleft()
            if old is not None:
                del srt[bisect_left(srt, old)]
        if len(win) == n and x is not None and len(srt) == n:
            out[i] = 100.0 * bisect_right(srt, x) / n
    return out
