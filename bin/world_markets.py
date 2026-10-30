"""
World markets for the website's "World" tab (Python 3.9 stdlib only). Refreshed once an hour by the engine.

What it collects (public data only; no key, no account):
  * prices: stock indexes by region, commodities, currencies, government bond yields, US sectors and the largest US
    companies, from TradingView's public screener (one request for everything: price, day / week / month / 3-month /
    year-to-date / 1-year change, 52-week range; some quotes delayed up to 15 minutes). Every hour's price is kept in
    the database, so the history (and the sparklines) grow from the first run on.
  * news: headlines from public RSS feeds (central banks, business desks, Google News searches that carry Reuters,
    Bloomberg, FT ... headlines). Only the headline, the source, the time and the link are kept and shown: never the
    article text. Each headline is tagged (central banks, inflation and data, geopolitics and trade, energy, ...)
    and scored for likely market impact.
  * the economic calendar of the week (Forex Factory's public weekly file): events, impact, forecast, previous.
  * sentiment: VIX (from the prices) and the crypto Fear & Greed index (alternative.me).
Everything fetched is untrusted text: titles are stripped of markup and cut, links must be plain http(s), and the
page shows them as text. The database (data/world.db) keeps every daily close and every headline.

    python3 bin/world_markets.py            refresh now (ignores the hourly throttle) and print a summary
    python3 bin/world_markets.py --probe    try every source once and print which ones answer (ops: world-probe)
Output: web_export/world.json
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(ROOT, "data", "world.db")
OUT = os.path.join(ROOT, "web_export", "world.json")
EVERY_S = 55 * 60
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MAX_BODY = 3_000_000

# (id, label, group, TradingView ticker(s), unit). "A|B|C": the first ticker that answers is used.   unit: "" index points, "$" price, "%" yield, "fx" exchange rate
INSTRUMENTS = [
    ("spx", "S&P 500", "Americas", "SP:SPX", ""),
    ("ndx", "Nasdaq 100", "Americas", "NASDAQ:NDX", ""),
    ("dji", "Dow Jones", "Americas", "DJ:DJI", ""),
    ("rut", "Russell 2000", "Americas", "TVC:RUT", ""),
    ("vix", "VIX (fear gauge)", "Americas", "TVC:VIX", ""),
    ("tsx", "Canada TSX", "Americas", "TSX:TSX", ""),
    ("ibov", "Brazil Bovespa", "Americas", "BMFBOVESPA:IBOV", ""),
    ("sx5e", "Euro Stoxx 50", "Europe", "TVC:SX5E", ""),
    ("dax", "Germany DAX", "Europe", "XETR:DAX", ""),
    ("ftse", "UK FTSE 100", "Europe", "TVC:UKX", ""),
    ("cac", "France CAC 40", "Europe", "EURONEXT:PX1", ""),
    ("n225", "Japan Nikkei 225", "Asia & Middle East", "TVC:NI225", ""),
    ("hsi", "Hong Kong Hang Seng", "Asia & Middle East", "TVC:HSI", ""),
    ("shc", "China Shanghai", "Asia & Middle East", "SSE:000001", ""),
    ("kospi", "Korea KOSPI", "Asia & Middle East", "KRX:KOSPI", ""),
    ("nifty", "India Nifty 50", "Asia & Middle East", "NSE:NIFTY", ""),
    ("asx", "Australia ASX 200", "Asia & Middle East", "ASX:XJO|TVC:XJO|ASX:XJO1!|ASX24:AP1!", ""),
    ("tasi", "Saudi Tadawul", "Asia & Middle East", "TADAWUL:TASI", ""),
    ("kse", "Pakistan KSE-100", "Asia & Middle East", "PSX:KSE100", ""),
    ("gold", "Gold", "Commodities", "TVC:GOLD", "$"),
    ("silver", "Silver", "Commodities", "TVC:SILVER", "$"),
    ("brent", "Brent crude", "Commodities", "TVC:UKOIL|ICEEUR:BRN1!|NYMEX:BZ1!|FX:UKOIL", "$"),
    ("wti", "WTI crude", "Commodities", "TVC:USOIL|NYMEX:CL1!|FX:USOIL", "$"),
    ("natgas", "Natural gas (US)", "Commodities", "NYMEX:NG1!", "$"),
    ("copper", "Copper", "Commodities", "COMEX:HG1!", "$"),
    ("platinum", "Platinum", "Commodities", "TVC:PLATINUM", "$"),
    ("wheat", "Wheat", "Commodities", "CBOT:ZW1!", "$"),
    ("corn", "Corn", "Commodities", "CBOT:ZC1!", "$"),
    ("dxy", "US Dollar index", "Currencies", "TVC:DXY", ""),
    ("eurusd", "EUR / USD", "Currencies", "FX:EURUSD", "fx"),
    ("usdjpy", "USD / JPY", "Currencies", "FX:USDJPY", "fx"),
    ("gbpusd", "GBP / USD", "Currencies", "FX:GBPUSD", "fx"),
    ("usdcny", "USD / CNY", "Currencies", "FX_IDC:USDCNY", "fx"),
    ("usdchf", "USD / CHF", "Currencies", "FX:USDCHF", "fx"),
    ("usdpkr", "USD / PKR", "Currencies", "FX_IDC:USDPKR", "fx"),
    ("usdinr", "USD / INR", "Currencies", "FX_IDC:USDINR", "fx"),
    ("us3m", "US 3-month", "Rates", "TVC:US03MY", "%"),
    ("us2y", "US 2-year", "Rates", "TVC:US02Y", "%"),
    ("us10y", "US 10-year", "Rates", "TVC:US10Y", "%"),
    ("us30y", "US 30-year", "Rates", "TVC:US30Y", "%"),
    ("de10y", "Germany 10-year", "Rates", "TVC:DE10Y", "%"),
    ("gb10y", "UK 10-year", "Rates", "TVC:GB10Y", "%"),
    ("jp10y", "Japan 10-year", "Rates", "TVC:JP10Y", "%"),
    ("btc", "Bitcoin", "Crypto", "BINANCE:BTCUSDT", "$"),
    ("eth", "Ethereum", "Crypto", "BINANCE:ETHUSDT", "$"),
]
SECTORS = [("AMEX:XLK", "Technology"), ("NASDAQ:SMH", "Semiconductors"), ("AMEX:XLC", "Communication"),
           ("AMEX:XLY", "Consumer discretionary"), ("AMEX:XLF", "Financials"), ("AMEX:XLV", "Health care"),
           ("AMEX:XLI", "Industrials"), ("AMEX:XLE", "Energy"), ("AMEX:XLB", "Materials"), ("AMEX:XLP", "Consumer staples"),
           ("AMEX:XLU", "Utilities"), ("AMEX:XLRE", "Real estate")]
MEGACAPS = [("NASDAQ:NVDA", "Nvidia"), ("NASDAQ:MSFT", "Microsoft"), ("NASDAQ:AAPL", "Apple"), ("NASDAQ:GOOGL", "Alphabet"),
            ("NASDAQ:AMZN", "Amazon"), ("NASDAQ:META", "Meta"), ("NASDAQ:AVGO", "Broadcom"), ("NASDAQ:TSLA", "Tesla"),
            ("NYSE:BRK.B", "Berkshire Hathaway"), ("NYSE:JPM", "JPMorgan")]
SCAN = "https://scanner.tradingview.com/global/scan"
COLS = ["close", "change", "change_abs", "Perf.W", "Perf.1M", "Perf.3M", "Perf.YTD", "Perf.Y", "price_52_week_high",
        "price_52_week_low", "update_mode", "currency"]

FEEDS = [  # (name, url)
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("ECB", "https://www.ecb.europa.eu/rss/press.html"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("OilPrice", "https://oilprice.com/rss/main"),
    ("Dawn Business", "https://www.dawn.com/feeds/business"),
    ("Business Recorder", "https://www.brecorder.com/feeds/latest-news"),
    ("Google News: crypto", "https://news.google.com/rss/search?q=bitcoin+OR+crypto+market+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: Asia & emerging", "https://news.google.com/rss/search?q=China+economy+OR+Japan+markets+OR+India+markets+OR+Pakistan+economy+when:2d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: markets", "https://news.google.com/rss/search?q=stock+market+OR+stocks+OR+wall+street+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: central banks", "https://news.google.com/rss/search?q=%22Federal+Reserve%22+OR+ECB+OR+%22Bank+of+Japan%22+OR+%22interest+rates%22+when:2d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: oil & commodities", "https://news.google.com/rss/search?q=oil+prices+OR+OPEC+OR+gold+prices+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: trade & geopolitics", "https://news.google.com/rss/search?q=tariffs+OR+sanctions+OR+%22trade+war%22+when:2d&hl=en-US&gl=US&ceid=US:en"),
]
CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FNG = "https://api.alternative.me/fng/?limit=8"

# headline tags and how much each says about market impact (the highest tag wins, words add a little)
TAGS = [
    ("Central banks", 3, r"\b(fed|federal reserve|fomc|powell|ecb|lagarde|bank of japan|boj|bank of england|boe|pboc|rate (cut|hike)|interest rates?|monetary policy|central bank|state bank of pakistan|sbp)\b"),
    ("Inflation & data", 3, r"\b(inflation|cpi|ppi|pce|payrolls?|jobs report|unemployment|gdp|recession|retail sales|pmi|jobless claims|consumer confidence)\b"),
    ("Geopolitics & trade", 3, r"\b(tariffs?|trade war|sanctions?|war|ceasefire|missile|invasion|conflict|election|shutdown|debt ceiling|default)\b"),
    ("Energy", 2, r"\b(oil|crude|opec|brent|wti|natural gas|lng|refiner)"),
    ("Metals", 2, r"\b(gold|silver|copper|platinum|lithium)\b"),
    ("Earnings & companies", 2, r"\b(earnings|guidance|revenue|profit|quarterly results|ipo|merger|acquisition|buyback|layoffs?)\b"),
    ("Crypto", 1, r"\b(bitcoin|crypto|ethereum|stablecoin|etf flows?)\b"),
    ("Pakistan", 1, r"\b(pakistan|psx|kse|sbp|rupee|pkr|karachi|islamabad|imf programme|imf program)\b"),
    ("Markets", 1, r"\b(stocks?|shares|s&p|nasdaq|dow|equities|bonds?|yields?|treasur|dollar|yen|euro|selloff|rally|record high)\b"),
]
HOT = r"\b(surge|plunge|crash|soar|slump|tumble|record|emergency|unexpected|shock|biggest|warns?|halt|collapse)\w*"


# ------------------------------------------------------------------ fetching
def fetch(url, timeout=15, data=None, jar=None):
    """GET (or POST `data` as JSON) a public URL with curl (no credentials ever). Returns (status, body text)."""
    cfg = f'url = "{url}"\nheader = "User-Agent: {UA}"\nheader = "Accept: */*"\n'
    if data is not None:
        cfg += 'header = "Content-Type: application/json"\nheader = "Origin: https://www.tradingview.com"\n' \
               'header = "Referer: https://www.tradingview.com/"\n'
    args = ["curl", "-sS", "-L", "--max-redirs", "3", "--compressed", "--max-time", str(timeout),
            "--max-filesize", str(MAX_BODY), "-w", "\n%{http_code}", "--config", "-"]
    if jar:
        args += ["-b", jar, "-c", jar]
    if data is not None:
        args += ["--data-binary", "@" + _tmpbody(data)]
    try:
        p = subprocess.run(args, input=cfg, capture_output=True, text=True, errors="replace")
    finally:
        if data is not None:
            os.remove(args[args.index("--data-binary") + 1][1:])
    if p.returncode != 0:
        return 0, ""
    body, _, code = p.stdout.rpartition("\n")
    return int(code or 0), body


def _tmpbody(data):
    import tempfile
    fd, path = tempfile.mkstemp(prefix="wm_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data))
    return path


def scan(tickers, fetch=fetch):
    """TradingView's public screener: latest price and performance for many symbols in one request.
    Returns {ticker: {column: value}}."""
    st, body = fetch(SCAN, data={"symbols": {"tickers": tickers}, "columns": COLS}, timeout=25)
    if st != 200:
        return {}
    try:
        return {x["s"]: dict(zip(COLS, x["d"])) for x in json.loads(body).get("data", [])}
    except (ValueError, KeyError, TypeError):
        return {}


# ------------------------------------------------------------------ text hygiene
TAG_RE = re.compile(r"<[^>]+>")


def clean(s, n=240):
    import html
    s = html.unescape(TAG_RE.sub(" ", s or ""))
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def safe_link(u):
    u = (u or "").strip()
    return u if re.match(r"^https?://[^\s<>\"']+$", u) and len(u) < 600 else None


def parse_feed(body, source):
    """RSS or Atom -> [{"title", "link", "t" (unix), "source"}]."""
    out = []
    try:
        root = ET.fromstring(body.encode("utf-8", "replace"))
    except ET.ParseError:
        return out
    strip = lambda tag: tag.rsplit("}", 1)[-1]          # noqa: E731
    for it in root.iter():
        if strip(it.tag) not in ("item", "entry"):
            continue
        f = {strip(c.tag): c for c in it}
        title = clean(f["title"].text if "title" in f else "")
        link = None
        if "link" in f:
            link = f["link"].get("href") or f["link"].text
        link = safe_link(link)
        when = None
        for k in ("pubDate", "published", "updated", "date"):
            if k in f and f[k].text:
                try:
                    when = parsedate_to_datetime(f[k].text.strip()).timestamp()
                except (TypeError, ValueError):
                    try:
                        when = datetime.fromisoformat(f[k].text.strip().replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        when = None
                break
        src = source
        if "source" in f and f["source"].text:            # Google News names the real publisher
            src = clean(f["source"].text, 60)
            title = re.sub(r"\s+-\s+" + re.escape(src) + r"$", "", title)
        if title and link and when:
            out.append({"title": title, "link": link, "t": int(when), "source": src, "via": source})
    return out


def score(title):
    t = title.lower()
    tags, best = [], 0
    for name, w, rx in TAGS:
        if re.search(rx, t):
            tags.append(name)
            best = max(best, w)
    s = best + (1 if re.search(HOT, t) else 0)
    return tags, s


def norm_key(title):
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:70]


# ------------------------------------------------------------------ database
SCHEMA = """
CREATE TABLE IF NOT EXISTS hourly(id TEXT, utc TEXT, price REAL, PRIMARY KEY(id, utc));
CREATE TABLE IF NOT EXISTS daily(id TEXT, date TEXT, close REAL, PRIMARY KEY(id, date));
CREATE TABLE IF NOT EXISTS last(id TEXT PRIMARY KEY, label TEXT, grp TEXT, ticker TEXT, unit TEXT, data TEXT, fetched_utc TEXT);
CREATE TABLE IF NOT EXISTS news(key TEXT PRIMARY KEY, t INTEGER, title TEXT, link TEXT, source TEXT, via TEXT, tags TEXT, score INTEGER, first_seen_utc TEXT);
CREATE TABLE IF NOT EXISTS calendar(key TEXT PRIMARY KEY, t INTEGER, country TEXT, title TEXT, impact TEXT, forecast TEXT, previous TEXT, actual TEXT, updated_utc TEXT);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def catalogue():
    """[(id, label, group, ticker, unit)] for every instrument, sector and company tracked."""
    tid = lambda t: re.sub(r"[^a-z0-9]", "", t.split(":")[1].lower())          # noqa: E731
    return INSTRUMENTS + [(tid(t), n, "Sectors", t, "$") for t, n in SECTORS] + [(tid(t), n, "Companies", t, "$") for t, n in MEGACAPS]


