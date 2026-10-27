#!/usr/bin/env python3
"""
BinanceData analytics v2 - rebuilds every derived table from the raw `daily` table, and appends
to the persistent logs (highlight_log, universe_log). Raw data is never modified.

REBUILT EACH RUN (atomic swap, readers never see half-built tables)
  candles       symbol, tf (1d/1w/1M), period_start, OHLCV, taker buy, n_days, complete
  indicators    one row per symbol/tf/period: ~75 indicator columns + `bar` (index in series)
                + legacy *_warm flags for EMA21/50/200, RSI14, MACD
  dictionary    every indicator column: family, label, description, warm_from_bar
  signals       crossover / breakout / trend-flip events (warm, provisional flags)
  latest        one row per pair: returns + every indicator for 1d and the last CLOSED 1w/1M,
                with values that aren't warmed up set to NULL (safe to screen on directly)
  forward       per pair/day forward returns (research only; never feeds a signal)
  watch_hits    days meeting watchlist rule v0, with outcomes
  watch_stats   key numbers: rule vs. baseline of every liquid pair-day
  weekly_report last closed week: new listings, delistings, top movers, signal counts
  series_info   per pair: series start used, breaks (>= BREAK_DAYS missing days = relisting /
                token migration: indicators restart after the last break)

PERSISTENT (append-only, never dropped)
  highlight_log  every highlight the first time it appears, with first_seen_utc
  universe_state / universe_log   membership of the filtered universe and every enter/leave

Conventions: TA-Lib / TradingView formulas (see indicators.py, validated against TA-Lib).
A value is 'warm' from dictionary.warm_from_bar onward: before that it still depends on where
the series starts by more than 0.1%.
"""
import json, os, sqlite3, sys, time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicators as I
import signals_lib as SL

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(ROOT, "data", "binance.db")

BREAK_DAYS = 7
RETURN_DAYS = [1, 7, 30, 90, 180, 365]
FWD_DAYS = [1, 3, 7, 14, 30, 90]
VOL_SPIKE = 3.0
MIN_VOL_USDT = 100_000
WATCH = {"rvol_min": 3.0, "rsi_min": 50.0, "rsi_max": 75.0, "min_vol_usdt": 100_000}
YEAR_BARS = {"1d": 365, "1w": 52, "1M": 12}
HV = {"1d": (30, 365), "1w": (26, 52), "1M": (12, 12)}
EMA_N = [5, 8, 9, 12, 13, 20, 21, 26, 34, 50, 55, 89, 100, 144, 200]
SMA_N = [7, 9, 10, 20, 21, 25, 50, 99, 100, 200]
RSI_N = [6, 12, 14, 24]
K = I.k_needed

# ---------------------------------------------------------------- column catalogue
# name -> (family, label, description, warm_from_bar)
DICT = {}
for n in EMA_N:
    DICT[f"ema{n}"] = ("trend", f"EMA {n}", f"Exponential moving average of close, {n} bars, SMA-seeded.", I.ema_warm(n))
for n in SMA_N:
    DICT[f"sma{n}"] = ("trend", f"SMA {n}", f"Simple moving average of close, {n} bars.", n - 1)
for n in RSI_N:
    DICT[f"rsi{n}"] = ("momentum", f"RSI {n}", f"Wilder RSI, {n} bars.", I.rsi_warm(n))
