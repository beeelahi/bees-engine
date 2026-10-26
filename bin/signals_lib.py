"""
Signal library for BinanceData. Every event is computed on CLOSED candles and stamped on the bar
where it becomes knowable (no look-ahead): pivot-based signals (divergences, market structure)
are stamped on the confirmation bar, PIVOT_R bars after the swing.

Event tuple: (symbol, tf, period_start, signal, direction, warm, provisional, close, confirmed, rvol)
  warm       1 if every indicator involved was past its warm-up at that bar
  confirmed  1 if the signal candle's volume was >= CONFIRM_RVOL x the previous-20 average
"""

PIVOT_L, PIVOT_R = 5, 5          # TradingView default swing definition
DIV_RANGE = (5, 60)              # bars between the two pivots (TradingView Divergence Indicator)
CONFIRM_RVOL = 1.5
RECLAIM_BARS = 20
SQUEEZE_WINDOW = 10

MA_PAIRS = [(9, 21), (20, 50), (50, 100), (50, 200), (100, 200)]
PRICE_MA = [21, 50, 100, 200]

# Events that are one-directional by definition (a cross back is not the opposite signal)
ONE_WAY = {"price_x_bb_up": "bullish", "price_x_bb_lo": "bearish", "price_x_dc20": "bullish",
           "price_x_dc55": "bullish", "price_x_cloud": "bullish"}


def crosses(a, b, i):
    """Pine ta.crossover / ta.crossunder on bar i."""
    if a[i] is None or b[i] is None or a[i - 1] is None or b[i - 1] is None:
        return None
    if a[i] > b[i] and a[i - 1] <= b[i - 1]:
        return "bullish"
    if a[i] < b[i] and a[i - 1] >= b[i - 1]:
        return "bearish"
    return None


def pivots(src, left=PIVOT_L, right=PIVOT_R, high=True):
    """Pivot indices p (swing high if high=True, else swing low): src[p] is strictly beyond every one of
    the `left` bars before it and at least as extreme as the `right` bars after it. Confirmed at p+right."""
    out = []
    n = len(src)
    for p in range(left, n - right):
        v = src[p]
        if v is None:
            continue
        L = src[p - left:p]
        R = src[p + 1:p + right + 1]
        if any(x is None for x in L) or any(x is None for x in R):
            continue
        if high and all(v > x for x in L) and all(v >= x for x in R):
            out.append(p)
        if not high and all(v < x for x in L) and all(v <= x for x in R):
            out.append(p)
    return out


def divergences(osc, low, high, osc_warm):
    """TradingView 'Divergence Indicator' logic on oscillator pivots.
    Returns list of (confirm_bar, kind, direction): kind in regular/hidden."""
    ev = []
    for is_high in (False, True):
        piv = pivots(osc, high=is_high)
        prev = None
        for p in piv:
            i = p + PIVOT_R                                   # confirmation bar
            if prev is not None:
                bars = (i - 1) - (prev + PIVOT_R)             # Pine: barssince(found[1])
                if DIV_RANGE[0] <= bars <= DIV_RANGE[1] and prev >= osc_warm:
                    if not is_high:
                        if osc[p] > osc[prev] and low[p] < low[prev]:
                            ev.append((i, "regular", "bullish"))
                        elif osc[p] < osc[prev] and low[p] > low[prev]:
                            ev.append((i, "hidden", "bullish"))
                    else:
                        if osc[p] < osc[prev] and high[p] > high[prev]:
                            ev.append((i, "regular", "bearish"))
                        elif osc[p] > osc[prev] and high[p] < high[prev]:
                            ev.append((i, "hidden", "bearish"))
            prev = p
    return ev


def structure(high, low, close):
    """Market structure from price swings. Returns (events, state) where events are
    (bar, name, direction): structure_up / structure_down on a state change, bos_up / bos_down on the
    first close beyond the latest confirmed swing high / low. state[i] = +1 HH+HL, -1 LH+LL, 0 mixed."""
    n = len(close)
    ph = {p + PIVOT_R: p for p in pivots(high, high=True)}
    pl = {p + PIVOT_R: p for p in pivots(low, high=False)}
    highs, lows = [], []
    state, st = [0] * n, 0
    ev = []
    hi_level = lo_level = None
    hi_used = lo_used = True
    for i in range(n):
        if i in ph:
            highs.append(high[ph[i]])
            hi_level, hi_used = high[ph[i]], False
        if i in pl:
            lows.append(low[pl[i]])
            lo_level, lo_used = low[pl[i]], False
        if (i in ph or i in pl) and len(highs) >= 2 and len(lows) >= 2:
            new = 1 if (highs[-1] > highs[-2] and lows[-1] > lows[-2]) else (
                -1 if (highs[-1] < highs[-2] and lows[-1] < lows[-2]) else 0)
            if new != st and new != 0:
                ev.append((i, "structure_up" if new == 1 else "structure_down", "bullish" if new == 1 else "bearish"))
            st = new
        state[i] = st
        if i > 0:
            if hi_level is not None and not hi_used and close[i] > hi_level and close[i - 1] <= hi_level:
                ev.append((i, "bos_up", "bullish"))
                hi_used = True
            if lo_level is not None and not lo_used and close[i] < lo_level and close[i - 1] >= lo_level:
                ev.append((i, "bos_down", "bearish"))
                lo_used = True
    return ev, state