def _f(x, nd=2):
    try:
        return None if x is None else round(float(x), nd)
    except (TypeError, ValueError):
        return None


def row_from(iid, label, grp, ticker, unit, d):
    """One instrument from the screener's columns. Yields move in basis points (the screener's change is a % of the
    yield, which is not how rates are read)."""
    px = d.get("close")
    if px is None:
        return None
    r = {"id": iid, "label": label, "ticker": ticker, "unit": unit, "price": float(f"{float(px):.6g}"),
         "d1": _f(d.get("change")), "w1": _f(d.get("Perf.W")), "m1": _f(d.get("Perf.1M")), "m3": _f(d.get("Perf.3M")),
         "ytd": _f(d.get("Perf.YTD")), "y1": _f(d.get("Perf.Y")), "hi52": _f(d.get("price_52_week_high"), 6),
         "lo52": _f(d.get("price_52_week_low"), 6), "delayed": "delayed" in str(d.get("update_mode") or "")}
    if unit == "%":
        r["d1_bp"] = _f(100 * float(d["change_abs"]), 1) if d.get("change_abs") is not None else None
    return r


def spark(con, iid):
    """Up to 60 daily closes from our own history; while it is short, the last 7 days of hourly prices."""
    d = [c for (c,) in con.execute("SELECT close FROM (SELECT date, close FROM daily WHERE id=? ORDER BY date DESC LIMIT 60) ORDER BY date", (iid,))]
    if len(d) >= 10:
        return [float(f"{x:.6g}") for x in d]
    cut = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H")
    h = [c for (c,) in con.execute("SELECT price FROM hourly WHERE id=? AND utc>=? ORDER BY utc", (iid, cut))]
    return [float(f"{x:.6g}") for x in h]