MW, MFW = I.macd_warm(12, 26, 9), I.macd_warm(8, 21, 5)
DICT.update({
    "macd": ("momentum", "MACD 12/26", "EMA12 - EMA26 of close.", MW),
    "macd_signal": ("momentum", "MACD signal 9", "EMA9 of MACD.", MW),
    "macd_hist": ("momentum", "MACD histogram", "MACD - signal.", MW),
    "macdf": ("momentum", "MACD 8/21", "Fast MACD: EMA8 - EMA21.", MFW),
    "macdf_signal": ("momentum", "MACD 8/21 signal 5", "EMA5 of fast MACD.", MFW),
    "macdf_hist": ("momentum", "MACD 8/21 histogram", "Fast MACD - signal.", MFW),
    "roc10": ("momentum", "ROC 10", "Rate of change over 10 bars, %.", 10),
    "roc30": ("momentum", "ROC 30", "Rate of change over 30 bars, %.", 30),
    "stoch_k": ("momentum", "Stoch %K 14,3", "Slow stochastic %K (14, SMA3).", 15),
    "stoch_d": ("momentum", "Stoch %D 3", "SMA3 of %K.", 17),
    "stochrsi_k": ("momentum", "Stoch RSI %K", "Stochastic of RSI14 over 14 bars, SMA3 (TradingView).", I.stoch_rsi_warm() - 2),
    "stochrsi_d": ("momentum", "Stoch RSI %D", "SMA3 of Stoch RSI %K.", I.stoch_rsi_warm()),
    "bb_mid": ("volatility", "Bollinger mid 20", "SMA20 of close.", 19),
    "bb_up": ("volatility", "Bollinger upper 20,2", "SMA20 + 2 population std dev.", 19),
    "bb_lo": ("volatility", "Bollinger lower 20,2", "SMA20 - 2 population std dev.", 19),
    "bb_pctb": ("volatility", "Bollinger %B", "(close - lower) / (upper - lower). >1 above upper band.", 19),
    "bb_bw": ("volatility", "Bollinger bandwidth", "(upper - lower) / mid. Low = squeeze.", 19),
    "bb_bw_rank": ("volatility", "Bandwidth rank 120", "Percentile of bandwidth within the last 120 bars (<= 5 = squeeze).", 138),
    "atr14": ("volatility", "ATR 14", "Wilder average true range (TA-Lib).", I.atr_warm(14)),
    "atr14_pct": ("volatility", "ATR 14 %", "ATR14 / close, %.", I.atr_warm(14)),
    "hv": ("volatility", "Historical volatility", "Annualized std dev of log returns, % (1d: 30 bars, 1w: 26, 1M: 12).", None),
    "dc20_up": ("volatility", "Donchian 20 high", "Highest high of the previous 20 bars.", 20),
    "dc20_lo": ("volatility", "Donchian 20 low", "Lowest low of the previous 20 bars.", 20),
    "dc55_up": ("volatility", "Donchian 55 high", "Highest high of the previous 55 bars.", 55),
    "dc55_lo": ("volatility", "Donchian 55 low", "Lowest low of the previous 55 bars.", 55),
    "adx14": ("trend", "ADX 14", "Wilder ADX (trend strength; > 25 trending).", I.adx_warm(14)),
    "pdi14": ("trend", "+DI 14", "Wilder +DI.", 14 + K(1 / 14)),
    "mdi14": ("trend", "-DI 14", "Wilder -DI.", 14 + K(1 / 14)),
    "supertrend": ("trend", "Supertrend 10,3", "TradingView ta.supertrend(3, 10) line.", I.supertrend_warm(10)),
    "st_dir": ("trend", "Supertrend direction", "+1 uptrend, -1 downtrend.", I.supertrend_warm(10)),
    "ichi_tenkan": ("trend", "Ichimoku Tenkan 9", "(9-bar high + low) / 2.", 8),
    "ichi_kijun": ("trend", "Ichimoku Kijun 26", "(26-bar high + low) / 2.", 25),
    "ichi_span_a": ("trend", "Ichimoku Span A", "Cloud edge A under the current bar ((Tenkan+Kijun)/2 from 26 bars ago).", 51),
    "ichi_span_b": ("trend", "Ichimoku Span B", "Cloud edge B under the current bar (52-bar midpoint from 26 bars ago).", 77),
    "ichi_pos": ("trend", "Price vs cloud", "+1 above the cloud, 0 inside, -1 below.", 77),
    "volume_usdt": ("volume", "Volume (USDT)", "Quote volume of the candle.", 0),
    "vol_base20": ("volume", "Avg volume 20", "Average USDT volume of the previous 20 bars.", 20),
    "rvol20": ("volume", "RVOL 20", "Volume / average of the previous 20 bars.", 20),
    "rvol50": ("volume", "RVOL 50", "Volume / average of the previous 50 bars.", 50),
    "vol_z20": ("volume", "Volume z-score 20", "(volume - mean) / std dev of the previous 20 bars.", 20),
    "obv": ("volume", "OBV", "On-balance volume (base asset). Use its changes, not its level.", 0),
    "mfi14": ("volume", "MFI 14", "Money Flow Index (TA-Lib).", 14),
    "cmf20": ("volume", "CMF 20", "Chaikin Money Flow, 20 bars.", 19),
    "taker_buy_ratio": ("volume", "Taker buy ratio", "Share of USDT volume bought aggressively (> 0.5 = buyers lifting offers).", 0),
    "ath": ("position", "All-time high", "Highest high since listing (or since the last relisting break).", 0),
    "dist_ath": ("position", "From ATH %", "close / ATH - 1, %.", 0),
    "bars_since_ath": ("position", "Bars since ATH", "Candles since the all-time high.", 0),
    "hi_1y": ("position", "1-year high", "Highest high over 1 year (365 d / 52 w / 12 M).", None),
    "lo_1y": ("position", "1-year low", "Lowest low over 1 year.", None),
    "dist_hi_1y": ("position", "From 1y high %", "close / 1-year high - 1, %.", None),
    "dist_lo_1y": ("position", "From 1y low %", "close / 1-year low - 1, %.", None),
    "rs10": ("position", "vs BTC 10 bars %", "Relative return vs BTC over 10 bars: (pair/BTC ratio change), %.", 10),
    "rs30": ("position", "vs BTC 30 bars %", "Relative return vs BTC over 30 bars, %.", 30),
    "streak": ("position", "Close streak", "Consecutive higher (+) or lower (-) closes.", 0),
    "bull_score": ("composite", "Confluence score", "0-10: close>EMA200, EMA50>EMA200, close>EMA21, MACD hist>0, RSI14>50, "
                   "Supertrend up, above cloud, ADX>=20 with +DI>-DI, beating BTC over 30 bars, RVOL>=1 with CMF>0. "
                   "Null until all ten are warmed up.", None),
})
IND_COLS = list(DICT)
LEGACY_WARM = {"ema21_warm": "ema21", "ema50_warm": "ema50", "ema200_warm": "ema200", "rsi14_warm": "rsi14", "macd_warm": "macd"}


def warm_from(col, tf):
    w = DICT[col][3]
    if col == "hv":
        return HV[tf][0]
    if col == "bull_score":
        return max(I.ema_warm(200), I.macd_warm(12, 26, 9), I.supertrend_warm(10), 77, I.adx_warm(14), 30)
    if col in ("hi_1y", "lo_1y", "dist_hi_1y", "dist_lo_1y"):
        return YEAR_BARS[tf] - 1
    return w


# ---------------------------------------------------------------- resample
def period_key(d, tf):
    if tf == "1d":
        return d
    dt = datetime.strptime(d, "%Y-%m-%d")
    if tf == "1w":
        return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-01")


def period_end(key, tf):
    dt = datetime.strptime(key, "%Y-%m-%d")
    if tf == "1d":
        return key
    if tf == "1w":
        return (dt + timedelta(days=6)).strftime("%Y-%m-%d")
    nxt = datetime(dt.year + (dt.month == 12), dt.month % 12 + 1, 1)
    return (nxt - timedelta(days=1)).strftime("%Y-%m-%d")


