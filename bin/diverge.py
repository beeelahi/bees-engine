"""
Daily divergences: price against RSI and MACD (Python 3.9 stdlib only).

Bullish divergence (a sign that selling is drying up, often near a bottom):
  * price makes a LOWER LOW: a swing low (lower than the P candles on each side) under the previous swing low, which
    was MIN_GAP..MAX_GAP candles earlier, with no candle between them lower than the new low;
  * RSI(14) makes a HIGHER LOW at the same two candles (by at least RSI_MIN points), and the first RSI low was in
    the weak zone (under RSI_ZONE_BULL);
  * MACD(12,26,9) agrees when its histogram makes a higher low at the same two candles.
Bearish divergence is the mirror image at swing highs (higher high, lower RSI high from above RSI_ZONE_BEAR, lower MACD
histogram high).
The signal is known only when the new swing is confirmed, P candles after it (no look-ahead): `events` reports it on
that day. "Triple" = price + RSI + MACD all agree (the strongest form); "rsi" = price + RSI only.
"""

P, MIN_GAP, MAX_GAP, RSI_MIN = 3, 8, 60, 1.0
RSI_ZONE_BULL, RSI_ZONE_BEAR = 40.0, 60.0


def rsi(c, n=14):
    """Wilder's RSI; None until there are n changes."""
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = l_ = 0.0
    for k in range(1, n + 1):
        d = c[k] - c[k - 1]
        g, l_ = g + max(d, 0.0), l_ + max(-d, 0.0)
    g, l_ = g / n, l_ / n
    out[n] = 100.0 if l_ == 0 else 100.0 - 100.0 / (1 + g / l_)
    for k in range(n + 1, len(c)):
        d = c[k] - c[k - 1]
        g, l_ = (g * (n - 1) + max(d, 0.0)) / n, (l_ * (n - 1) + max(-d, 0.0)) / n
        out[k] = 100.0 if l_ == 0 else 100.0 - 100.0 / (1 + g / l_)
    return out


def _ema(x, n):
    a, out, e = 2 / (n + 1), [], None
    for v in x:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


def macd(c, fast=12, slow=26, sig=9):
    """(line, signal, histogram); values before `slow` candles are None."""
    f, s = _ema(c, fast), _ema(c, slow)
    line = [a - b for a, b in zip(f, s)]
    sg = _ema(line, sig)
    hist = [a - b for a, b in zip(line, sg)]
    warm = lambda x: [None if k < slow + sig else v for k, v in enumerate(x)]  # noqa: E731
    return warm(line), warm(sg), warm(hist)


def _pivot(x, j, low):
    a, b = x[j - P:j], x[j + 1:j + P + 1]
    v = x[j]
    return all(v < z for z in a) and all(v <= z for z in b) if low else all(v > z for z in a) and all(v >= z for z in b)


def events(o, h, l, c, r=None, hist=None, start=None, end=None):
    """Divergences confirmed on days start..end (indices; default: all). Each is a dict:
    side ('bull'/'bear'), i (day it is known), j (the new swing), p (the previous swing), kind ('triple' or 'rsi'),
    price / rsi / hist at both swings, and a candle flag (the confirming day closed in the signal's direction)."""
    n = len(c)
    r = r if r is not None else rsi(c)
    hist = hist if hist is not None else macd(c)[2]
    start = max(2 * P + 30, start if start is not None else 0)
    end = n - 1 if end is None else min(end, n - 1)
    out = []
    for i in range(start, end + 1):
        j = i - P
        for low in (True, False):
            x = l if low else h
            if not _pivot(x, j, low) or r[j] is None:
                continue
            for p in range(j - MIN_GAP, max(j - MAX_GAP, P) - 1, -1):     # the nearest earlier swing on that side
                if not _pivot(x, p, low):
                    continue
                if r[p] is None:
                    break
                between = x[p + 1:j]
                if low:
                    ok = x[j] < x[p] and (not between or min(between) > x[j]) and r[j] >= r[p] + RSI_MIN and r[p] < RSI_ZONE_BULL
                else:
                    ok = x[j] > x[p] and (not between or max(between) < x[j]) and r[j] <= r[p] - RSI_MIN and r[p] > RSI_ZONE_BEAR
                if ok:
                    m = hist[j] is not None and hist[p] is not None and ((hist[j] > hist[p]) if low else (hist[j] < hist[p]))
                    out.append({"side": "bull" if low else "bear", "i": i, "j": j, "p": p, "kind": "triple" if m else "rsi",
                                "px_p": x[p], "px_j": x[j], "rsi_p": round(r[p], 1), "rsi_j": round(r[j], 1),
                                "hist_p": hist[p], "hist_j": hist[j], "candle": (c[i] > o[i]) if low else (c[i] < o[i]),
                                "swing": max(h[p:j + 1]) if low else min(l[p:j + 1])})   # the swing high (low) between: the classic target
                break
    return out