# ------------------------------------------------------------------ refresh
def refresh(force=False, log=print, fetch=fetch, out=OUT, db=DB):
    """Fetch everything, update the database and write world.json. Returns True when written."""
    try:
        if not force and time.time() - os.path.getmtime(out) < EVERY_S:
            return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(db), exist_ok=True)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    con = sqlite3.connect(db, timeout=30)
    con.executescript(SCHEMA)
    t0 = time.time()
    cat = catalogue()
    got = scan(sorted({t for x in cat for t in x[3].split("|")}), fetch)
    hour, day, ok, miss = now_iso()[:13], now_iso()[:10], 0, []
    for iid, label, grp, tickers, unit in cat:
        ticker = next((t for t in tickers.split("|") if (got.get(t) or {}).get("close") is not None), tickers.split("|")[0])
        r = row_from(iid, label, grp, ticker, unit, got.get(ticker) or {})
        if not r:
            miss.append(iid)
            continue
        ok += 1
        con.execute("INSERT OR REPLACE INTO last VALUES(?,?,?,?,?,?,?)", (iid, label, grp, ticker, unit, json.dumps(r), now_iso()))
        con.execute("INSERT OR REPLACE INTO hourly VALUES(?,?,?)", (iid, hour, r["price"]))
        con.execute("INSERT OR REPLACE INTO daily VALUES(?,?,?)", (iid, day, r["price"]))   # the day's latest price
    con.commit()
    news_ok = []
    for name, url in FEEDS:
        st, body = fetch(url)
        items = parse_feed(body, name) if st == 200 else []
        if items:
            news_ok.append(name)
        for n in items:
            tags, sc = score(n["title"])
            con.execute("INSERT OR IGNORE INTO news VALUES(?,?,?,?,?,?,?,?,?)",
                        (norm_key(n["title"]), n["t"], n["title"], n["link"], n["source"], n["via"], json.dumps(tags), sc, now_iso()))
    con.commit()
    cal_ok = False
    st, body = fetch(CALENDAR)
    if st == 200:
        try:
            for e in json.loads(body):
                t = int(datetime.fromisoformat(e["date"]).timestamp())
                key = f"{e.get('country')}|{e.get('title')}|{t}"
                con.execute("INSERT OR REPLACE INTO calendar VALUES(?,?,?,?,?,?,?,?,?)",
                            (key, t, clean(e.get("country"), 8), clean(e.get("title"), 120), clean(e.get("impact"), 12),
                             clean(e.get("forecast"), 20), clean(e.get("previous"), 20), clean(e.get("actual"), 20), now_iso()))
            cal_ok = True
        except (ValueError, KeyError, TypeError):
            pass
    con.commit()
    fng = None
    st, body = fetch(FNG)
    if st == 200:
        try:
            d = json.loads(body)["data"]
            fng = {"value": int(d[0]["value"]), "label": clean(d[0]["value_classification"], 30),
                   "week_ago": int(d[min(7, len(d) - 1)]["value"])}
        except (ValueError, KeyError, IndexError, TypeError):
            fng = None
    data = build(con, cat, fng)
    data["sources"] = {"prices_ok": ok, "prices_total": len(cat), "prices_missing": miss, "news_feeds_ok": news_ok,
                       "news_feeds_total": len(FEEDS), "calendar": cal_ok, "fear_greed": fng is not None, "seconds": round(time.time() - t0)}
    con.close()
    if ok == 0 and not news_ok:
        log("world markets: no source answered; world.json left as it was")
        return False
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, out)
    log(f"world markets: {ok}/{len(cat)} prices, {len(news_ok)}/{len(FEEDS)} news feeds, calendar {'ok' if cal_ok else 'missing'}, "
        f"{data['news_count']} headlines (48 h), {round(time.time() - t0)} s" + (f"; missing {', '.join(miss)}" if miss else ""))
    return True