def resample(rows, tf, last_date, drop_partial_first):
    """rows: (date, o, h, l, c, vb, vq, trades, taker_buy_usdt). Returns
    [period_start, o, h, l, c, vb, vq, trades, tb, n_days, complete]."""
    if tf == "1d":
        return [[r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], 1, 1] for r in rows]
    out, cur = [], None
    for d, o, h, l, c, vb, vq, n, tb in rows:
        k = period_key(d, tf)
        if cur is None or cur[0] != k:
            if cur:
                out.append(cur)
            cur = [k, o, h, l, c, vb, vq, n, None if n is None else (tb or 0.0), 1]   # n None: venue without
            #                                                     trade counts / taker volume (venues.py); kept empty
        else:
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], l)
            cur[4] = c
            cur[5] += vb
            cur[6] += vq
            cur[7] = None if n is None or cur[7] is None else cur[7] + n
            cur[8] = None if n is None or cur[8] is None else cur[8] + (tb or 0.0)
            cur[9] += 1
    if cur:
        out.append(cur)
    if out and drop_partial_first and out[0][0] < rows[0][0]:
        out = out[1:]
    for p in out:
        p.append(1 if period_end(p[0], tf) <= last_date else 0)
    return out


JUMP_X = 20      # a >= 20x price jump (either way) across a trading pause = redenomination / token swap / ticker reuse


def segment(rows):
    """Return rows after the last series break, and the break dates. A break is a gap of >= BREAK_DAYS missing
    days, or a price discontinuity of >= JUMP_X (close to close, either direction) across at least one missing
    day. Real crashes and pumps trade continuously and are kept."""
    breaks, start = [], 0
    for i in range(1, len(rows)):
        gap = (datetime.strptime(rows[i][0], "%Y-%m-%d") - datetime.strptime(rows[i - 1][0], "%Y-%m-%d")).days - 1
        pc, cc = rows[i - 1][4], rows[i][4]
        jump = gap >= 1 and pc and cc and (cc / pc >= JUMP_X or pc / cc >= JUMP_X)
        if gap >= BREAK_DAYS or jump:
            breaks.append((rows[i - 1][0], rows[i][0], gap))
            start = i
    return rows[start:], breaks


# ---------------------------------------------------------------- per-series indicators
def compute(cs, tf, btc_close):
    # opens are not used by these indicators
    h = [c[2] for c in cs]
    l = [c[3] for c in cs]
    c_ = [c[4] for c in cs]
    vb = [c[5] for c in cs]
    vq = [c[6] for c in cs]
    tb = [c[8] for c in cs]
    n = len(cs)
    X = {}
    for p in EMA_N:
        X[f"ema{p}"] = I.ema(c_, p)
    for p in SMA_N:
        X[f"sma{p}"] = I.sma(c_, p)
    for p in RSI_N:
        X[f"rsi{p}"] = I.rsi(c_, p)
    X["macd"], X["macd_signal"], X["macd_hist"] = I.macd(c_, 12, 26, 9)
    X["macdf"], X["macdf_signal"], X["macdf_hist"] = I.macd(c_, 8, 21, 5)
    X["roc10"], X["roc30"] = I.roc(c_, 10), I.roc(c_, 30)
    X["stoch_k"], X["stoch_d"] = I.stoch(h, l, c_)
    X["stochrsi_k"], X["stochrsi_d"] = I.stoch_rsi(c_)
    X["bb_mid"], X["bb_up"], X["bb_lo"] = I.bbands(c_, 20, 2.0)
    X["bb_pctb"] = [None if u is None or u == lo else (c_[i] - lo) / (u - lo) for i, (u, lo) in enumerate(zip(X["bb_up"], X["bb_lo"]))]
    X["bb_bw"] = [None if m is None or m == 0 else (u - lo) / m for m, u, lo in zip(X["bb_mid"], X["bb_up"], X["bb_lo"])]
    X["bb_bw_rank"] = I.rolling_pct_rank(X["bb_bw"], 120)
    X["atr14"] = I.atr(h, l, c_, 14)
    X["atr14_pct"] = [None if a is None or c_[i] == 0 else a / c_[i] * 100 for i, a in enumerate(X["atr14"])]
    X["hv"] = I.realized_vol(c_, *HV[tf])
    X["dc20_up"], X["dc20_lo"] = I.donchian(h, l, 20)
    X["dc55_up"], X["dc55_lo"] = I.donchian(h, l, 55)
    X["pdi14"], X["mdi14"], X["adx14"] = I.adx(h, l, c_, 14)
    X["supertrend"], X["st_dir"] = I.supertrend(h, l, c_, 10, 3.0)
    X["ichi_tenkan"], X["ichi_kijun"], X["ichi_span_a"], X["ichi_span_b"] = I.ichimoku(h, l)
    X["ichi_pos"] = [None if a is None or b is None else (1 if c_[i] > max(a, b) else (-1 if c_[i] < min(a, b) else 0))
                     for i, (a, b) in enumerate(zip(X["ichi_span_a"], X["ichi_span_b"]))]
    X["volume_usdt"] = vq
    X["vol_base20"], X["rvol20"], X["vol_z20"] = I.rvol(vq, 20)
    X["rvol50"] = I.rvol(vq, 50)[1]
    X["obv"] = I.obv(c_, vb)
    X["mfi14"] = I.mfi(h, l, c_, vb, 14)
    X["cmf20"] = I.cmf(h, l, c_, vb, 20)
    X["taker_buy_ratio"] = [None if vq[i] <= 0 or tb[i] is None else tb[i] / vq[i] for i in range(n)]
    X["ath"], X["bars_since_ath"] = I.running_high(h)
    X["dist_ath"] = [(c_[i] / X["ath"][i] - 1) * 100 for i in range(n)]
    X["hi_1y"], X["lo_1y"] = I.window_high_low(h, l, YEAR_BARS[tf])
    X["dist_hi_1y"] = [None if a is None else (c_[i] / a - 1) * 100 for i, a in enumerate(X["hi_1y"])]
    X["dist_lo_1y"] = [None if a is None else (c_[i] / a - 1) * 100 for i, a in enumerate(X["lo_1y"])]
    ratio = [None if btc_close.get(cs[i][0]) in (None, 0) else c_[i] / btc_close[cs[i][0]] for i in range(n)]
    for p in (10, 30):
        X[f"rs{p}"] = [None if i < p or ratio[i] is None or ratio[i - p] in (None, 0) else (ratio[i] / ratio[i - p] - 1) * 100 for i in range(n)]
    X["streak"] = I.streak(c_)
    X["_ratio"] = ratio
    sc = []
    for i in range(n):
        conds = [
            None if X["ema200"][i] is None else c_[i] > X["ema200"][i],
            None if X["ema200"][i] is None else X["ema50"][i] > X["ema200"][i],
            None if X["ema21"][i] is None else c_[i] > X["ema21"][i],
            None if X["macd_hist"][i] is None else X["macd_hist"][i] > 0,
            None if X["rsi14"][i] is None else X["rsi14"][i] > 50,
            None if X["st_dir"][i] is None else X["st_dir"][i] == 1,
            None if X["ichi_pos"][i] is None else X["ichi_pos"][i] == 1,
            None if X["adx14"][i] is None else (X["adx14"][i] >= 20 and X["pdi14"][i] > X["mdi14"][i]),
            None if X["rs30"][i] is None else X["rs30"][i] > 0,
            None if (X["rvol20"][i] is None or X["cmf20"][i] is None) else (X["rvol20"][i] >= 1 and X["cmf20"][i] > 0),
        ]
        sc.append(None if None in conds else sum(conds))
    X["bull_score"] = sc
    return X


