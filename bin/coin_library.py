"""
Coin library: CMC's top 1,000 crypto projects by market cap, with stablecoins, tokenized stocks / ETFs / gold / bonds
and wrapped or staked copies removed, each mapped to the exchanges we trust (Binance, KuCoin, Gate, MEXC), plus the
coins trending on CMC and CoinGecko searches. Python 3.9 stdlib only. Public data only, no keys.

Exchange matching is never by ticker alone: a coin is matched to an exchange's USDT pair only when the ticker matches
(or "1000" + ticker, priced per 1,000) AND the exchange's last price is within 10% of CMC's price; when several CMC
coins share a ticker the closest price wins, so a same-ticker different coin is never mapped.

Where it trades: Binance first (the engine already tracks every Binance USDT pair). A coin not on Binance is tracked
from whichever of KuCoin / Gate / MEXC trades the most USDT volume for it, when that is at least EXT_MIN_VOL a day;
those pairs are written to data/external_pairs.json, which venues.py reads. A pair once added stays tracked (its
history keeps growing) until its exchange stops listing it.

Market totals: CMC's daily global history split into BTC, ETH, stablecoins and everything else, so
TOTAL = all, TOTAL2 = all minus BTC, TOTAL3 = all minus BTC and ETH, and "alts ex stablecoins" = TOTAL3 minus stablecoins;
plus BTC / ETH / stablecoin dominance and CMC's altcoin season index.

Stored in data/library.db (coins today, a daily snapshot per coin, the global series); exported to
web_export/library.json and web_export/caps.json.
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from world_markets import fetch as _wfetch  # noqa: E402


def fetch(url, tv=False, data=None, timeout=30):
    return _wfetch(url, timeout=timeout, data=data, tv=tv)


CMC_LIST = ("https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listing?start={start}&limit={limit}&sortBy=market_cap"
            "&sortType=desc&convert=USD&cryptoType=all&tagType=all&audited=false&aux=tags,cmc_rank,date_added,platform,max_supply,"
            "circulating_supply,total_supply,self_reported_circulating_supply")


def cmc_listing(n=1000, page=200, log=print):
    """CMC's top n by market cap (its website's public data), rank order."""
    out = []
    for start in range(1, n + 1, page):
        for attempt in range(4):
            c, b = fetch(CMC_LIST.format(start=start, limit=min(page, n - start + 1)))
            if c == 200:
                break
            time.sleep(5 * (attempt + 1))
        if c != 200:
            raise RuntimeError(f"CMC listing from {start}: HTTP {c}")
        out += json.loads(b)["data"]["cryptoCurrencyList"]
        time.sleep(1.5)
    return out


def tags():
    from collections import Counter
    rows = cmc_listing(1000)
    print("sample row:", json.dumps(rows[700])[:1500])
    cnt = Counter(t for r in rows for t in r.get("tags", []))
    keys = ("stable", "wrap", "stak", "tokeniz", "bridg", "peg", "gold", "commod", "stock", "treasur", "rwa", "yield", "fiat", "meme", "real-world")
    for t, k in sorted(cnt.items(), key=lambda x: -x[1]):
        if any(x in t for x in keys):
            ex = [r["symbol"] for r in rows if t in r.get("tags", [])][:12]
            print(f"{t}: {k}  e.g. {' '.join(ex)}")
    print("most common tags:", cnt.most_common(40))


ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "data", "library.db")
BINANCE_DB = os.path.join(ROOT, "data", "binance.db")
EXT_FILE = os.path.join(ROOT, "data", "external_pairs.json")
OUT_LIB = os.path.join(ROOT, "web_export", "library.json")
OUT_CAPS = os.path.join(ROOT, "web_export", "caps.json")
TOP_N = 1000
EXT_MIN_VOL = 100_000          # USDT a day on the chosen exchange, to start tracking a coin from KuCoin / Gate / MEXC
PRICE_TOL = 0.10
EVERY_S = 3 * 3600

STABLE_TAGS = {"stablecoin", "asset-backed-stablecoin", "usd-stablecoin", "fiat-stablecoin", "eur-stablecoin", "algorithmic-stablecoin"}
STOCK_TAGS = {"tokenized-stock", "bstocks", "xstocks-ecosystem", "ondo-stocks-ecosystem", "tokenized-etfs", "robinhood-stock"}
GOLD_TAGS = {"tokenized-gold", "tokenized-commodities"}
WRAP_RE = re.compile(r"\b(wrapped|bridged|staked|restaked|binance-peg|liquid staked)\b", re.I)
SECTORS = [("Meme", {"memes"}), ("Layer 1", {"layer-1"}), ("Layer 2", {"layer-2"}), ("AI", {"ai-big-data", "ai-agents", "ai-memes"}),
           ("RWA", {"real-world-assets-protocols"}), ("DePIN", {"depin"}), ("Gaming", {"gaming", "metaverse", "play-to-earn"}),
           ("Exchange", {"centralized-exchange", "cex"}), ("DeFi", {"defi", "decentralized-exchange-dex-token", "yield-farming", "lending-borowing"}),
           ("Privacy", {"privacy"}), ("Payments", {"payments"}), ("Infrastructure", {"distributed-computing", "storage", "interoperability", "oracles"}),
           ("NFT", {"collectibles-nfts"}), ("Store of value", {"store-of-value"})]
VENUES = ("binance", "kucoin", "gate", "mexc")
VENUE_NAME = {"binance": "Binance", "kucoin": "KuCoin", "gate": "Gate", "mexc": "MEXC"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins(cmc_id INTEGER PRIMARY KEY, rank INTEGER, symbol TEXT, name TEXT, slug TEXT, sector TEXT,
    tags TEXT, chain TEXT, date_added TEXT, price REAL, mcap REAL, fdv REAL, vol24 REAL, ch24 REAL, ch7 REAL, ch30 REAL, ch1y REAL,
    excluded TEXT, venues TEXT, primary_venue TEXT, pair TEXT, trending INTEGER, updated_utc TEXT);
CREATE TABLE IF NOT EXISTS coin_daily(date TEXT, cmc_id INTEGER, rank INTEGER, price REAL, mcap REAL, vol24 REAL,
    PRIMARY KEY(date, cmc_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS global_daily(ts INTEGER PRIMARY KEY, total REAL, btc REAL, eth REAL, stable REAL, other REAL, volume REAL);
CREATE TABLE IF NOT EXISTS altseason(ts INTEGER PRIMARY KEY, idx REAL, alt_mcap REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def _json(url, tries=3, wait=4):
    last = None
    for k in range(tries):
        c, b = fetch(url)
        if c == 200:
            try:
                return json.loads(b)
            except ValueError as e:
                last = e
        else:
            last = RuntimeError(f"HTTP {c} from {url.split('?')[0]}")
        time.sleep(wait * (k + 1))
    raise last


def q(r):
    return (r.get("quotes") or [{}])[0]


def exclusion(r):
    """Why a CMC row is not a crypto project for our purposes, or None. Tags decide, with the price confirming a peg."""
    t, qt, name = set(r.get("tags") or []), q(r), r.get("name") or ""
    px, c30, c7 = qt.get("price") or 0, qt.get("percentChange30d") or 0, qt.get("percentChange7d") or 0
    if t & STABLE_TAGS and ((0.95 <= px <= 1.05) or ("eur-stablecoin" in t and 0.95 <= px <= 1.35)):
        return "stablecoin"
    if not t & STABLE_TAGS and 0.98 <= px <= 1.02 and abs(c30) < 2 and abs(c7) < 1.5:
        return "stablecoin (price held at $1)"
    if t & STOCK_TAGS and not (r.get("symbol") or "").upper().startswith("USD"):
        return "tokenized stock or ETF"
    if t & GOLD_TAGS:
        return "tokenized gold or commodity"
    if "tokenized-treasury-bonds" in t or (t & STOCK_TAGS and (r.get("symbol") or "").upper().startswith("USD")):
        return "tokenized bond fund"
    if "tokenized-assets" in t:
        return "tokenized asset"
    if WRAP_RE.search(name):
        return "wrapped or staked copy of another coin"
    return None


def sector(tags):
    t = set(tags or [])
    return next((name for name, ks in SECTORS if t & ks), "Other")


# ---------------------------------------------------------------- exchanges
def exchange_books():
    """{venue: {base: (last_price, usdt_volume_24h, native_symbol)}} for every USDT pair trading now. Binance's list comes
    from our own database (every Binance USDT pair the engine tracks) priced by Binance's live ticker."""
    out = {}
    try:
        con = sqlite3.connect(f"file:{BINANCE_DB}?mode=ro", uri=True, timeout=60)
        tok = dict(con.execute("SELECT symbol, token FROM pairs WHERE status='TRADING' AND symbol LIKE '%USDT'"))
        allsyms = {s for (s,) in con.execute("SELECT symbol FROM pairs")}
        con.close()
        px = {d["symbol"]: float(d["price"]) for d in _json("https://api.binance.com/api/v3/ticker/price")}
        vol = {}
        con = sqlite3.connect(f"file:{BINANCE_DB}?mode=ro", uri=True, timeout=60)
        vol = dict(con.execute("SELECT symbol, quote_volume_24h FROM ticker_snapshots WHERE snap_date=(SELECT MAX(snap_date) FROM ticker_snapshots)"))
        con.close()
        out["binance"] = {b: (px[s], vol.get(s, 0.0), s) for s, b in tok.items() if s in px}
        out["_binance_all"] = allsyms
    except Exception as e:
        out["binance"], out["_binance_all"] = {}, set()
        print(f"library: Binance list unavailable ({e})")
    try:
        sy = _json("https://api.kucoin.com/api/v2/symbols")["data"]
        ok = {x["symbol"] for x in sy if x.get("quoteCurrency") == "USDT" and x.get("enableTrading")}
        tk = _json("https://api.kucoin.com/api/v1/market/allTickers")["data"]["ticker"]
        out["kucoin"] = {t["symbol"][:-5]: (float(t["last"]), float(t.get("volValue") or 0), t["symbol"]) for t in tk
                         if t["symbol"] in ok and t.get("last")}
    except Exception as e:
        out["kucoin"] = {}
        print(f"library: KuCoin unavailable ({e})")
    try:
        cp = _json("https://api.gateio.ws/api/v4/spot/currency_pairs")
        ok = {x["id"] for x in cp if x.get("quote") == "USDT" and x.get("trade_status") == "tradable"}
        tk = _json("https://api.gateio.ws/api/v4/spot/tickers")
        out["gate"] = {t["currency_pair"][:-5]: (float(t["last"]), float(t.get("quote_volume") or 0), t["currency_pair"]) for t in tk
                       if t["currency_pair"] in ok and t.get("last")}
    except Exception as e:
        out["gate"] = {}
        print(f"library: Gate unavailable ({e})")
    try:
        info = _json("https://api.mexc.com/api/v3/exchangeInfo")["symbols"]
        ok = {x["symbol"]: x["baseAsset"] for x in info if x.get("quoteAsset") == "USDT" and str(x.get("status")) in ("1", "ENABLED", "TRADING")
              and x.get("isSpotTradingAllowed", True)}
        tk = _json("https://api.mexc.com/api/v3/ticker/24hr")
        out["mexc"] = {ok[t["symbol"]]: (float(t["lastPrice"]), float(t.get("quoteVolume") or 0), t["symbol"]) for t in tk
                       if t.get("symbol") in ok and t.get("lastPrice")}
    except Exception as e:
        out["mexc"] = {}
        print(f"library: MEXC unavailable ({e})")
    return out