def build(con, cat, fng):
    groups, by_id = {}, {}
    for iid, label, grp, ticker, unit in cat:
        r = con.execute("SELECT data, fetched_utc FROM last WHERE id=?", (iid,)).fetchone()
        if not r:
            continue
        row = dict(json.loads(r[0]), as_of=r[1], spark=spark(con, iid))
        groups.setdefault(grp, []).append(row)
        by_id[iid] = row
    now = int(time.time())
    seen, news = set(), []
    for t, lk, s_, tt, tg, sc in con.execute("SELECT title, link, source, t, tags, score FROM news WHERE t>=? AND t<=? ORDER BY t DESC LIMIT 600",
                                             (now - 48 * 3600, now + 3600)):
        k = norm_key(t)[:45]
        if k in seen:
            continue
        seen.add(k)
        news.append({"title": t, "link": lk, "source": s_, "t": tt, "tags": json.loads(tg), "score": sc})
    news = news[:300]
    cal = [{"t": t, "country": c, "title": ti, "impact": im, "forecast": f, "previous": p, "actual": a}
           for t, c, ti, im, f, p, a in con.execute(
               "SELECT t, country, title, impact, forecast, previous, actual FROM calendar WHERE t>=? AND t<=? AND impact IN ('High','Medium') ORDER BY t",
               (now - 2 * 86400, now + 8 * 86400))]
    return {"generated_utc": now_iso(), "groups": groups, "summary": summary(by_id, groups.get("Sectors", [])),
            "news": news, "news_count": len(news), "calendar": cal, "fear_greed": fng,
            "credits": "Prices: TradingView screener (some delayed up to 15 min). News: publishers' public RSS feeds and Google News. "
                       "Calendar: Forex Factory. Crypto sentiment: alternative.me."}