def events(sym, tf, cs, X, W):
    """cs: candles [period_start, o, h, l, c, vb, vq, trades, tb, n_days, complete]; X: indicator series;
    W(col) -> warm-from bar index for this timeframe."""
    n = len(cs)
    o = [c[1] for c in cs]
    h = [c[2] for c in cs]
    l = [c[3] for c in cs]
    c_ = [c[4] for c in cs]
    rv = X["rvol20"]
    out = []

    def add(i, name, direction, warm_bar):
        out.append((sym, tf, cs[i][0], name, direction, 1 if i - 1 >= warm_bar else 0,
                    0 if cs[i][-1] else 1, c_[i], 1 if (rv[i] is not None and rv[i] >= CONFIRM_RVOL) else 0, rv[i]))

    const = lambda v: [v] * n
    defs = []
    for kind in ("ema", "sma"):
        for f, s in MA_PAIRS:
            defs.append((f"{kind}{f}_x_{kind}{s}", X[f"{kind}{f}"], X[f"{kind}{s}"], max(W(f"{kind}{f}"), W(f"{kind}{s}"))))
        for p in PRICE_MA:
            defs.append((f"price_x_{kind}{p}", c_, X[f"{kind}{p}"], W(f"{kind}{p}")))
    defs += [("ema12_x_ema26", X["ema12"], X["ema26"], W("ema26")),
             ("sma7_x_sma25", X["sma7"], X["sma25"], W("sma25")),
             ("macd_x_signal", X["macd"], X["macd_signal"], W("macd")),
             ("macd_x_zero", X["macd"], const(0.0), W("macd")),
             ("macdf_x_signal", X["macdf"], X["macdf_signal"], W("macdf")),
             ("rsi14_x_30", X["rsi14"], const(30.0), W("rsi14")),
             ("rsi14_x_50", X["rsi14"], const(50.0), W("rsi14")),
             ("rsi14_x_70", X["rsi14"], const(70.0), W("rsi14")),
             ("tk_cross", X["ichi_tenkan"], X["ichi_kijun"], W("ichi_kijun")),
             ("rs30_x_zero", X["rs30"], const(0.0), W("rs30")),
             ("price_x_bb_up", c_, X["bb_up"], W("bb_up")),
             ("price_x_bb_lo", c_, X["bb_lo"], W("bb_lo")),
             ("price_x_dc20", c_, X["dc20_up"], W("dc20_up")),
             ("price_x_dc55", c_, X["dc55_up"], W("dc55_up"))]
    cloud_top = [None if a is None or b is None else max(a, b) for a, b in zip(X["ichi_span_a"], X["ichi_span_b"])]
    defs.append(("price_x_cloud", c_, cloud_top, W("ichi_span_b")))
    for name, a, b, w in defs:
        for i in range(1, n):
            d = crosses(a, b, i)
            if d and not (name in ONE_WAY and d != ONE_WAY[name]):
                add(i, name, d, w)

    # ADX crossing 25: direction from DI
    adx, pdi, mdi = X["adx14"], X["pdi14"], X["mdi14"]
    for i in range(1, n):
        if crosses(adx, const(25.0), i) == "bullish" and pdi[i] is not None:
            add(i, "adx_x_25", "bullish" if pdi[i] > mdi[i] else "bearish", W("adx14"))

    # Stoch RSI cross inside the extreme zones only
    k, d = X["stochrsi_k"], X["stochrsi_d"]
    for i in range(1, n):
        x = crosses(k, d, i)
        if x == "bullish" and k[i - 1] < 20 and d[i - 1] < 20:
            add(i, "stochrsi_os_cross", "bullish", W("stochrsi_d"))
        elif x == "bearish" and k[i - 1] > 80 and d[i - 1] > 80:
            add(i, "stochrsi_ob_cross", "bearish", W("stochrsi_d"))

    # MACD momentum turn: histogram still on the wrong side of zero but moving 2 bars in a row the other way
    hh = X["macd_hist"]
    for i in range(3, n):
        if None in (hh[i], hh[i - 1], hh[i - 2], hh[i - 3]):
            continue
        if hh[i] < 0 and hh[i] > hh[i - 1] > hh[i - 2] and not (hh[i - 1] > hh[i - 2] > hh[i - 3]):
            add(i, "macd_momentum_turn", "bullish", W("macd"))
        if hh[i] > 0 and hh[i] < hh[i - 1] < hh[i - 2] and not (hh[i - 1] < hh[i - 2] < hh[i - 3]):
            add(i, "macd_momentum_turn", "bearish", W("macd"))

    # Reclaim / loss of the 200 after a long stay on the other side
    for kind in ("ema", "sma"):
        ma = X[f"{kind}200"]
        run_below = run_above = 0
        for i in range(n):
            if ma[i] is None:
                continue
            if c_[i] > ma[i]:
                if run_below >= RECLAIM_BARS:
                    add(i, f"reclaim_{kind}200", "bullish", W(f"{kind}200"))
                run_above, run_below = run_above + 1, 0
            elif c_[i] < ma[i]:
                if run_above >= RECLAIM_BARS:
                    add(i, f"lose_{kind}200", "bearish", W(f"{kind}200"))
                run_below, run_above = run_below + 1, 0

    # Supertrend flip
    sd = X["st_dir"]
    for i in range(1, n):
        if sd[i - 1] is not None and sd[i] is not None and sd[i] != sd[i - 1]:
            add(i, "supertrend_flip", "bullish" if sd[i] == 1 else "bearish", W("st_dir"))

    # Volume, squeeze, taker flow, highs/lows
    bw, tbr = X["bb_bw_rank"], X["taker_buy_ratio"]
    last_sq = None
    ath, hi1, lo1 = X["ath"], X["hi_1y"], X["lo_1y"]
    year = {"1d": 365, "1w": 52, "1M": 12}[tf]
    for i in range(1, n):
        if rv[i] is not None and rv[i] >= 3.0 and (rv[i - 1] is None or rv[i - 1] < 3.0):
            add(i, "vol_spike_3x", "bullish" if c_[i] > o[i] else "bearish", 20)
        if bw[i] is not None and bw[i] <= 5 and (bw[i - 1] is None or bw[i - 1] > 5):
            add(i, "bb_squeeze", "neutral", W("bb_bw_rank"))
        if bw[i] is not None and bw[i] <= 5:
            last_sq = i
        if last_sq is not None and 0 < i - last_sq <= SQUEEZE_WINDOW and X["bb_up"][i] is not None:
            if c_[i] > X["bb_up"][i]:
                add(i, "squeeze_breakout", "bullish", W("bb_bw_rank"))
                last_sq = None
            elif c_[i] < X["bb_lo"][i]:
                add(i, "squeeze_breakout", "bearish", W("bb_bw_rank"))
                last_sq = None
        if rv[i] is not None and rv[i] >= 2.0 and tbr[i] is not None:
            prev_rv_ok = rv[i - 1] is not None and rv[i - 1] >= 2.0 and tbr[i - 1] is not None
            if tbr[i] >= 0.6 and not (prev_rv_ok and tbr[i - 1] >= 0.6):
                add(i, "taker_buy_surge", "bullish", 20)
            if tbr[i] <= 0.4 and not (prev_rv_ok and tbr[i - 1] <= 0.4):
                add(i, "taker_sell_surge", "bearish", 20)
        if i >= year and h[i] > ath[i - 1]:
            add(i, "new_ath", "bullish", 0)
        if hi1[i - 1] is not None and h[i] > hi1[i - 1] and h[i] <= ath[i - 1]:
            add(i, "new_1y_high", "bullish", 0)
        if lo1[i - 1] is not None and l[i] < lo1[i - 1]:
            add(i, "new_1y_low", "bearish", 0)

    # Relative strength vs BTC: pair/BTC ratio closes above its previous 90-bar high (first bar only)
    ratio = X.get("_ratio")
    if ratio:
        from indicators import rolling_max
        filled = [r if r is not None else float("-inf") for r in ratio]
        pm = rolling_max(filled, 90, include_current=False)
        nh = [ratio[i] is not None and pm[i] is not None and pm[i] > float("-inf") and ratio[i] > pm[i] for i in range(n)]
        for i in range(1, n):
            if nh[i] and not nh[i - 1]:
                add(i, "rs_new_high_90", "bullish", 90)

    # Divergences (RSI14 and MACD histogram), confirmed PIVOT_R bars after the swing
    for osc_name, osc, wcol in (("rsi", X["rsi14"], "rsi14"), ("macd", X["macd_hist"], "macd")):
        for i, kind, dirn in divergences(osc, l, h, W(wcol)):
            add(i, f"{osc_name}_div_{kind}", dirn, W(wcol))

    # Market structure and break of structure
    sev, _ = structure(h, l, c_)
    for i, name, dirn in sev:
        add(i, name, dirn, 2 * (PIVOT_L + PIVOT_R))

    # Confluence score crossing strong / weak thresholds
    sc = X["bull_score"]
    for i in range(1, n):
        if sc[i] is not None and sc[i - 1] is not None:
            if sc[i] >= 8 > sc[i - 1]:
                add(i, "confluence_strong", "bullish", W("bull_score"))
            if sc[i] <= 2 < sc[i - 1]:
                add(i, "confluence_weak", "bearish", W("bull_score"))
    return out