def match(rows, books):
    """{cmc_id: {venue: (base, factor, last, vol, native)}}: ticker (or 1000 + ticker) and price within PRICE_TOL; one
    exchange pair goes to one coin only (the closest price)."""
    best = {}                                   # (venue, base) -> (error, cmc_id, factor)
    for r in rows:
        px = q(r).get("price") or 0
        sym = (r.get("symbol") or "").upper()
        if px <= 0 or not sym.isascii():
            continue
        for v in VENUES:
            bk = books.get(v) or {}
            for base, f in ((sym, 1), ("1000" + sym, 1000)):
                hit = bk.get(base)
                if not hit or hit[0] <= 0:
                    continue
                err = abs(hit[0] / (px * f) - 1)
                if err <= PRICE_TOL and err < best.get((v, base), (9, None, 1))[0]:
                    best[(v, base)] = (err, r["id"], f)
    claimed = {k for k in best}
    for r in rows:                              # second pass, coins with no match yet: the exchange may add letters
        px = q(r).get("price") or 0             # (CMC "BEAM" trades as "BEAMX"); a tighter 3% price check
        sym = (r.get("symbol") or "").upper()
        if px <= 0 or not sym.isascii() or len(sym) < 3 or any(c == r["id"] for _, c, _ in best.values()):
            continue
        for v in VENUES:
            for base, hit in (books.get(v) or {}).items():
                if (v, base) in claimed or not base.startswith(sym) or len(base) - len(sym) > 2 or hit[0] <= 0:
                    continue
                err = abs(hit[0] / px - 1)
                if err <= 0.03:
                    best[(v, base)] = (err, r["id"], 1)
                    claimed.add((v, base))
    out = {}
    for (v, base), (err, cid, f) in best.items():
        last, vol, native = books[v][base]
        cur = out.setdefault(cid, {}).get(v)
        if cur is None or vol > cur[3]:
            out[cid][v] = (base, f, last, vol, native)
    return out