def summary(by, sectors):
    """A plain reading of the latest session from the numbers only (no opinion): each block's direction, the leading
    and lagging sectors, and a risk-on / risk-off tally."""
    g = lambda k, f="d1": (by.get(k) or {}).get(f)     # noqa: E731

    def mv(k, name):
        v = g(k, "d1_bp") if (by.get(k) or {}).get("unit") == "%" else g(k)
        if v is None:
            return None
        return f"{name} {'+' if v > 0 else ''}{v:.0f} bp" if (by.get(k) or {}).get("unit") == "%" else f"{name} {'+' if v > 0 else ''}{v:.2f}%"
    lines = []
    for head, keys in (("US stocks", (("spx", "S&P 500"), ("ndx", "Nasdaq 100"), ("dji", "Dow"))),
                       ("Europe", (("sx5e", "Euro Stoxx 50"), ("dax", "DAX"), ("ftse", "FTSE 100"))),
                       ("Asia", (("n225", "Nikkei"), ("hsi", "Hang Seng"), ("shc", "Shanghai"), ("nifty", "Nifty"), ("kse", "KSE-100"))),
                       ("Rates and dollar", (("us10y", "US 10-year"), ("us2y", "US 2-year"), ("dxy", "Dollar index"))),
                       ("Commodities", (("gold", "Gold"), ("brent", "Brent"), ("copper", "Copper"))),
                       ("Crypto", (("btc", "Bitcoin"), ("eth", "Ethereum")))):
        parts = [x for x in (mv(k, n) for k, n in keys) if x]
        if parts:
            lines.append(f"{head}: " + ", ".join(parts))
    sc = 0.0
    for k, w in (("spx", 1), ("ndx", 1), ("sx5e", 0.5), ("hsi", 0.5), ("btc", 0.5), ("copper", 0.5)):
        v = g(k)
        if v is not None:
            sc += w * (1 if v > 0.2 else -1 if v < -0.2 else 0)
    for k, w, th in (("vix", 1, 3), ("gold", 0.5, 0.3), ("dxy", 0.5, 0.3)):
        v = g(k)
        if v is not None:
            sc -= w * (1 if v > th else -1 if v < -th else 0)
    tone = "risk-on" if sc >= 2 else "risk-off" if sc <= -2 else "mixed"
    sec = sorted([x for x in sectors if x.get("d1") is not None], key=lambda x: -x["d1"])
    sec_m = sorted([x for x in sectors if x.get("m1") is not None], key=lambda x: -x["m1"])
    return {"lines": lines, "tone": tone, "score": sc, "vix": g("vix", "price"),
            "sector_leaders": [f"{x['label']} {x['d1']:+.1f}%" for x in sec[:3]],
            "sector_laggards": [f"{x['label']} {x['d1']:+.1f}%" for x in sec[-3:][::-1]],
            "sector_month_leaders": [f"{x['label']} {x['m1']:+.1f}%" for x in sec_m[:3]],
            "sector_month_laggards": [f"{x['label']} {x['m1']:+.1f}%" for x in sec_m[-3:][::-1]]}


def probe():
    print(f"== world sources probe {now_iso()} ==")
    cat = catalogue()
    got = scan(sorted({t for x in cat for t in x[3].split("|")}))
    miss = [x[3] for x in cat if not any((got.get(t) or {}).get("close") is not None for t in x[3].split("|"))]
    print("  alternatives used: " + ", ".join(next(t for t in x[3].split("|") if (got.get(t) or {}).get("close") is not None)
                                             for x in cat if "|" in x[3] and x[3] not in miss))
    print(f"  prices (TradingView screener): {len(cat) - len(miss)}/{len(cat)}" + (f"; missing {', '.join(miss)}" if miss else ""))
    for name, url in FEEDS:
        st, body = fetch(url)
        n = parse_feed(body, name) if st == 200 else []
        print(f"  feed {name:34s} http {st} items {len(n)}")
    for name, url in (("calendar", CALENDAR), ("fear&greed", FNG)):
        st, body = fetch(url)
        print(f"  {name:12s} http {st} bytes {len(body)}")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    else:
        refresh(force=True)
