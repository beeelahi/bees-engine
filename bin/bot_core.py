"""
Bee's Bot - shared core (Python 3.9 stdlib only).

The SAME code drives the backtest and the live paper trader, so what was tested is what trades.

Timing (no look-ahead):
  * Features and signals for day t use only candles up to and including day t's close.
  * Orders decided at t's close are filled at the OPEN of day t+1 (the next calendar day's candle).
  * Stops are fixed before day t+1 starts. If the next open gaps through the stop we exit at the open
    (worse than the stop); otherwise at the stop price.
  * If a pair's candles stop for >= 7 days (halt / delisting), the position is closed at the last
    close available, then marked with the reason "data_gap".
Costs: fee per side, size-dependent slippage, and a daily carry charge on shorts (perp funding/borrow).
"""

STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "EUR", "EURI", "AEUR",
                "GBP", "AUD", "TRY", "BRL", "PAX", "USD1", "XUSD", "USDS", "SUSD", "UST",
                "BFUSD", "USDE", "PYUSD", "RLUSD", "USDD", "FRAX", "LUSD", "GUSD", "U"}
# wrapped / pegged duplicates of other assets
EXCLUDE_BASES = {"WBTC", "WBETH", "BETH", "PAXG", "XAUT", "BNSOL", "WSTETH", "STETH"}
BREAK_DAYS = 7
JUMP_X = 20          # >= 20x close-to-close jump across a trading pause = redenomination / ticker reuse


def excluded_tokens(tokens):
    """Stablecoins, wrapped duplicates and leveraged tokens (BTCUP, ETHDOWN, BNBBULL ...)."""
    tokens = set(tokens)
    out = set()
    for t in tokens:
        if t in STABLE_BASES or t in EXCLUDE_BASES:
            out.add(t)
            continue
        for suf in ("UP", "DOWN", "BULL", "BEAR"):
            if t.endswith(suf) and len(t) > len(suf) and t[:-len(suf)] in tokens:
                out.add(t)
    return out


# ---------------------------------------------------------------- calendar helpers
def day_num(d):
    """'YYYY-MM-DD' -> integer day count (proleptic), cheap and exact."""
    y, m, dd = int(d[0:4]), int(d[5:7]), int(d[8:10])
    if m <= 2:
        y -= 1
        m += 12
    return 365 * y + y // 4 - y // 100 + y // 400 + (153 * (m - 3) + 2) // 5 + dd


# ---------------------------------------------------------------- features
def _sma(x, n):
    out = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _ema(x, n):
    """SMA-seeded EMA (TA-Lib convention)."""
    out = [None] * len(x)
    if len(x) < n:
        return out
    a = 2.0 / (n + 1)
    e = sum(x[:n]) / n
    out[n - 1] = e
    for i in range(n, len(x)):
        e = a * x[i] + (1 - a) * e
        out[i] = e
    return out