def continues_binance(sym, venue, native, db=BINANCE_DB):
    """A Binance pair that stopped trading may be the same coin another exchange still trades (XMR) or a different coin
    that reused the ticker (Binance's old LIT was Litentry). Same coin when the other exchange's close on Binance's last
    trading day is within 3% of Binance's. Returns that day, or None (different coin, or not checkable)."""
    import venues as V
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
        st = con.execute("SELECT status FROM pairs WHERE symbol=?", (sym,)).fetchone()
        last = con.execute("SELECT date, close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
        con.close()
    except sqlite3.Error:
        return None
    if not st or st[0] == "TRADING" or not last or not last[1]:
        return None
    old = (V.EXTERNAL.get(sym), V.NATIVE.get(sym))
    V.EXTERNAL[sym], V.NATIVE[sym] = venue, native
    try:
        t = int(datetime.strptime(last[0], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        k = V.klines(None, {"symbol": sym, "interval": "1d", "startTime": t, "endTime": t, "limit": 1})
    except Exception:
        k = []
    finally:
        if old[0] is None:
            V.EXTERNAL.pop(sym, None), V.NATIVE.pop(sym, None)
        else:
            V.EXTERNAL[sym], V.NATIVE[sym] = old
    if k and k[0][0] == t and abs(float(k[0][4]) / last[1] - 1) <= 0.03:
        return last[0]
    return None


def trending():
    """Coins people are searching for right now: CMC's top searches and CoinGecko's trending list. [(source, symbol, name, price)]."""
    out = []
    try:
        d = _json("https://api.coinmarketcap.com/data-api/v3/topsearch/rank")["data"]["cryptoTopSearchRanks"]
        for x in d:
            out.append(("CMC", x.get("symbol"), x.get("name"), (x.get("priceChange") or {}).get("price"), x.get("id")))
    except Exception as e:
        print(f"library: CMC trending unavailable ({e})")
    try:
        d = _json("https://api.coingecko.com/api/v3/search/trending", tries=2, wait=10)["coins"]
        for x in d:
            it = x["item"]
            out.append(("CoinGecko", it.get("symbol"), it.get("name"), (it.get("data") or {}).get("price"), None))
    except Exception as e:
        print(f"library: CoinGecko trending unavailable ({e})")
    return out


# ---------------------------------------------------------------- market totals
def global_history(con, log=print):
    """CMC's daily global market cap split into BTC / ETH / stablecoins / the rest; full history the first time."""
    have = con.execute("SELECT MIN(ts) FROM global_daily").fetchone()[0]
    pts = []
    for rng in (("1y",) if have and have < time.time() - 3 * 365 * 86400 else ("all", "1y")):   # "all" is ~weekly; "1y" daily
        try:
            pts += _json(f"https://api.coinmarketcap.com/data-api/v4/global-metrics/quotes/historical?convertId=2781&range={rng}")["data"]["points"]
        except Exception as e:
            log(f"library: global history range {rng} failed ({str(e)[:80]})")
    if not pts:
        raise RuntimeError("CMC global history unavailable")
    rows = []
    for p in pts:
        try:
            tot, b, e, st, ot = (float(p[k]) for k in ("marketCap", "btcValue", "ethValue", "stableValue", "otherValue"))
        except (KeyError, TypeError, ValueError):
            continue
        if tot > 0 and abs(b + e + st + ot - tot) <= 0.02 * tot:      # the four parts must add up to the total
            rows.append((int(p["timestamp"]), tot, b, e, st, ot, float(p.get("volume") or 0)))
    con.executemany("INSERT OR REPLACE INTO global_daily VALUES(?,?,?,?,?,?,?)", rows)
    now = int(time.time())
    try:
        a = _json(f"https://api.coinmarketcap.com/data-api/v3/altcoin-season/chart?start={now - 4 * 365 * 86400}&end={now}")["data"]["points"]
        con.executemany("INSERT OR REPLACE INTO altseason VALUES(?,?,?)",
                        [(int(x["timestamp"]), float(x["altcoinIndex"]), float(x.get("altcoinMarketcap") or 0)) for x in a])
    except Exception as e:
        log(f"library: altcoin season index unavailable ({str(e)[:80]})")
    return len(rows)


def caps_export(con, now_utc):
    g = [list(r) for r in con.execute("SELECT ts, total, btc, eth, stable, other, volume FROM global_daily ORDER BY ts")]
    a = [list(r) for r in con.execute("SELECT ts, idx FROM altseason ORDER BY ts")]
    last = g[-1] if g else None

    def ago(days):
        if not g:
            return None
        t = last[0] - days * 86400
        return min(g, key=lambda r: abs(r[0] - t))

    def pack(r):
        tot, b, e, st, ot = r[1:6]
        return {"total": tot, "total2": tot - b, "total3": tot - b - e, "alts_ex_stables": ot, "btc": b, "eth": e, "stables": st,
                "btc_d": 100 * b / tot, "eth_d": 100 * e / tot, "stable_d": 100 * st / tot, "volume": r[6], "ts": r[0]}
    return {"generated_utc": now_utc, "source": "CoinMarketCap global metrics (daily)",
            "now": pack(last) if last else None, "d1": pack(ago(1)) if last else None, "d7": pack(ago(7)) if last else None,
            "d30": pack(ago(30)) if last else None, "d365": pack(ago(365)) if last else None,
            "altseason": {"now": a[-1][1] if a else None, "ts": a[-1][0] if a else None, "series": a},
            "columns": ["ts", "total", "btc", "eth", "stables", "other", "volume"], "series": g}


# ---------------------------------------------------------------- build
def build(log=print, db=DB, now=None, rows=None, books=None, tr=None, ext_file=EXT_FILE, out_lib=OUT_LIB, out_caps=OUT_CAPS, totals=None,
          binance_db=None):
    """rows / books / tr / totals / binance_db: injected data (tests); by default fetched live."""
    from venues import PINNED
    binance_db = {"db": binance_db} if binance_db else {}
    now_utc = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now_utc[:10]
    os.makedirs(os.path.dirname(db), exist_ok=True)
    con = sqlite3.connect(db, timeout=60)
    con.executescript(SCHEMA)
    rows = cmc_listing(TOP_N) if rows is None else rows
    if len(rows) < TOP_N * 0.9 and books is None:
        raise RuntimeError(f"CMC listing short: {len(rows)} rows")
    books = dict(exchange_books() if books is None else books)
    bin_all = books.pop("_binance_all", set())
    hits = match(rows, books)
    tr = trending() if tr is None else tr
    tr_syms = {(x[1] or "").upper() for x in tr}
    ext_prev = {}
    try:
        ext_prev = json.load(open(ext_file))
    except (OSError, ValueError):
        pass
    ext = dict(ext_prev)
    lib, excluded, counts = [], [], {"listed": len(rows), "binance": 0, "other_only": 0, "none": 0, "tracked_other": 0}
    for r in rows:
        qt, why = q(r), exclusion(r)
        h = hits.get(r["id"], {})
        prim = "binance" if "binance" in h else max((v for v in h if v != "binance"), key=lambda v: h[v][3], default=None)
        pair = None
        if prim == "binance":
            pair = h["binance"][4]
        elif prim:
            base, f, last, vol, native = h[prim]
            sym = base + "USDT"
            mine = sym in ext and (ext[sym].get("cmc_id") == r["id"] or (ext[sym].get("venue") == prim and ext[sym].get("native") == native))
            ok = not why and vol >= EXT_MIN_VOL and sym.isascii() and sym.isalnum()
            cont = continues_binance(sym, prim, native, **binance_db) if ok and sym not in ext and sym in bin_all else None
            if mine or sym in PINNED or (sym not in ext and ok and (sym not in bin_all or cont)):
                if sym not in ext and sym not in PINNED:
                    ext[sym] = {"venue": prim, "native": native, "base": base, "cmc_id": r["id"], "name": r.get("name"), "added": today}
                    if cont:
                        ext[sym]["continues_binance_after"] = cont      # same coin: Binance's history, then this exchange
                pair = sym
        vset = "".join(v[0].upper() for v in VENUES if v in h)          # B K G M
        rec = (r["id"], r.get("cmcRank"), r.get("symbol"), r.get("name"), r.get("slug"), sector(r.get("tags")), ",".join(r.get("tags") or []),
               (r.get("platform") or {}).get("name"), (r.get("dateAdded") or "")[:10], qt.get("price"), qt.get("marketCap"),
               qt.get("fullyDilluttedMarketCap"), qt.get("volume24h"), qt.get("percentChange24h"), qt.get("percentChange7d"),
               qt.get("percentChange30d"), qt.get("percentChange1y"), why, vset, prim, pair, int((r.get("symbol") or "").upper() in tr_syms), now_utc)
        con.execute("INSERT OR REPLACE INTO coins VALUES(" + ",".join("?" * 23) + ")", rec)
        con.execute("INSERT OR REPLACE INTO coin_daily VALUES(?,?,?,?,?,?)", (today, r["id"], r.get("cmcRank"), qt.get("price"), qt.get("marketCap"), qt.get("volume24h")))
        if why:
            excluded.append([r.get("cmcRank"), r.get("symbol"), r.get("name"), why])
            continue
        counts["binance" if prim == "binance" else "other_only" if prim else "none"] += 1
        if prim and prim != "binance" and pair:
            counts["tracked_other"] += 1
        lib.append([r.get("cmcRank"), r["id"], r.get("symbol"), r.get("name"), r.get("slug"), sector(r.get("tags")), qt.get("price"),
                    qt.get("marketCap"), qt.get("fullyDilluttedMarketCap"), qt.get("volume24h"), qt.get("percentChange24h"),
                    qt.get("percentChange7d"), qt.get("percentChange30d"), qt.get("percentChange1y"), (r.get("dateAdded") or "")[:10],
                    (r.get("platform") or {}).get("name"), vset, VENUE_NAME.get(prim), pair,
                    round(h[prim][3]) if prim else None, int((r.get("symbol") or "").upper() in tr_syms)])
    in_top = {(r.get("symbol") or "").upper() for r in rows}
    for src, sy, nm, px, _ in tr:              # hyped coins outside the top 1,000: tracked when an exchange we trust trades them
        sy = (sy or "").upper()
        try:
            px = float(px or 0)
        except (TypeError, ValueError):
            px = 0
        if sy in in_top or px <= 0 or not sy.isascii() or not sy.isalnum():
            continue
        cands = [(v, books[v][sy]) for v in ("kucoin", "gate", "mexc") if sy in (books.get(v) or {}) and abs(books[v][sy][0] / px - 1) <= PRICE_TOL]
        bh = "binance" if sy in (books.get("binance") or {}) and abs(books["binance"][sy][0] / px - 1) <= PRICE_TOL else None
        if bh or not cands:
            continue
        v, (last, vol, nat) = max(cands, key=lambda c: c[1][1])
        sym = sy + "USDT"
        if vol >= EXT_MIN_VOL and sym not in bin_all and sym not in ext:
            ext[sym] = {"venue": v, "native": nat, "base": sy, "cmc_id": None, "name": nm, "added": today, "why": f"trending on {src}"}
    hot = [{"source": s, "symbol": sy, "name": nm, "in_top": (sy or "").upper() in in_top} for s, sy, nm, _, _ in tr]
    for x in set(ext) - set(ext_prev):
        log(f"library: new pair {x} from {VENUE_NAME[ext[x]['venue']]} ({ext[x]['name']})")
    try:
        n = (totals or global_history)(con, log)
    except Exception as e:
        n = 0
        log(f"library: market totals skipped ({str(e)[:120]})")
    con.execute("INSERT OR REPLACE INTO meta VALUES('built_utc', ?)", (now_utc,))
    con.commit()
    counts["excluded"] = len(excluded)
    from collections import Counter
    counts["excluded_by"] = dict(Counter(x[3] for x in excluded))
    lib_out = {"generated_utc": now_utc, "source": "CoinMarketCap top 1,000 by market cap; exchanges' public market data",
               "counts": counts, "trending": hot,
               "columns": ["rank", "cmc_id", "symbol", "name", "slug", "sector", "price", "mcap", "fdv", "vol24", "ch24", "ch7", "ch30", "ch1y",
                           "added", "chain", "venues", "primary", "pair", "venue_vol24", "trending"],
               "rows": lib, "excluded": excluded}
    caps = caps_export(con, now_utc)
    con.close()
    for path, d in ((out_lib, lib_out), (out_caps, caps)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(d, f, separators=(",", ":"))
        os.replace(path + ".tmp", path)
    with open(ext_file + ".tmp", "w") as f:
        json.dump(ext, f, indent=1, sort_keys=True)
    os.replace(ext_file + ".tmp", ext_file)
    log(f"library: {len(lib)} coins kept of {len(rows)} ({len(excluded)} excluded); on Binance {counts['binance']}, "
        f"only elsewhere {counts['other_only']} ({counts['tracked_other']} tracked), on none of our exchanges {counts['none']}; "
        f"{len(ext)} pairs from other exchanges; market totals {n} days")
    return lib_out


def refresh(force=False, log=print):
    try:
        if not force and time.time() - os.path.getmtime(OUT_LIB) < EVERY_S:
            return False
    except OSError:
        pass
    build(log=log)
    return True


def status():
    d = json.load(open(OUT_LIB))
    c = d["counts"]
    print(f"built {d['generated_utc']}: {len(d['rows'])} kept of {c['listed']}; excluded {c['excluded']} {c['excluded_by']}")
    print(f"on Binance {c['binance']}; only on KuCoin/Gate/MEXC {c['other_only']} ({c['tracked_other']} tracked, >= ${EXT_MIN_VOL:,} a day); on none {c['none']}")
    from collections import Counter
    print("primary venue:", dict(Counter(r[17] for r in d["rows"])))
    print("sectors:", dict(Counter(r[5] for r in d["rows"]).most_common()))
    ext = json.load(open(EXT_FILE))
    print(f"external pairs: {len(ext)}", dict(Counter(v["venue"] for v in ext.values())))
    print("top 15 not on Binance:", [(r[0], r[2], r[17], r[19]) for r in d["rows"] if r[17] and r[17] != "Binance"][:15])
    print("top 15 on none:", [(r[0], r[2]) for r in d["rows"] if not r[17]][:15])
    print("excluded sample:", d["excluded"][:25])
    print("trending:", [(t["source"], t["symbol"], t["in_top"]) for t in d["trending"]])
    cp = json.load(open(OUT_CAPS))
    n = cp["now"]
    if n:
        print(f"  BTC ${n['btc'] / 1e9:,.1f}B ETH ${n['eth'] / 1e9:,.1f}B stables ${n['stables'] / 1e9:,.1f}B rest ${n['alts_ex_stables'] / 1e9:,.1f}B; d30 TOTAL ${cp['d30']['total'] / 1e12:.3f}T")
        g = cp["series"]
        gaps = [round((b[0] - a[0]) / 86400, 1) for a, b in zip(g, g[1:])]
        print(f"  spacing: last 365 points median {sorted(gaps[-365:])[len(gaps[-365:]) // 2]} days, max {max(gaps[-365:])}")
        print(f"caps {datetime.fromtimestamp(n['ts'], timezone.utc):%Y-%m-%d}: TOTAL ${n['total'] / 1e12:.3f}T, TOTAL2 ${n['total2'] / 1e12:.3f}T, TOTAL3 ${n['total3'] / 1e12:.3f}T, "
              f"BTC.D {n['btc_d']:.2f}%, ETH.D {n['eth_d']:.2f}%, stables {n['stable_d']:.2f}%; series {len(cp['series'])} days from "
              f"{datetime.fromtimestamp(cp['series'][0][0], timezone.utc):%Y-%m-%d}; altseason {cp['altseason']['now']}")


def probe():
    now = int(time.time())
    tests = [
        ("CMC listing", "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listing?start=1&limit=3&sortBy=market_cap&sortType=desc&convert=USD&cryptoType=all&tagType=all&audited=false&aux=tags,cmc_rank,circulating_supply"),
        ("CMC listing 1000", "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listing?start=901&limit=100&sortBy=market_cap&sortType=desc&convert=USD&cryptoType=all&tagType=all&audited=false"),
        ("CMC global hist v3", f"https://api.coinmarketcap.com/data-api/v3/global-metrics/quotes/historical?format=chart&interval=1d&timeStart={now - 400 * 86400}&timeEnd={now}"),
        ("CMC global hist v4", "https://api.coinmarketcap.com/data-api/v4/global-metrics/quotes/historical?convertId=2781&range=1y"),
        ("CMC global now", "https://api.coinmarketcap.com/data-api/v4/global-metrics/quotes/latest"),
        ("CMC altcoin season", "https://api.coinmarketcap.com/data-api/v3/altcoin-season/chart?start=" + str(now - 90 * 86400) + "&end=" + str(now)),
        ("CMC trending", "https://api.coinmarketcap.com/data-api/v3/topsearch/rank"),
        ("CMC trending2", "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/spotlight?dataType=2&limit=10&rankRange=0&timeframe=24h"),
        ("CG markets", "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=4"),
        ("CG global", "https://api.coingecko.com/api/v3/global"),
        ("CG stable cat", "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&category=stablecoins&per_page=250&page=1"),
        ("CG trending", "https://api.coingecko.com/api/v3/search/trending"),
        ("CG mexc tickers", "https://api.coingecko.com/api/v3/exchanges/mexc/tickers?page=1"),
        ("CG global chart", "https://api.coingecko.com/api/v3/global/market_cap_chart?days=365"),
        ("KuCoin symbols", "https://api.kucoin.com/api/v2/symbols"),
        ("KuCoin tickers", "https://api.kucoin.com/api/v1/market/allTickers"),
        ("KuCoin 1d BTC old", f"https://api.kucoin.com/api/v1/market/candles?type=1day&symbol=BTC-USDT&startAt={1500000000}&endAt={1500000000 + 1500 * 86400}"),
        ("Gate pairs", "https://api.gateio.ws/api/v4/spot/currency_pairs"),
        ("Gate tickers", "https://api.gateio.ws/api/v4/spot/tickers"),
        ("Gate 1d BTC old", f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=BTC_USDT&interval=1d&from={1500000000}&to={1500000000 + 999 * 86400}"),
        ("Gate 1d BTC recent", f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=BTC_USDT&interval=1d&from={now - 900 * 86400}&to={now}"),
        ("MEXC info", "https://api.mexc.com/api/v3/exchangeInfo"),
        ("MEXC 1d BTC old", f"https://api.mexc.com/api/v3/klines?symbol=BTCUSDT&interval=1d&startTime={1600000000000}&endTime={1600000000000 + 499 * 86400000}&limit=500"),
        ("Llama stables", "https://stablecoins.llama.fi/stablecoincharts/all"),
    ]
    for name, url in tests:
        c, b = fetch(url)
        snip = b[:260].replace("\n", " ")
        print(f"{name}: HTTP {c}, {len(b):,} bytes | {snip}")
        time.sleep(1.5)
    c, b = fetch("https://scanner.tradingview.com/crypto/scan", data={"symbols": {"tickers": [
        "CRYPTOCAP:TOTAL", "CRYPTOCAP:TOTAL2", "CRYPTOCAP:TOTAL3", "CRYPTOCAP:OTHERS", "CRYPTOCAP:BTC.D", "CRYPTOCAP:USDT.D", "CRYPTOCAP:STABLE.C.D"]},
        "columns": ["close", "change", "description"]})
    print(f"TV cryptocap: HTTP {c} | {b[:600]}")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    elif "--tags" in sys.argv:
        tags()
    elif "--status" in sys.argv:
        status()
    else:
        build()
