"""
On-chain tokenized US stocks (Python 3.9 stdlib only): the stocks and ETFs you can buy on Solana as tokens, from the
two regulated issuers whose tokens trade there, xStocks (Backed) and Ondo Global Markets. Read-only public APIs, no keys.

Only the real issuers' tokens are kept. Copycat tokens with the same names are everywhere (pump.fun "NVDAx" at a
millionth of a dollar), so a token counts only when ALL of these hold:
  * Jupiter marks it verified and tags it with the issuer ("xstocks" or "ondo");
  * its mint follows the issuer's address pattern (xStocks mints start "Xs", Ondo mints end "ondo");
  * its symbol is the stock ticker plus the issuer suffix ("NVDAx", "NVDAon") and its price is within 25% of the
    underlying stock's (a token tracking the stock cannot sit far from it).
Each token gets the underlying company's name, sector, industry and market cap from TradingView's screener, and its
own price, on-chain liquidity and 24h volume from Jupiter (Solana's swap router: it finds the best route across
Raydium, Orca, Meteora and the rest).

    python3 bin/onchain_stocks.py              refresh now and write web_export/onchain.json
    python3 bin/onchain_stocks.py --probe      what the public sources return (ops: onchain-probe)
Runs hourly from the engine (refresh() is throttled to EVERY_S).
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from world_markets import fetch, clean, US_SCAN  # noqa: E402

O_FETCH = fetch                # the real fetcher (tests pass their own, which never waits)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(ROOT, "data", "onchain.db")
OUT = os.path.join(ROOT, "web_export", "onchain.json")
EVERY_S, DISCOVER_S = 50 * 60, 20 * 3600
JUP_SEARCH = "https://lite-api.jup.ag/tokens/v2/search?query="
JUP_TAG = "https://lite-api.jup.ag/tokens/v2/tag?query="
ISSUERS = {
    "xstocks": {"name": "xStocks (Backed)", "suffix": "x", "tag": "xstocks", "ok": lambda m: m.startswith("Xs")},
    "ondo": {"name": "Ondo Global Markets", "suffix": "on", "tag": "ondo", "ok": lambda m: m.endswith("ondo")},
}
MIN_LIQ = 50_000            # a basket only uses tokens with at least this much on-chain liquidity (USD)
# tickers searched one by one when the issuer listings miss some (the largest US companies are added from the World data)
EXTRA = ["SPY", "QQQ", "IWM", "VTI", "DIA", "GLD", "SLV", "USO", "TLT", "IBIT", "SMH", "SOXL", "TQQQ", "XLK", "XLF", "XLE", "XLV",
         "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "AMD", "TSM", "INTC", "MU", "QCOM", "ARM", "ASML", "AMAT", "LRCX",
         "ORCL", "CRM", "ADBE", "PLTR", "NFLX", "IBM", "CSCO", "SMCI", "DELL", "COIN", "MSTR", "HOOD", "CRCL", "GME", "AMC", "BRK.B",
         "JPM", "V", "MA", "BAC", "GS", "LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE", "NVO", "XOM", "CVX", "KO", "PEP", "PG", "WMT", "COST",
         "MCD", "NKE", "DIS", "UBER", "ABNB", "SHOP", "BABA", "PDD", "RDDT", "APP", "SPOT", "MARA", "RIOT", "CLSK", "GLXY", "OPEN"]
# themes for the smart basket: built from the underlying company's industry / sector, plus a few named lists
THEMES = {
    "semis": {"label": "Semiconductors", "industries": ["Semiconductors"], "tickers": ["AMAT", "LRCX", "KLAC", "ASML", "SMH", "SOXL"]},
    "tech": {"label": "Technology", "sectors": ["Electronic Technology", "Technology Services"], "not_industries": ["Internet Software/Services"],
             "tickers": ["XLK", "QQQ", "TQQQ"]},
    "ai": {"label": "AI leaders", "tickers": ["NVDA", "MSFT", "GOOGL", "GOOG", "META", "AMD", "AVGO", "TSM", "PLTR", "ORCL", "SMCI", "ARM", "AMZN", "DELL", "CRWV", "APP"]},
    "internet": {"label": "Internet & media", "industries": ["Internet Software/Services", "Internet Retail", "Movies/Entertainment", "Cable/Satellite TV"]},
    "finance": {"label": "Banks & finance", "sectors": ["Finance"], "not_industries": ["Real Estate Investment Trusts"], "tickers": ["XLF"]},
    "crypto": {"label": "Crypto stocks", "tickers": ["COIN", "MSTR", "HOOD", "CRCL", "GLXY", "MARA", "RIOT", "CLSK", "IBIT", "BMNR", "SBET", "CIFR", "IREN", "BTGO"]},
    "health": {"label": "Health care", "sectors": ["Health Technology", "Health Services"], "tickers": ["XLV"]},
    "energy": {"label": "Energy", "sectors": ["Energy Minerals"], "tickers": ["XLE", "USO"]},
    "consumer": {"label": "Consumer brands", "sectors": ["Consumer Non-Durables", "Retail Trade", "Consumer Services", "Consumer Durables"]},
    "ev": {"label": "EV & autos", "industries": ["Motor Vehicles"], "tickers": ["TSLA", "RIVN", "LCID", "NIO"]},
    "index": {"label": "Index funds", "tickers": ["SPY", "QQQ", "IWM", "VTI", "DIA", "VOO"]},
    "gold": {"label": "Gold & commodities", "tickers": ["GLD", "SLV", "USO", "IAU", "GDX"]},
    "bonds": {"label": "Bonds", "tickers": ["TLT", "SHY", "IEF", "BND", "AGG"]},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens(mint TEXT PRIMARY KEY, issuer TEXT, symbol TEXT, underlying TEXT, name TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS quotes(mint TEXT PRIMARY KEY, data TEXT, utc TEXT);
CREATE TABLE IF NOT EXISTS hourly(mint TEXT, utc TEXT, price REAL, liq REAL, PRIMARY KEY(mint, utc));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS searched(q TEXT PRIMARY KEY, t INTEGER);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def issuer_of(t):
    """The issuer key when token dict t (a Jupiter token) is a real issuer token, else None (copycats, unverified)."""
    mint, sym, tags = str(t.get("id") or ""), str(t.get("symbol") or ""), set(t.get("tags") or [])
    if not t.get("isVerified"):
        return None
    for k, iss in ISSUERS.items():
        if iss["tag"] in tags and iss["ok"](mint) and sym.endswith(iss["suffix"]) and len(sym) > len(iss["suffix"]):
            return k
    return None


def underlying(sym, issuer):
    return sym[: -len(ISSUERS[issuer]["suffix"])].upper()


def jup_list(url, fetch=fetch, tries=3):
    """A Jupiter token list; waits and retries when rate-limited (HTTP 429) or briefly unavailable."""
    for k in range(tries):
        st, body = fetch(url, timeout=20)
        if st == 200:
            break
        if st not in (429, 500, 502, 503, 0) or k == tries - 1:
            return []
        time.sleep(6 * (k + 1) if fetch is O_FETCH else 0)
    else:
        return []
    try:
        d = json.loads(body)
    except ValueError:
        return []
    return d if isinstance(d, list) else []


def discover(con, fetch=fetch, candidates=(), log=print):
    """Find the issuers' tokens: their Jupiter tag listing first, then a search per candidate ticker. Returns how many
    real tokens are known."""
    found = {}
    for k, iss in ISSUERS.items():
        for t in jup_list(JUP_TAG + iss["tag"], fetch):
            if issuer_of(t) == k:
                found[t["id"]] = (k, t)
        for q in ("xStock" if k == "xstocks" else "Ondo Tokenized",):
            for t in jup_list(JUP_SEARCH + q.replace(" ", "%20"), fetch):
                if issuer_of(t) == k:
                    found[t["id"]] = (k, t)
    have = {underlying(t["symbol"], k) + "|" + k for k, t in found.values()}
    known = {u + "|" + i for u, i in con.execute("SELECT underlying, issuer FROM tokens")}
    recent = {q for (q,) in con.execute("SELECT q FROM searched WHERE t > ?", (int(time.time()) - 7 * 86400,))}
    for tk in candidates:
        for k, iss in ISSUERS.items():
            q = tk.replace(".", "") + iss["suffix"]
            if f"{tk}|{k}" in have or f"{tk.replace('.', '')}|{k}" in known or q in recent:
                continue                                   # already known, or searched without a hit this week
            for t in jup_list(JUP_SEARCH + q, fetch):
                if issuer_of(t) == k and underlying(t["symbol"], k) == tk.replace(".", ""):
                    found[t["id"]] = (k, t)
            con.execute("INSERT OR REPLACE INTO searched VALUES(?,?)", (q, int(time.time())))
            time.sleep(1.1 if fetch is O_FETCH else 0)     # Jupiter's free tier: about one request a second
    stamp = now_iso()
    for mint, (k, t) in found.items():
        con.execute("INSERT INTO tokens VALUES(?,?,?,?,?,?,?) ON CONFLICT(mint) DO UPDATE SET last_seen=excluded.last_seen, name=excluded.name",
                    (mint, k, t["symbol"], underlying(t["symbol"], k), clean(t.get("name"), 80), stamp, stamp))
    con.execute("INSERT OR REPLACE INTO meta VALUES('discovered', ?)", (str(int(time.time())),))
    con.commit()
    log(f"onchain: {len(found)} real issuer tokens found this pass")
    return con.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]


def quotes(con, fetch=fetch):
    """Price, liquidity, market cap and 24h stats for every known token (Jupiter, 50 mints a request)."""
    mints = [m for (m,) in con.execute("SELECT mint FROM tokens")]
    got = {}
    for i in range(0, len(mints), 25):
        for t in jup_list(JUP_SEARCH + ",".join(mints[i:i + 25]), fetch):
            if t.get("id") in mints and issuer_of(t):
                got[t["id"]] = t
        time.sleep(1.1 if fetch is O_FETCH else 0)
    stamp, hour = now_iso(), now_iso()[:13]
    for m, t in got.items():
        s24 = t.get("stats24h") or {}
        q = {"price": _num(t.get("usdPrice")), "liq": _num(t.get("liquidity")), "mcap": _num(t.get("mcap")),
             "vol24": (_num(s24.get("buyVolume")) or 0) + (_num(s24.get("sellVolume")) or 0), "chg24": _num(s24.get("priceChange")),
             "holders": t.get("holderCount"), "icon": t.get("icon") if str(t.get("icon") or "").startswith("https://") else None}
        con.execute("INSERT OR REPLACE INTO quotes VALUES(?,?,?)", (m, json.dumps(q), stamp))
        if q["price"]:
            con.execute("INSERT OR REPLACE INTO hourly VALUES(?,?,?,?)", (m, hour, q["price"], q["liq"]))
    con.commit()
    return len(got)


def stock_info(tickers, fetch=fetch):
    """{ticker: name, sector, industry, market cap, price, type} for the underlying stocks and ETFs (TradingView)."""
    out = {}
    cols = ["name", "description", "close", "change", "market_cap_basic", "sector", "industry", "type"]
    tl = sorted({t.replace(".", ".") for t in tickers})
    for i in range(0, len(tl), 150):
        q = {"filter": [{"left": "name", "operation": "in_range", "right": tl[i:i + 150]},
                        {"left": "exchange", "operation": "in_range", "right": ["NYSE", "NASDAQ", "AMEX", "CBOE"]},
                        {"left": "is_primary", "operation": "equal", "right": True}],
             "columns": cols, "range": [0, 400], "markets": ["america"]}
        st, body = fetch(US_SCAN, data=q, timeout=25)
        if st != 200:
            continue
        try:
            for d in json.loads(body).get("data", []):
                v = dict(zip(cols, d["d"]))
                if v["name"] in out:
                    continue
                out[v["name"]] = {"name": clean(v.get("description"), 70), "price": _num(v.get("close")), "chg": _num(v.get("change")),
                                  "mcap": _num(v.get("market_cap_basic")), "sector": clean(v.get("sector"), 40), "industry": clean(v.get("industry"), 50),
                                  "type": "ETF" if v.get("type") == "fund" else "Stock", "ticker": d["s"]}
        except (ValueError, KeyError, TypeError):
            continue
    return out


def themes_for(tk, info):
    out = []
    for k, th in THEMES.items():
        if tk in th.get("tickers", []):
            out.append(k)
            continue
        if not info or info.get("type") == "ETF":
            continue
        if th.get("industries") and info.get("industry") in th["industries"]:
            out.append(k)
        elif th.get("sectors") and info.get("sector") in th["sectors"] and info.get("industry") not in th.get("not_industries", []):
            out.append(k)
    return out


def build(con, info):
    rows = []
    for mint, iss, sym, tk, name in con.execute("SELECT mint, issuer, symbol, underlying, name FROM tokens ORDER BY underlying"):
        r = con.execute("SELECT data, utc FROM quotes WHERE mint=?", (mint,)).fetchone()
        if not r:
            continue
        q, u = json.loads(r[0]), info.get(tk) or info.get(tk.replace("BRKB", "BRK.B")) or {}
        if not q.get("price"):
            continue
        dev = (q["price"] / u["price"] - 1) if u.get("price") else None
        if dev is not None and abs(dev) > 0.25:
            continue                                   # far from the stock: not a faithful tracker (or a wrong match)
        spark = [p for (p,) in con.execute("SELECT price FROM (SELECT utc, price FROM hourly WHERE mint=? ORDER BY utc DESC LIMIT 168) ORDER BY utc", (mint,))]
        rows.append({"mint": mint, "chain": "solana", "issuer": iss, "symbol": sym, "ticker": tk, "name": u.get("name") or name,
                     "type": u.get("type") or ("ETF" if "ETF" in (name or "") else "Stock"), "sector": u.get("sector"), "industry": u.get("industry"),
                     "stock_mcap": u.get("mcap"), "stock_price": u.get("price"), "stock_chg": u.get("chg"), "premium_pct": None if dev is None else round(100 * dev, 2),
                     "price": q["price"], "chg24": q.get("chg24"), "liq": q.get("liq"), "vol24": q.get("vol24"), "token_mcap": q.get("mcap"),
                     "holders": q.get("holders"), "icon": q.get("icon"), "themes": themes_for(tk, u), "tradable": (q.get("liq") or 0) >= MIN_LIQ,
                     "spark": [float(f"{p:.6g}") for p in spark[-72:]], "as_of": r[1]})
    rows.sort(key=lambda x: -(x["liq"] or 0))
    return {"generated_utc": now_iso(), "rows": rows,
            "themes": {k: {"label": v["label"]} for k, v in THEMES.items()},
            "issuers": {k: v["name"] for k, v in ISSUERS.items()},
            "rules": {"min_liq": MIN_LIQ, "max_premium_pct": 25, "chain": "solana",
                      "verified": "Jupiter-verified tokens of the issuer (tag + mint pattern + ticker suffix), within 25% of the stock's price"},
            "credits": "Token data: Jupiter (Solana). Stock data: TradingView screener. Issuers: xStocks by Backed, Ondo Global Markets."}


def candidates(con_world=None):
    """Tickers to search: the fixed list plus the largest US companies of every World sector."""
    out = list(EXTRA)
    path = os.path.join(ROOT, "data", "world.db")
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for (d,) in c.execute("SELECT data FROM sector_cos"):
            out += [r["sym"] for r in json.loads(d)]
        c.close()
    except sqlite3.Error:
        pass
    return list(dict.fromkeys(re.sub(r"[^A-Z.]", "", t) for t in out if t))


def refresh(force=False, log=print, fetch=fetch, out=OUT, db=DB, cands=None):
    try:
        if not force and time.time() - os.path.getmtime(out) < EVERY_S:
            return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(db), exist_ok=True)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    con = sqlite3.connect(db, timeout=30)
    con.executescript(SCHEMA)
    last = con.execute("SELECT v FROM meta WHERE k='discovered'").fetchone()
    if force or not last or time.time() - int(last[0]) > DISCOVER_S:
        discover(con, fetch, cands if cands is not None else candidates(), log)
    n = quotes(con, fetch)
    if not n:
        con.close()
        log("onchain: no quotes this run; the old file stays")
        return False
    info = stock_info([u for (u,) in con.execute("SELECT DISTINCT underlying FROM tokens")] + ["BRK.B"], fetch)
    d = build(con, info)
    con.close()
    with open(out + ".tmp", "w") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(out + ".tmp", out)
    log(f"onchain: {len(d['rows'])} tokens published ({sum(r['tradable'] for r in d['rows'])} with ${MIN_LIQ:,} + liquidity)")
    return True


def probe():
    for k, iss in ISSUERS.items():
        L = jup_list(JUP_TAG + iss["tag"])
        print(f"== Jupiter tag {iss['tag']!r}: {len(L)} tokens, {sum(1 for t in L if issuer_of(t) == k)} real issuer tokens")
    L = jup_list(JUP_SEARCH + "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh,XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp")
    print(f"== Jupiter search by two mints: {len(L)} results: {[t.get('symbol') for t in L]}")
    info = stock_info(["NVDA", "AAPL", "SPY", "BRK.B", "GLD"])
    print("== TradingView underlying:", {k: (v["sector"], v["industry"], v["type"], v["price"]) for k, v in info.items()})


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    else:
        refresh(force=True)