# Human-readable catalogue (also exported to the app)
CATALOG = {
    "ema9_x_ema21": "EMA9 crosses EMA21", "sma9_x_sma21": "MA9 crosses MA21",
    "ema20_x_ema50": "EMA20 crosses EMA50", "sma20_x_sma50": "MA20 crosses MA50",
    "ema50_x_ema100": "EMA50 crosses EMA100", "sma50_x_sma100": "MA50 crosses MA100",
    "ema50_x_ema200": "EMA golden / death cross (50 × 200)", "sma50_x_sma200": "MA golden / death cross (50 × 200)",
    "ema100_x_ema200": "EMA100 crosses EMA200", "sma100_x_sma200": "MA100 crosses MA200",
    "ema12_x_ema26": "EMA12 crosses EMA26", "sma7_x_sma25": "MA7 crosses MA25 (Binance default)",
    "price_x_ema21": "Close crosses EMA21", "price_x_ema50": "Close crosses EMA50", "price_x_ema100": "Close crosses EMA100",
    "price_x_ema200": "Close crosses EMA200", "price_x_sma21": "Close crosses MA21", "price_x_sma50": "Close crosses MA50",
    "price_x_sma100": "Close crosses MA100", "price_x_sma200": "Close crosses MA200",
    "reclaim_ema200": "Reclaims EMA200 after 20+ bars below", "lose_ema200": "Loses EMA200 after 20+ bars above",
    "reclaim_sma200": "Reclaims MA200 after 20+ bars below", "lose_sma200": "Loses MA200 after 20+ bars above",
    "macd_x_signal": "MACD crosses signal", "macd_x_zero": "MACD crosses zero", "macdf_x_signal": "Fast MACD 8/21/5 crosses signal",
    "macd_momentum_turn": "MACD histogram turning (2 bars) before the cross",
    "rsi14_x_30": "RSI crosses 30", "rsi14_x_50": "RSI crosses 50", "rsi14_x_70": "RSI crosses 70",
    "rsi_div_regular": "RSI regular divergence", "rsi_div_hidden": "RSI hidden divergence",
    "macd_div_regular": "MACD-histogram regular divergence", "macd_div_hidden": "MACD-histogram hidden divergence",
    "structure_up": "Uptrend structure (higher high + higher low)", "structure_down": "Downtrend structure (lower high + lower low)",
    "bos_up": "Break of structure up (close above last swing high)", "bos_down": "Break of structure down (close below last swing low)",
    "price_x_dc20": "20-bar breakout", "price_x_dc55": "55-bar breakout", "new_1y_high": "New 1-year high",
    "new_1y_low": "New 1-year low", "new_ath": "New all-time high", "price_x_bb_up": "Close above upper Bollinger",
    "price_x_bb_lo": "Close below lower Bollinger", "bb_squeeze": "Bollinger squeeze (tightest in 120 bars)",
    "squeeze_breakout": "Breakout from a squeeze (within 10 bars)", "price_x_cloud": "Ichimoku cloud breakout",
    "tk_cross": "Ichimoku Tenkan × Kijun", "supertrend_flip": "Supertrend flip", "adx_x_25": "ADX rises above 25 (DI sets direction)",
    "vol_spike_3x": "Volume spike ≥ 3×", "taker_buy_surge": "Aggressive buying surge (≥ 60% taker buy, RVOL ≥ 2)",
    "taker_sell_surge": "Aggressive selling surge (≤ 40% taker buy, RVOL ≥ 2)",
    "rs30_x_zero": "Relative strength vs BTC (30 bars) turns positive / negative", "rs_new_high_90": "Strength vs BTC at a 90-bar high",
    "stochrsi_os_cross": "Stoch RSI bullish cross below 20", "stochrsi_ob_cross": "Stoch RSI bearish cross above 80",
    "confluence_strong": "Confluence score reaches 8+ of 10", "confluence_weak": "Confluence score drops to 2 or less",
}