def warm_vec(col, tf, n):
    w = warm_from(col, tf)
    return [1 if i >= w else 0 for i in range(n)]


# ---------------------------------------------------------------- forward / watchlist
def forward_returns(cs):
    closes, highs, lows = [c[4] for c in cs], [c[2] for c in cs], [c[3] for c in cs]
    n = len(cs)
    hi7, lo7 = I.rolling_max(highs, 7), I.rolling_min(lows, 7)
    hi30, lo30 = I.rolling_max(highs, 30), I.rolling_min(lows, 30)
    out = []
    for i in range(n):
        c = closes[i]
        r = [((closes[i + k] / c - 1) * 100 if i + k < n else None) for k in FWD_DAYS]
        r += [(hi7[i + 7] / c - 1) * 100, (lo7[i + 7] / c - 1) * 100] if i + 7 < n else [None, None]
        r += [(hi30[i + 30] / c - 1) * 100, (lo30[i + 30] / c - 1) * 100] if i + 30 < n else [None, None]
        out.append(r)
    return out


FWD_COLS = [f"fwd_ret_{n}d" for n in FWD_DAYS] + ["max_gain_7d", "max_dd_7d", "max_gain_30d", "max_dd_30d"]
# Averages cap each outcome so one freak event cannot dominate a statistic (raw values stay in `forward`).
FWD_CAP = {"fwd_ret_1d": 100, "fwd_ret_3d": 100, "fwd_ret_7d": 150, "fwd_ret_14d": 200, "fwd_ret_30d": 300,
           "fwd_ret_90d": 500, "max_gain_7d": 150, "max_gain_30d": 300}


def capped(col, alias=""):
    """SQL expression for a capped outcome column (NULL stays NULL)."""
    c = (alias + "." if alias else "") + col
    return f"MIN({c}, {FWD_CAP[col]})" if col in FWD_CAP else c


# ---------------------------------------------------------------- schema
def build_schema(con):
    ind = ", ".join(f"{c} REAL" for c in IND_COLS)
    legacy = ", ".join(f"{c} INTEGER" for c in LEGACY_WARM)
    con.executescript(f"""
    DROP TABLE IF EXISTS candles_new; DROP TABLE IF EXISTS indicators_new; DROP TABLE IF EXISTS signals_new;
    DROP TABLE IF EXISTS latest_new; DROP TABLE IF EXISTS forward_new; DROP TABLE IF EXISTS watch_hits_new;
    DROP TABLE IF EXISTS watch_stats_new; DROP TABLE IF EXISTS dictionary_new; DROP TABLE IF EXISTS series_info_new;
    DROP TABLE IF EXISTS weekly_report_new;
    CREATE TABLE candles_new(symbol TEXT, tf TEXT, period_start TEXT, open REAL, high REAL, low REAL, close REAL,
        volume_base REAL, volume_usdt REAL, trades INTEGER, taker_buy_usdt REAL, n_days INTEGER, complete INTEGER,
        PRIMARY KEY(symbol, tf, period_start));
    CREATE TABLE indicators_new(symbol TEXT, tf TEXT, period_start TEXT, complete INTEGER, bar INTEGER, close REAL,
        {ind}, {legacy}, PRIMARY KEY(symbol, tf, period_start));
    CREATE TABLE signals_new(symbol TEXT, tf TEXT, period_start TEXT, signal TEXT, direction TEXT, warm INTEGER,
        provisional INTEGER, close REAL, confirmed INTEGER, rvol REAL);
    CREATE TABLE forward_new(symbol TEXT, date TEXT, close REAL, {", ".join(c + " REAL" for c in FWD_COLS)},
        PRIMARY KEY(symbol, date));
    CREATE TABLE watch_hits_new(symbol TEXT, date TEXT, close REAL, volume_usdt REAL, rvol REAL, vol_z REAL, rsi REAL,
        macd_hist REAL, taker_buy_ratio REAL, {", ".join(c + " REAL" for c in FWD_COLS)}, PRIMARY KEY(symbol, date));
    CREATE TABLE dictionary_new(col TEXT PRIMARY KEY, family TEXT, label TEXT, description TEXT,
        warm_from_bar_1d INTEGER, warm_from_bar_1w INTEGER, warm_from_bar_1M INTEGER);
    CREATE TABLE series_info_new(symbol TEXT PRIMARY KEY, series_start TEXT, last_date TEXT, breaks INTEGER,
        break_detail TEXT);
    CREATE TABLE IF NOT EXISTS highlight_log(symbol TEXT, tf TEXT, period_start TEXT, kind TEXT, direction TEXT,
        detail TEXT, first_seen_utc TEXT, PRIMARY KEY(symbol, tf, period_start, kind));
    CREATE TABLE IF NOT EXISTS universe_state(symbol TEXT PRIMARY KEY, since TEXT);
    CREATE TABLE IF NOT EXISTS universe_log(symbol TEXT, event TEXT, date TEXT, logged_utc TEXT, vol_24h_usdt REAL);
    """)
    con.executemany("INSERT INTO dictionary_new VALUES(?,?,?,?,?,?,?)",
                    [(c, *DICT[c][:3], warm_from(c, "1d"), warm_from(c, "1w"), warm_from(c, "1M")) for c in IND_COLS])