def _atr(h, l, c, n):
    """Wilder ATR, SMA-seeded (TA-Lib)."""
    m = len(c)
    out = [None] * m
    if m <= n:
        return out
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, m)]
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, m):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def _rsi(c, n):
    m = len(c)
    out = [None] * m
    if m <= n:
        return out
    g = l_ = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0.0)
        l_ += max(-d, 0.0)
    g /= n
    l_ /= n
    out[n] = 100.0 if l_ == 0 else 100.0 - 100.0 / (1 + g / l_)
    for i in range(n + 1, m):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0.0)) / n
        l_ = (l_ * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if l_ == 0 else 100.0 - 100.0 / (1 + g / l_)
    return out


def _prev_max(x, n):
    """max of the PREVIOUS n values (excludes today), monotonic deque."""
    from collections import deque
    out = [None] * len(x)
    dq = deque()
    for i in range(len(x)):
        if i >= n:
            out[i] = x[dq[0]]
        # push x[i] for future windows
        while dq and x[dq[-1]] <= x[i]:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - n:
            dq.popleft()
    return out


def _prev_min(x, n):
    neg = _prev_max([-v for v in x], n)
    return [None if v is None else -v for v in neg]


def segments(dates, closes=None):
    """Split a pair's history into continuous segments: a gap of >= BREAK_DAYS days, or a >= JUMP_X price
    discontinuity across at least one missing day (same rule as analytics.segment), restarts the series."""
    seg, start = [], 0
    for i in range(1, len(dates)):
        jump = False
        if closes is not None and dates[i] - dates[i - 1] >= 2 and closes[i - 1] and closes[i]:
            q = closes[i] / closes[i - 1]
            jump = q >= JUMP_X or q <= 1.0 / JUMP_X
        if dates[i] - dates[i - 1] >= BREAK_DAYS or jump:
            seg.append((start, i))
            start = i
    seg.append((start, len(dates)))
    return seg


FEATURE_SPEC = {
    "sma200": lambda o, h, l, c, v: _sma(c, 200),
    "sma50": lambda o, h, l, c, v: _sma(c, 50),
    "sma5": lambda o, h, l, c, v: _sma(c, 5),
    "ema20": lambda o, h, l, c, v: _ema(c, 20),
    "ema50": lambda o, h, l, c, v: _ema(c, 50),
    "atr20": lambda o, h, l, c, v: _atr(h, l, c, 20),
    "rsi2": lambda o, h, l, c, v: _rsi(c, 2),
    "rsi14": lambda o, h, l, c, v: _rsi(c, 14),
    "hh20": lambda o, h, l, c, v: _prev_max(h, 20),
    "hh55": lambda o, h, l, c, v: _prev_max(h, 55),
    "ll20": lambda o, h, l, c, v: _prev_min(l, 20),
    "ll55": lambda o, h, l, c, v: _prev_min(l, 55),
    "ll10": lambda o, h, l, c, v: _prev_min(l, 10),
    "hh10": lambda o, h, l, c, v: _prev_max(h, 10),
    "hh60": lambda o, h, l, c, v: _prev_max(h, 60),
    "ll60": lambda o, h, l, c, v: _prev_min(l, 60),
    "vavg20": lambda o, h, l, c, v: [None] + _sma(v, 20)[:-1],       # previous 20 days' mean volume
    "vavg20i": lambda o, h, l, c, v: _sma(v, 20),                     # incl. today (liquidity filter)
    "vavg60i": lambda o, h, l, c, v: _sma(v, 60),
    "macdh": lambda o, h, l, c, v: _macd_hist(c),
}


def _macd_hist(c, f=12, s=26, sig=9):
    """MACD(12,26,9) histogram, TA-Lib convention (signal EMA seeded on the first valid MACD values)."""
    ef, es = _ema(c, f), _ema(c, s)
    m = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    start = next((i for i, v in enumerate(m) if v is not None), None)
    out = [None] * len(c)
    if start is None:
        return out
    sg = _ema(m[start:], sig)
    for k, v in enumerate(sg):
        if v is not None:
            out[start + k] = m[start + k] - v
    return out


def build_features(rows):
    """rows: list of (date, o, h, l, c, vol_usdt) sorted by date.
    Returns dict of lists aligned to rows; indicators restart at each series break."""
    n = len(rows)
    dn = [day_num(r[0]) for r in rows]
    F = {"date": [r[0] for r in rows], "dn": dn,
         "o": [r[1] for r in rows], "h": [r[2] for r in rows], "l": [r[3] for r in rows],
         "c": [r[4] for r in rows], "v": [r[5] or 0.0 for r in rows], "seg_bar": [0] * n}
    for k in FEATURE_SPEC:
        F[k] = [None] * n
    for a, b in segments(dn, F["c"]):
        o, h, l, c, v = F["o"][a:b], F["h"][a:b], F["l"][a:b], F["c"][a:b], F["v"][a:b]
        for k, fn in FEATURE_SPEC.items():
            F[k][a:b] = fn(o, h, l, c, v)
        for i in range(a, b):
            F["seg_bar"][i] = i - a
    # 90-day and 20-day returns within segment
    F["ret90"] = [None] * n
    F["ret20"] = [None] * n
    for i in range(n):
        if F["seg_bar"][i] >= 90:
            F["ret90"][i] = F["c"][i] / F["c"][i - 90] - 1
        if F["seg_bar"][i] >= 20:
            F["ret20"][i] = F["c"][i] / F["c"][i - 20] - 1
    return F


SCORE_PARTS = [
    "Close above its 200-day average",
    "Close above its 50-day EMA",
    "20-day EMA above 50-day EMA",
    "50-day EMA above 200-day average",
    "Beating BTC over 90 days",
    "RSI(14) between 50 and 75",
    "Within 10% of its 55-day high",
    "Volume rising (20-day average above 60-day average)",
    "MACD histogram above zero",
    "Higher than 20 days ago",
]


def score_parts(F, i, btc90):
    """The 10 conditions behind the Top-50 score (each True/False), using data up to the close of bar i."""
    c = F["c"][i]
    g = lambda k: F[k][i]
    return [
        g("sma200") is not None and c > g("sma200"),
        g("ema50") is not None and c > g("ema50"),
        g("ema20") is not None and g("ema50") is not None and g("ema20") > g("ema50"),
        g("ema50") is not None and g("sma200") is not None and g("ema50") > g("sma200"),
        g("ret90") is not None and btc90 is not None and g("ret90") > btc90,
        g("rsi14") is not None and 50 <= g("rsi14") <= 75,
        g("hh55") is not None and c >= 0.9 * g("hh55"),
        g("vavg20i") is not None and g("vavg60i") is not None and g("vavg20i") > g("vavg60i"),
        g("macdh") is not None and g("macdh") > 0,
        g("ret20") is not None and g("ret20") > 0,
    ]