# ---------------------------------------------------------------- per symbol
def process_symbol(con, sym, rows, drop_partial_first, btc, stamp):
    rows, breaks = segment(rows)
    con.execute("INSERT INTO series_info_new VALUES(?,?,?,?,?)",
                (sym, rows[0][0], rows[-1][0], len(breaks), json.dumps(breaks) if breaks else None))
    last_date = rows[-1][0]
    if breaks:
        drop_partial_first = True          # the series starts mid-period after a relisting
    sig_rows = []
    for tf in ("1d", "1w", "1M"):
        cs = resample(rows, tf, last_date, drop_partial_first)
        if not cs:
            continue
        n = len(cs)
        con.executemany("INSERT INTO candles_new VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [(sym, tf, *c) for c in cs])
        X = compute(cs, tf, btc.get(tf, {}))
        c_ = [c[4] for c in cs]
        legacy = {k: warm_vec(v, tf, n) for k, v in LEGACY_WARM.items()}
        con.executemany(
            f"INSERT INTO indicators_new VALUES({','.join('?' * (6 + len(IND_COLS) + len(LEGACY_WARM)))})",
            [(sym, tf, cs[i][0], cs[i][-1], i, c_[i], *[X[k][i] for k in IND_COLS], *[legacy[k][i] for k in LEGACY_WARM])
             for i in range(n)])
        sig_rows += SL.events(sym, tf, cs, X, lambda col, tf=tf: warm_from(col, tf))
        if tf == "1d":
            fw = forward_returns(cs)
            con.executemany(f"INSERT INTO forward_new VALUES({','.join('?' * (3 + len(FWD_COLS)))})",
                            [(sym, cs[i][0], c_[i], *fw[i]) for i in range(n)])
            W = WATCH
            ew, mw, rw = warm_from("ema21", tf), warm_from("macd", tf), warm_from("rsi14", tf)
            hits = []
            for i in range(n):
                rv = X["rvol20"][i]
                if (rv is not None and rv >= W["rvol_min"] and cs[i][6] >= W["min_vol_usdt"] and c_[i] > cs[i][1]
                        and i >= ew and c_[i] > X["ema21"][i] and i >= mw and X["macd_hist"][i] > 0
                        and i >= rw and W["rsi_min"] <= X["rsi14"][i] <= W["rsi_max"]):
                    hits.append((sym, cs[i][0], c_[i], cs[i][6], rv, X["vol_z20"][i], X["rsi14"][i], X["macd_hist"][i],
                                 X["taker_buy_ratio"][i], *fw[i]))
            con.executemany(f"INSERT INTO watch_hits_new VALUES({','.join('?' * (9 + len(FWD_COLS)))})", hits)
    con.executemany("INSERT INTO signals_new VALUES(?,?,?,?,?,?,?,?,?,?)", sig_rows)


# ---------------------------------------------------------------- latest
LATEST_COLS = ["close", "ema9", "ema21", "ema50", "ema100", "ema200", "sma7", "sma25", "sma99", "sma200",
               "rsi6", "rsi14", "rsi24", "macd", "macd_signal", "macd_hist", "macdf_hist", "roc10", "roc30",
               "stoch_k", "stoch_d", "stochrsi_k", "stochrsi_d", "bb_pctb", "bb_bw", "bb_bw_rank", "atr14_pct", "hv",
               "adx14", "pdi14", "mdi14", "st_dir", "supertrend", "ichi_pos", "dc20_up", "dc55_up", "mfi14", "cmf20",
               "rvol20", "rvol50", "vol_z20", "taker_buy_ratio", "dist_ath", "bars_since_ath", "dist_hi_1y",
               "dist_lo_1y", "rs10", "rs30", "streak", "bull_score"]


def build_latest(con):
    universe = {s for (s,) in con.execute("SELECT symbol FROM v_universe")} if con.execute(
        "SELECT 1 FROM sqlite_master WHERE name='v_universe'").fetchone() else set()
    vol24 = dict(con.execute("""SELECT symbol, quote_volume_24h FROM ticker_snapshots
                                WHERE snap_date = (SELECT MAX(snap_date) FROM ticker_snapshots)"""))
    pairs = {r[0]: r[1:] for r in con.execute("SELECT symbol, token, status, first_candle FROM pairs")}
    watch_last = {(s, d) for s, d in con.execute("SELECT symbol, date FROM watch_hits_new")}
    cols = (["symbol", "token", "status", "listed", "last_date", "last_close", "last_day_volume_usdt", "vol_24h_usdt",
             "in_universe", "on_watchlist_today"] + [f"ret_{n}d" for n in RETURN_DAYS] + ["w_period", "m_period"] +
            [f"{p}_{c}" for p in ("d", "w", "m") for c in LATEST_COLS])
    sel = ", ".join(["period_start", "complete", "bar"] + LATEST_COLS)
    out = []
    for (sym,) in con.execute("SELECT DISTINCT symbol FROM candles_new WHERE tf='1d'").fetchall():
        d = con.execute(f"SELECT {sel} FROM indicators_new WHERE symbol=? AND tf='1d' ORDER BY period_start DESC LIMIT 1", (sym,)).fetchone()
        w = con.execute(f"SELECT {sel} FROM indicators_new WHERE symbol=? AND tf='1w' AND complete=1 ORDER BY period_start DESC LIMIT 1", (sym,)).fetchone()
        m = con.execute(f"SELECT {sel} FROM indicators_new WHERE symbol=? AND tf='1M' AND complete=1 ORDER BY period_start DESC LIMIT 1", (sym,)).fetchone()
        last_date, last_close = d[0], d[3]
        cl = dict(con.execute("SELECT date, close FROM daily WHERE symbol=? AND date IN (" +
                              ",".join("?" * len(RETURN_DAYS)) + ")",
                              (sym, *[(datetime.strptime(last_date, "%Y-%m-%d") - timedelta(days=n)).strftime("%Y-%m-%d") for n in RETURN_DAYS])))
        rets = []
        for n in RETURN_DAYS:
            p = cl.get((datetime.strptime(last_date, "%Y-%m-%d") - timedelta(days=n)).strftime("%Y-%m-%d"))
            rets.append(None if not p else (last_close / p - 1) * 100)
        vol = con.execute("SELECT volume_usdt FROM daily WHERE symbol=? AND date=?", (sym, last_date)).fetchone()[0]

        def vals(row, tf):
            if row is None:
                return [None] * len(LATEST_COLS)
            bar = row[2]
            return [v if (c == "close" or DICT.get(c) is None or bar >= warm_from(c, tf)) else None
                    for c, v in zip(LATEST_COLS, row[3:])]
        info = pairs.get(sym, (None, None, None))
        out.append([sym, info[0], info[1], info[2], last_date, last_close, vol, vol24.get(sym),
                    1 if sym in universe else 0, 1 if (sym, last_date) in watch_last else 0, *rets,
                    w[0] if w else None, m[0] if m else None, *vals(d, "1d"), *vals(w, "1w"), *vals(m, "1M")])
    con.execute(f"CREATE TABLE latest_new({', '.join(cols)})")
    con.executemany(f"INSERT INTO latest_new VALUES({','.join('?' * len(cols))})", out)


# ---------------------------------------------------------------- stats
def _median(xs):
    xs = sorted(xs)
    k = len(xs)
    return None if not k else (xs[k // 2] if k % 2 else (xs[k // 2 - 1] + xs[k // 2]) / 2)


def build_watch_stats(con):
    con.execute("CREATE TABLE watch_stats_new(cohort TEXT, horizon TEXT, n INTEGER, win_rate_pct REAL,"
                " avg_pct REAL, median_pct REAL, p10_pct REAL, p90_pct REAL)")
    base_sql = ("SELECT {cc} FROM forward_new f JOIN daily d ON d.symbol=f.symbol AND d.date=f.date"
                f" WHERE d.volume_usdt >= {WATCH['min_vol_usdt']} AND f.{{c}} IS NOT NULL")
    out = []
    for cohort, sql in (("watchlist_v0", "SELECT {cw} FROM watch_hits_new WHERE {c} IS NOT NULL"),
                        ("baseline_all_liquid_days", base_sql)):
        for c in FWD_COLS:
            xs = sorted(r[0] for r in con.execute(sql.format(c=c, cc=capped(c, "f"), cw=capped(c))))
            if not xs:
                continue
            k = len(xs)
            out.append((cohort, c, k, round(100 * sum(1 for x in xs if x > 0) / k, 2), round(sum(xs) / k, 3),
                        round(_median(xs), 3), round(xs[int(0.1 * (k - 1))], 3), round(xs[int(0.9 * (k - 1))], 3)))
    con.executemany("INSERT INTO watch_stats_new VALUES(?,?,?,?,?,?,?,?)", out)


SPLIT_DATE = "2022-01-01"


def build_signal_stats(con):
    """Scorecard: for every signal x timeframe x direction (and the volume-confirmed subset), what happened
    after entry at the signal candle's close. Only warm, closed signals on pairs that were liquid that day
    (daily volume >= $100k). Includes pairs that were later halted/delisted. Weekly/monthly entries are taken
    at the close of the period's last day."""
    con.executescript("""
    DROP TABLE IF EXISTS signal_stats_new;
    CREATE TABLE signal_stats_new(signal TEXT, tf TEXT, direction TEXT, variant TEXT, n INTEGER,
        win_7d REAL, avg_7d REAL, win_30d REAL, avg_30d REAL, avg_90d REAL, avg_max_dd_30d REAL,
        edge_7d REAL, edge_30d REAL, edge_30d_early REAL, edge_30d_late REAL, n_early INTEGER, n_late INTEGER,
        first_date TEXT, last_date TEXT);
    DROP VIEW IF EXISTS temp.fwdc;
    DROP TABLE IF EXISTS temp.s_entry;
    CREATE TEMP TABLE s_entry AS
      SELECT s.signal, s.tf, s.direction, s.confirmed, s.symbol,
             CASE s.tf WHEN '1d' THEN s.period_start WHEN '1w' THEN date(s.period_start, '+6 day')
                  ELSE date(s.period_start, '+1 month', '-1 day') END AS entry
      FROM signals_new s WHERE s.warm = 1 AND s.provisional = 0;
    """)
    con.execute("CREATE TEMP VIEW fwdc AS SELECT symbol, date, " +
                ", ".join(f"{capped(c)} AS {c}" for c in ("fwd_ret_7d", "fwd_ret_30d", "fwd_ret_90d")) +
                ", max_dd_30d FROM forward_new")
    base = {}
    for early, cond in ((None, "1=1"), (1, f"f.date < '{SPLIT_DATE}'"), (0, f"f.date >= '{SPLIT_DATE}'")):
        base[early] = con.execute(f"""SELECT AVG(f.fwd_ret_7d), AVG(f.fwd_ret_30d) FROM fwdc f
            JOIN daily d ON d.symbol = f.symbol AND d.date = f.date
            WHERE d.volume_usdt >= {MIN_VOL_USDT} AND {cond}""").fetchone()
    q = f"""
      SELECT e.signal, e.tf, e.direction, COUNT(f.fwd_ret_7d),
             100.0 * SUM(CASE WHEN f.fwd_ret_7d > 0 THEN 1 ELSE 0 END) / COUNT(f.fwd_ret_7d), AVG(f.fwd_ret_7d),
             100.0 * SUM(CASE WHEN f.fwd_ret_30d > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(f.fwd_ret_30d), 0), AVG(f.fwd_ret_30d),
             AVG(f.fwd_ret_90d), AVG(f.max_dd_30d),
             AVG(CASE WHEN e.entry < '{SPLIT_DATE}' THEN f.fwd_ret_30d END),
             AVG(CASE WHEN e.entry >= '{SPLIT_DATE}' THEN f.fwd_ret_30d END),
             COUNT(CASE WHEN e.entry < '{SPLIT_DATE}' THEN f.fwd_ret_30d END),
             COUNT(CASE WHEN e.entry >= '{SPLIT_DATE}' THEN f.fwd_ret_30d END),
             MIN(e.entry), MAX(e.entry)
      FROM s_entry e
      JOIN fwdc f ON f.symbol = e.symbol AND f.date = e.entry
      JOIN daily d ON d.symbol = e.symbol AND d.date = e.entry
      WHERE d.volume_usdt >= {MIN_VOL_USDT} AND f.fwd_ret_7d IS NOT NULL {{extra}}
      GROUP BY e.signal, e.tf, e.direction"""
    rows = []
    for variant, extra in (("all", ""), ("confirmed", "AND e.confirmed = 1")):
        for r in con.execute(q.format(extra=extra)):
            sig, tf, d, n, w7, a7, w30, a30, a90, dd, ae, al, ne, nl, fd, ld = r
            ed = lambda v, b: None if v is None or b is None else v - b
            rows.append((sig, tf, d, variant, n, w7, a7, w30, a30, a90, dd, ed(a7, base[None][0]), ed(a30, base[None][1]),
                         ed(ae, base[1][1]), ed(al, base[0][1]), ne, nl, fd, ld))
    con.executemany("INSERT INTO signal_stats_new VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT INTO signal_stats_new(signal, tf, direction, variant, n, avg_7d, avg_30d, edge_30d_early, edge_30d_late)"
                " VALUES('baseline_all_liquid_days', '1d', 'neutral', 'all', NULL, ?, ?, ?, ?)",
                (base[None][0], base[None][1], base[1][1], base[0][1]))
    con.execute("DROP TABLE temp.s_entry")
    con.execute("DROP VIEW temp.fwdc")


def build_weekly_report(con):
    wk = con.execute("SELECT MAX(period_start) FROM candles_new WHERE tf='1w' AND complete=1 AND symbol='BTCUSDT'").fetchone()[0]
    con.execute("CREATE TABLE weekly_report_new(week_start TEXT, section TEXT, symbol TEXT, value REAL, note TEXT)")
    if not wk:
        return
    we = (datetime.strptime(wk, "%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
    rows = []
    for s, fc in con.execute("SELECT symbol, first_candle FROM pairs WHERE first_candle BETWEEN ? AND ?", (wk, we)):
        rows.append((wk, "new_listing", s, None, f"first candle {fc}"))
    for s, dd in con.execute("SELECT symbol, delisted_on FROM pairs WHERE delisted_on BETWEEN ? AND ?", (wk, we)):
        rows.append((wk, "delisted_or_halted", s, None, f"stopped trading {dd}"))
    perf = con.execute("""SELECT c.symbol, (c.close / c.open - 1) * 100 FROM candles_new c
                          WHERE c.tf='1w' AND c.period_start=? AND c.complete=1
                            AND c.symbol IN (SELECT symbol FROM v_universe) ORDER BY 2 DESC""", (wk,)).fetchall()
    rows += [(wk, "top_gainer", s, v, None) for s, v in perf[:10]]
    rows += [(wk, "top_loser", s, v, None) for s, v in perf[-10:][::-1]]
    for name, d, cnt in con.execute("""SELECT signal, direction, COUNT(*) FROM signals_new WHERE tf='1w' AND period_start=?
                                       AND warm=1 AND symbol IN (SELECT symbol FROM v_universe) GROUP BY 1, 2""", (wk,)):
        rows.append((wk, "weekly_signal_count", None, cnt, f"{name} {d}"))
    for s, ev, dt in con.execute("SELECT symbol, event, date FROM universe_log WHERE date BETWEEN ? AND ?", (wk, we)):
        rows.append((wk, f"universe_{ev}", s, None, dt))
    con.executemany("INSERT INTO weekly_report_new VALUES(?,?,?,?,?)", rows)


# ---------------------------------------------------------------- highlights (persistent)
HIGHLIGHT_KINDS = {   # daily only (owner's choice); weekly/monthly events stay in `signals`
    "1d": {"sma9_x_sma21", "ema9_x_ema21", "ema50_x_ema200", "sma50_x_sma200", "ema100_x_ema200", "sma100_x_sma200",
           "price_x_ema100", "price_x_sma100", "price_x_ema200", "price_x_sma200", "reclaim_ema200", "reclaim_sma200",
           "lose_ema200", "lose_sma200", "macd_x_signal", "macd_x_zero", "macd_momentum_turn", "rsi_div_regular",
           "rsi_div_hidden", "macd_div_regular", "structure_up", "structure_down", "bos_up", "bos_down",
           "supertrend_flip", "price_x_cloud", "tk_cross", "price_x_dc55", "new_ath", "new_1y_high", "new_1y_low",
           "squeeze_breakout", "adx_x_25", "vol_spike_3x", "taker_buy_surge", "rs_new_high_90", "rs30_x_zero",
           "stochrsi_os_cross", "stochrsi_ob_cross", "confluence_strong", "confluence_weak"},
}


def log_highlights(con, stamp):
    """Append highlights for the latest bars. Each (symbol, tf, period, kind) is logged once, with the time it
    was first seen. Daily: latest closed day; weekly/monthly: latest closed candle."""
    first_run = con.execute("SELECT COUNT(*) FROM highlight_log").fetchone()[0] == 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_d = con.execute("SELECT MAX(period_start) FROM candles_new WHERE tf='1d'").fetchone()[0]
    since = {"1d": (datetime.strptime(last_d, "%Y-%m-%d") - timedelta(days=6 if first_run else 1)).strftime("%Y-%m-%d")}
    for tf in ("1w", "1M"):
        p = con.execute("SELECT MAX(period_start) FROM candles_new WHERE tf=? AND complete=1 AND symbol='BTCUSDT'", (tf,)).fetchone()[0]
        since[tf] = p or "9999"
    rows = []
    for tf, kinds in HIGHLIGHT_KINDS.items():
        for sym, ps, sig, d, close, conf, rv in con.execute(
                f"""SELECT symbol, period_start, signal, direction, close, confirmed, rvol FROM signals_new
                    WHERE tf=? AND period_start >= ? AND warm=1 AND provisional=0
                      AND symbol IN (SELECT symbol FROM v_universe)
                      AND signal IN ({','.join('?' * len(kinds))})""", (tf, since[tf], *kinds)):
            rows.append((sym, tf, ps, sig, d, json.dumps({"close": close, "confirmed": conf, "rvol": rv}), stamp))
    for sym, dt, rv, rsi in con.execute("SELECT symbol, date, rvol, rsi FROM watch_hits_new WHERE date >= ?", (since["1d"],)):
        rows.append((sym, "1d", dt, "watchlist_hit", "bullish", json.dumps({"rvol": rv, "rsi": rsi}), stamp))
    for sym, fc in con.execute("SELECT symbol, first_candle FROM pairs WHERE first_candle >= ?",
                               ((datetime.strptime(last_d, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d"),)):
        rows.append((sym, "1d", fc, "new_listing", "neutral", None, stamp))
    for sym, dd in con.execute("SELECT symbol, delisted_on FROM pairs WHERE delisted_on >= ?",
                               ((datetime.strptime(last_d, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d"),)):
        rows.append((sym, "1d", dd, "stopped_trading", "bearish", None, stamp))
    # universe membership changes
    current = {s: v for s, v in con.execute("""SELECT u.symbol, t.quote_volume_24h FROM v_universe u
        LEFT JOIN ticker_snapshots t ON t.symbol=u.symbol AND t.snap_date=(SELECT MAX(snap_date) FROM ticker_snapshots)""")}
    prev = {s for (s,) in con.execute("SELECT symbol FROM universe_state")}
    if prev:  # first run only seeds the state
        for s in set(current) - prev:
            con.execute("INSERT INTO universe_log VALUES(?,?,?,?,?)", (s, "entered", today, stamp, current[s]))
            rows.append((s, "1d", today, "entered_universe", "bullish", json.dumps({"vol_24h_usdt": current[s]}), stamp))
        for s in prev - set(current):
            con.execute("INSERT INTO universe_log VALUES(?,?,?,?,?)", (s, "left", today, stamp, None))
            rows.append((s, "1d", today, "left_universe", "bearish", None, stamp))
    con.execute("DELETE FROM universe_state")
    con.executemany("INSERT INTO universe_state VALUES(?,?)", [(s, today) for s in current])
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO highlight_log VALUES(?,?,?,?,?,?,?)", rows)
    return con.total_changes - before


# ---------------------------------------------------------------- run
SWAP = ("candles", "indicators", "signals", "latest", "forward", "watch_hits", "watch_stats", "dictionary",
        "series_info", "weekly_report", "signal_stats")


def run(con=None, verbose=True):
    t0 = time.time()
    own = con is None
    if own:
        con = sqlite3.connect(DB, timeout=60)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    from binance_sync import FILTERED_SQL          # single source of truth for the universe filter
    con.execute("DROP VIEW IF EXISTS v_universe")
    con.execute(f"CREATE VIEW v_universe AS {FILTERED_SQL}")
    con.commit()
    build_schema(con)
    hs = (con.execute("SELECT value FROM meta WHERE key='history_start'").fetchone() or [None])[0]
    syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily ORDER BY symbol")]
    q = "SELECT date, open, high, low, close, volume_base, volume_usdt, trades, taker_buy_usdt FROM daily WHERE symbol=? ORDER BY date"
    # BTC closes per timeframe, for relative strength
    btc = {}
    if "BTCUSDT" in syms:
        brows = segment(con.execute(q, ("BTCUSDT",)).fetchall())[0]
        for tf in ("1d", "1w", "1M"):
            btc[tf] = {c[0]: c[4] for c in resample(brows, tf, brows[-1][0], False)}
    for k, sym in enumerate(syms, 1):
        rows = con.execute(q, (sym,)).fetchall()
        if not rows:
            continue
        fc = con.execute("SELECT first_candle FROM pairs WHERE symbol=?", (sym,)).fetchone()
        truncated = bool(fc and fc[0] and hs and fc[0] <= hs)
        process_symbol(con, sym, rows, truncated, btc, stamp)
        if verbose and k % 100 == 0:
            print(f"  analytics {k}/{len(syms)} ({time.time() - t0:.0f}s)")
    con.commit()
    build_latest(con)
    build_watch_stats(con)
    build_weekly_report(con)
    build_signal_stats(con)
    new_hl = log_highlights(con, stamp)
    con.commit()
    con.execute("BEGIN")
    for t in SWAP:
        con.execute(f"DROP TABLE IF EXISTS {t}")
        con.execute(f"ALTER TABLE {t}_new RENAME TO {t}")
    con.execute("INSERT OR REPLACE INTO meta VALUES('analytics_built_utc', ?)", (stamp,))
    con.execute("INSERT OR REPLACE INTO meta VALUES('analytics_version', '4')")
    con.commit()
    for sql in ("CREATE INDEX IF NOT EXISTS ix_ind_tf ON indicators(tf, period_start)",
                "CREATE INDEX IF NOT EXISTS ix_sig ON signals(tf, period_start, signal)",
                "CREATE INDEX IF NOT EXISTS ix_sig_sym ON signals(symbol, tf)",
                "CREATE INDEX IF NOT EXISTS ix_hl_seen ON highlight_log(first_seen_utc)"):
        con.execute(sql)
    con.commit()
    if verbose:
        n = con.execute("SELECT COUNT(*) FROM indicators").fetchone()[0]
        s = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        print(f"Analytics v2: {len(syms)} symbols, {n:,} indicator rows, {s:,} signals, {new_hl} new highlights, "
              f"{time.time() - t0:.0f}s")
    if own:
        con.close()


if __name__ == "__main__":
    run()
