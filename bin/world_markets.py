"""
World markets for the website's "World" tab (Python 3.9 stdlib only). Refreshed once an hour by the engine.

What it collects (public data only; no key, no account):
  * prices: stock indexes by region, commodities, currencies, US Treasury yields, US sectors, the largest US
    companies. Source: Yahoo Finance's public chart endpoint (daily closes for a year plus the latest price);
    Stooq's CSV as a fallback for any symbol Yahoo does not answer. Pakistan's KSE-100 also from the PSX data portal.
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
import csv
import io
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

# (id, label, group, yahoo symbol, stooq symbol, unit)   unit: "" index points, "$" dollars, "%" yield, "fx"
INSTRUMENTS = [
    # United States
    ("spx", "S&P 500", "Americas", "^GSPC", "^spx", ""),
    ("ndx", "Nasdaq 100", "Americas", "^NDX", "^ndx", ""),
    ("dji", "Dow Jones", "Americas", "^DJI", "^dji", ""),
    ("rut", "Russell 2000", "Americas", "^RUT", "", ""),
    ("vix", "VIX (fear gauge)", "Americas", "^VIX", "", ""),
    ("tsx", "Canada TSX", "Americas", "^GSPTSE", "", ""),
    ("bvsp", "Brazil Bovespa", "Americas", "^BVSP", "", ""),
    # Europe
    ("sx5e", "Euro Stoxx 50", "Europe", "^STOXX50E", "", ""),
    ("dax", "Germany DAX", "Europe", "^GDAXI", "^dax", ""),
    ("ftse", "UK FTSE 100", "Europe", "^FTSE", "^ukx", ""),
    ("cac", "France CAC 40", "Europe", "^FCHI", "^cac", ""),
    # Asia-Pacific, Middle East
    ("n225", "Japan Nikkei 225", "Asia & Middle East", "^N225", "^nkx", ""),
    ("hsi", "Hong Kong Hang Seng", "Asia & Middle East", "^HSI", "^hsi", ""),
    ("shc", "China Shanghai", "Asia & Middle East", "000001.SS", "^shc", ""),
    ("kospi", "Korea KOSPI", "Asia & Middle East", "^KS11", "", ""),
    ("nifty", "India Nifty 50", "Asia & Middle East", "^NSEI", "", ""),
    ("asx", "Australia ASX 200", "Asia & Middle East", "^AXJO", "", ""),
    ("tasi", "Saudi Tadawul", "Asia & Middle East", "^TASI.SR", "", ""),
    ("kse", "Pakistan KSE-100", "Asia & Middle East", "^KSE", "", ""),
    # Commodities
    ("gold", "Gold", "Commodities", "GC=F", "gc.f", "$"),
    ("silver", "Silver", "Commodities", "SI=F", "si.f", "$"),
    ("brent", "Brent crude", "Commodities", "BZ=F", "cb.f", "$"),
    ("wti", "WTI crude", "Commodities", "CL=F", "cl.f", "$"),
    ("natgas", "Natural gas (US)", "Commodities", "NG=F", "ng.f", "$"),
    ("copper", "Copper", "Commodities", "HG=F", "hg.f", "$"),
    ("platinum", "Platinum", "Commodities", "PL=F", "", "$"),
    ("wheat", "Wheat", "Commodities", "ZW=F", "", "$"),
    ("corn", "Corn", "Commodities", "ZC=F", "", "$"),
    # Currencies
    ("dxy", "US Dollar index", "Currencies", "DX-Y.NYB", "", ""),
    ("eurusd", "EUR / USD", "Currencies", "EURUSD=X", "eurusd", "fx"),
    ("usdjpy", "USD / JPY", "Currencies", "JPY=X", "usdjpy", "fx"),
    ("gbpusd", "GBP / USD", "Currencies", "GBPUSD=X", "gbpusd", "fx"),
    ("usdcny", "USD / CNY", "Currencies", "CNY=X", "usdcny", "fx"),
    ("usdchf", "USD / CHF", "Currencies", "CHF=X", "usdchf", "fx"),
    ("usdpkr", "USD / PKR", "Currencies", "PKR=X", "usdpkr", "fx"),
    ("usdinr", "USD / INR", "Currencies", "INR=X", "usdinr", "fx"),
    # US Treasury yields (Yahoo quotes them in percent)
    ("us3m", "US 3-month yield", "Rates", "^IRX", "", "%"),
    ("us5y", "US 5-year yield", "Rates", "^FVX", "", "%"),
    ("us10y", "US 10-year yield", "Rates", "^TNX", "", "%"),
    ("us30y", "US 30-year yield", "Rates", "^TYX", "", "%"),
    # Crypto (the dashboard's own Binance prices are the main source; these are the fallback)
    ("btc", "Bitcoin", "Crypto", "BTC-USD", "", "$"),
    ("eth", "Ethereum", "Crypto", "ETH-USD", "", "$"),
]
SECTORS = [("XLK", "Technology"), ("SMH", "Semiconductors"), ("XLC", "Communication"), ("XLY", "Consumer discretionary"),
           ("XLF", "Financials"), ("XLV", "Health care"), ("XLI", "Industrials"), ("XLE", "Energy"), ("XLB", "Materials"),
           ("XLP", "Consumer staples"), ("XLU", "Utilities"), ("XLRE", "Real estate")]
MEGACAPS = [("NVDA", "Nvidia"), ("MSFT", "Microsoft"), ("AAPL", "Apple"), ("GOOGL", "Alphabet"), ("AMZN", "Amazon"),
            ("META", "Meta"), ("AVGO", "Broadcom"), ("TSLA", "Tesla"), ("BRK-B", "Berkshire Hathaway"), ("JPM", "JPMorgan")]

FEEDS = [  # (name, url)
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("ECB", "https://www.ecb.europa.eu/rss/press.html"),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC Markets", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("OilPrice", "https://oilprice.com/rss/main"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Dawn Business", "https://www.dawn.com/feeds/business"),
    ("Business Recorder", "https://www.brecorder.com/feeds/latest-news"),
    ("Google News: markets", "https://news.google.com/rss/search?q=stock+market+OR+stocks+OR+wall+street+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: central banks", "https://news.google.com/rss/search?q=%22Federal+Reserve%22+OR+ECB+OR+%22Bank+of+Japan%22+OR+%22interest+rates%22+when:2d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: oil & commodities", "https://news.google.com/rss/search?q=oil+prices+OR+OPEC+OR+gold+prices+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: trade & geopolitics", "https://news.google.com/rss/search?q=tariffs+OR+sanctions+OR+%22trade+war%22+when:2d&hl=en-US&gl=US&ceid=US:en"),
]
CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FNG = "https://api.alternative.me/fng/?limit=8"
PSX = "https://dps.psx.com.pk/timeseries/int/KSE100"

# headline tags and how much each says about market impact (the highest tag wins, words add a little)
TAGS = [
    ("Central banks", 3, r"\b(fed|federal reserve|fomc|powell|ecb|lagarde|bank of japan|boj|bank of england|boe|pboc|rate (cut|hike)|interest rates?|monetary policy|central bank|state bank of pakistan|sbp)\b"),
    ("Inflation & data", 3, r"\b(inflation|cpi|ppi|pce|payrolls?|jobs report|unemployment|gdp|recession|retail sales|pmi|jobless claims|consumer confidence)\b"),
    ("Geopolitics & trade", 3, r"\b(tariffs?|trade war|sanctions?|war|ceasefire|missile|invasion|conflict|election|shutdown|debt ceiling|default)\b"),
    ("Energy", 2, r"\b(oil|crude|opec|brent|wti|natural gas|lng|refiner)"),
    ("Metals", 2, r"\b(gold|silver|copper|platinum|lithium)\b"),
    ("Earnings & companies", 2, r"\b(earnings|guidance|revenue|profit|quarterly results|ipo|merger|acquisition|buyback|layoffs?)\b"),
    ("Crypto", 1, r"\b(bitcoin|crypto|ethereum|stablecoin|etf flows?)\b"),
    ("Markets", 1, r"\b(stocks?|shares|s&p|nasdaq|dow|equities|bonds?|yields?|treasur|dollar|yen|euro|selloff|rally|record high)\b"),
]
HOT = r"\b(surge|plunge|crash|soar|slump|tumble|record|emergency|unexpected|shock|biggest|warns?|halt|collapse)\w*"


# ------------------------------------------------------------------ fetching
def fetch(url, timeout=15):
    """GET a public URL with curl (no credentials ever). Returns (status, body text)."""
    cfg = f'url = "{url}"\nheader = "User-Agent: {UA}"\nheader = "Accept: */*"\n'
    p = subprocess.run(["curl", "-sS", "-L", "--max-redirs", "3", "--compressed", "--max-time", str(timeout),
                        "--max-filesize", str(MAX_BODY), "-w", "\n%{http_code}", "--config", "-"],
                       input=cfg, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        return 0, ""
    body, _, code = p.stdout.rpartition("\n")
    return int(code or 0), body


def yahoo(sym, fetch=fetch):
    """Daily closes for a year and the latest quote. Returns {"price", "prev", "time", "closes": [(date, close)], "tz",
    "open"} or None."""
    import urllib.parse
    st, body = fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1y&interval=1d&includePrePost=false")
    if st != 200:
        return None
    try:
        r = json.loads(body)["chart"]["result"][0]
        m = r["meta"]
        ts, cl = r.get("timestamp") or [], r["indicators"]["quote"][0].get("close") or []
        off = int(m.get("gmtoffset") or 0)
        closes = [(datetime.fromtimestamp(t + off, timezone.utc).strftime("%Y-%m-%d"), float(c)) for t, c in zip(ts, cl) if c is not None]
        price = m.get("regularMarketPrice")
        if price is None and closes:
            price = closes[-1][1]
        rt = int(m.get("regularMarketTime") or (ts[-1] if ts else 0))
        per = ((m.get("currentTradingPeriod") or {}).get("regular") or {})
        now = time.time()
        is_open = bool(per) and int(per.get("start", 0)) <= now < int(per.get("end", 0))
        return {"price": float(price), "time": rt, "closes": closes, "tz": m.get("exchangeTimezoneName"), "open": is_open,
                "currency": m.get("currency")}
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def stooq(sym, fetch=fetch):
    st, body = fetch(f"https://stooq.com/q/d/l/?s={sym}&i=d")
    if st != 200 or not body.startswith("Date"):
        return None
    try:
        rows = [(r["Date"], float(r["Close"])) for r in csv.DictReader(io.StringIO(body)) if r.get("Close")]
    except (ValueError, KeyError):
        return None
    if not rows:
        return None
    rows = rows[-260:]
    return {"price": rows[-1][1], "time": int(datetime.strptime(rows[-1][0], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()),
            "closes": rows, "tz": None, "open": None, "currency": None}


def psx_kse(fetch=fetch):
    """Pakistan KSE-100 intraday from the PSX data portal: [[unix, price, volume], ...] (newest first)."""
    st, body = fetch(PSX)
    if st != 200:
        return None
    try:
        d = json.loads(body).get("data") or []
        pts = sorted((int(x[0]), float(x[1])) for x in d if x and x[1])
        return pts[-1] if pts else None
    except (ValueError, TypeError, IndexError, AttributeError):
        return None


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
CREATE TABLE IF NOT EXISTS daily(id TEXT, date TEXT, close REAL, PRIMARY KEY(id, date));
CREATE TABLE IF NOT EXISTS last(id TEXT PRIMARY KEY, price REAL, time INTEGER, source TEXT, is_open INTEGER, fetched_utc TEXT);
CREATE TABLE IF NOT EXISTS news(key TEXT PRIMARY KEY, t INTEGER, title TEXT, link TEXT, source TEXT, via TEXT, tags TEXT, score INTEGER, first_seen_utc TEXT);
CREATE TABLE IF NOT EXISTS calendar(key TEXT PRIMARY KEY, t INTEGER, country TEXT, title TEXT, impact TEXT, forecast TEXT, previous TEXT, actual TEXT, updated_utc TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def store_quote(con, iid, q, source):
    con.executemany("INSERT OR REPLACE INTO daily VALUES(?,?,?)", [(iid, d, c) for d, c in q["closes"]])
    con.execute("INSERT OR REPLACE INTO last VALUES(?,?,?,?,?,?)", (iid, q["price"], q["time"], source,
                                                                     None if q.get("open") is None else int(q["open"]), now_iso()))


def changes(con, iid, price, unit):
    """1D / 1W / 1M / YTD / 1Y change, and the last 60 closes for a sparkline. For yields the change is in basis
    points (percentage points x 100), otherwise in percent."""
    rows = con.execute("SELECT date, close FROM daily WHERE id=? ORDER BY date", (iid,)).fetchall()
    if not rows or price is None:
        return {}
    today = datetime.now(timezone.utc).date()
    last_d = rows[-1][0]

    def before(days=None, date=None):
        lim = date or (today - timedelta(days=days)).isoformat()
        r = [c for d, c in rows if d <= lim]
        return r[-1] if r else None
    # the previous close: the last daily close before the latest trading day
    prev = rows[-2][1] if len(rows) > 1 else None
    if abs(rows[-1][1] - price) > 1e-12 and last_d < today.isoformat():
        prev = rows[-1][1]                       # the latest price is newer than the last daily close
    ref = {"d1": prev, "w1": before(7), "m1": before(30), "ytd": before(date=f"{today.year - 1}-12-31"), "y1": before(365)}
    out = {}
    for k, r in ref.items():
        if r:
            out[k] = round((price - r) * 100, 1) if unit == "%" else round(100 * (price / r - 1), 2)
    spark = [c for _, c in rows[-60:]]
    out["spark"] = [float(f"{x:.6g}") for x in spark]
    out["hi52"], out["lo52"] = max(c for _, c in rows[-260:]), min(c for _, c in rows[-260:])
    return out


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
    t0, ok, miss = time.time(), 0, []
    items = [(i, l, g, y, s, u) for i, l, g, y, s, u in INSTRUMENTS] + \
            [(t.lower().replace("-", ""), n, "Sectors", t, t.lower().replace("-", ".") + ".us", "$") for t, n in SECTORS] + \
            [(t.lower().replace("-", ""), n, "Megacaps", t, t.lower().replace("-", ".") + ".us", "$") for t, n in MEGACAPS]
    for iid, label, grp, ysym, ssym, unit in items:
        q, src = yahoo(ysym, fetch), "Yahoo Finance"
        if not q and ssym:
            q, src = stooq(ssym, fetch), "Stooq"
        if iid == "kse":
            p = psx_kse(fetch)
            if p:
                q = q or {"closes": [], "open": None}
                q.update(price=p[1], time=p[0])
                src = "PSX" if not q["closes"] else src + " + PSX"
        if q and q.get("price") is not None:
            store_quote(con, iid, q, src)
            ok += 1
        else:
            miss.append(iid)
        time.sleep(0.25)                          # gentle: one request at a time
    con.commit()
    # news
    news_ok = []
    for name, url in FEEDS:
        st, body = fetch(url)
        items_ = parse_feed(body, name) if st == 200 else []
        if items_:
            news_ok.append(name)
        for n in items_:
            tags, sc = score(n["title"])
            con.execute("INSERT OR IGNORE INTO news VALUES(?,?,?,?,?,?,?,?,?)",
                        (norm_key(n["title"]), n["t"], n["title"], n["link"], n["source"], n["via"], json.dumps(tags), sc, now_iso()))
    con.commit()
    # calendar
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
    data = build(con, items, fng)
    data["sources"] = {"prices_ok": ok, "prices_missing": miss, "news_feeds_ok": news_ok, "calendar": cal_ok,
                       "fear_greed": fng is not None, "seconds": round(time.time() - t0)}
    con.close()
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, out)
    log(f"world markets: {ok}/{len(items)} prices, {len(news_ok)}/{len(FEEDS)} news feeds, calendar {'ok' if cal_ok else 'missing'}, "
        f"{data['news_count']} headlines (48 h), {round(time.time() - t0)} s" + (f"; missing {', '.join(miss)}" if miss else ""))
    return True


def build(con, items, fng):
    groups, by_id = {}, {}
    for iid, label, grp, ysym, ssym, unit in items:
        r = con.execute("SELECT price, time, source, is_open FROM last WHERE id=?", (iid,)).fetchone()
        if not r:
            continue
        ch = changes(con, iid, r[0], unit)
        row = dict({"id": iid, "label": label, "symbol": ysym, "unit": unit, "price": float(f"{r[0]:.6g}"), "time": r[1],
                    "source": r[2], "open": None if r[3] is None else bool(r[3])}, **ch)
        groups.setdefault(grp, []).append(row)
        by_id[iid] = row
    cut = int(time.time()) - 48 * 3600
    news = [{"title": t, "link": lk, "source": s, "t": tt, "tags": json.loads(tg), "score": sc}
            for t, lk, s, tt, tg, sc in con.execute(
                "SELECT title, link, source, t, tags, score FROM news WHERE t>=? AND t<=? ORDER BY t DESC LIMIT 400", (cut, int(time.time()) + 3600))]
    week0 = int(time.time()) - 2 * 86400
    cal = [{"t": t, "country": c, "title": ti, "impact": im, "forecast": f, "previous": p, "actual": a}
           for t, c, ti, im, f, p, a in con.execute(
               "SELECT t, country, title, impact, forecast, previous, actual FROM calendar WHERE t>=? AND t<=? AND impact IN ('High','Medium') ORDER BY t",
               (week0, int(time.time()) + 8 * 86400))]
    return {"generated_utc": now_iso(), "groups": groups, "summary": summary(by_id, groups.get("Sectors", [])),
            "news": news, "news_count": len(news), "calendar": cal, "fear_greed": fng}


def summary(by, sectors):
    """A plain reading of the last session, from the numbers only (no opinion): direction of each block, the
    leading and lagging sectors, and a risk-on / risk-off score."""
    g = lambda k, f="d1": (by.get(k) or {}).get(f)     # noqa: E731
    lines, score_ = [], 0
    def mv(k, name, bp=False):
        v = g(k)
        if v is None:
            return None
        return f"{name} {('+' if v > 0 else '') + (f'{v:.0f} bp' if bp else f'{v:.2f}%')}"
    us = [x for x in (mv("spx", "S&P 500"), mv("ndx", "Nasdaq 100"), mv("dji", "Dow")) if x]
    if us:
        lines.append("US stocks: " + ", ".join(us))
    eu = [x for x in (mv("sx5e", "Euro Stoxx 50"), mv("dax", "DAX"), mv("ftse", "FTSE 100")) if x]
    if eu:
        lines.append("Europe: " + ", ".join(eu))
    asia = [x for x in (mv("n225", "Nikkei"), mv("hsi", "Hang Seng"), mv("shc", "Shanghai"), mv("kse", "KSE-100")) if x]
    if asia:
        lines.append("Asia: " + ", ".join(asia))
    m = [x for x in (mv("us10y", "US 10-year yield", True), mv("dxy", "Dollar index"), mv("gold", "Gold"), mv("brent", "Brent"), mv("btc", "Bitcoin")) if x]
    if m:
        lines.append("Rates, dollar, commodities: " + ", ".join(m))
    for k, w in (("spx", 1), ("ndx", 1), ("sx5e", 0.5), ("hsi", 0.5), ("btc", 0.5), ("copper", 0.5)):
        v = g(k)
        if v is not None:
            score_ += w * (1 if v > 0.2 else -1 if v < -0.2 else 0)
    for k, w in (("vix", 1), ("gold", 0.5), ("dxy", 0.5)):
        v = g(k)
        if v is not None:
            score_ -= w * (1 if v > (3 if k == "vix" else 0.3) else -1 if v < (-3 if k == "vix" else -0.3) else 0)
    tone = "risk-on" if score_ >= 2 else "risk-off" if score_ <= -2 else "mixed"
    sec = sorted([s for s in sectors if s.get("d1") is not None], key=lambda s: -s["d1"])
    lead = [f"{s['label']} {s['d1']:+.1f}%" for s in sec[:3]]
    lag = [f"{s['label']} {s['d1']:+.1f}%" for s in sec[-3:][::-1]]
    sec_m = sorted([s for s in sectors if s.get("m1") is not None], key=lambda s: -s["m1"])
    return {"lines": lines, "tone": tone, "score": score_, "sector_leaders": lead, "sector_laggards": lag,
            "sector_month_leaders": [f"{s['label']} {s['m1']:+.1f}%" for s in sec_m[:3]], "vix": g("vix", "price") if by.get("vix") else None}


PROBE_URLS = [
    "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d",
    "https://query2.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d",
    "https://stooq.com/q/l/?s=%5Espx&f=sd2t2ohlcv&h&e=csv",
    "https://stooq.com/q/d/l/?s=%5Espx&i=d",
    "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols=.SPX%7C@GC.1%7CUS10Y%7CEUR%3D&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json",
    "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500",
    "https://www.google.com/finance/quote/.INX:INDEXSP",
    "https://api.nasdaq.com/api/quote/SPX/info?assetclass=index",
    "https://www.marketwatch.com/investing/index/spx",
    "https://scanner.tradingview.com/global/scan",
]


def probe():
    print(f"== world sources probe {now_iso()} ==")
    for u in PROBE_URLS:
        st, body = fetch(u)
        print(f"  raw http {st} {len(body):7d} B {u[:70]} | {re.sub(chr(10), ' ', body[:110])}")
    for iid, label, grp, ysym, ssym, unit in INSTRUMENTS[:6] + [x for x in INSTRUMENTS if x[0] in ("kse", "tasi", "gold", "usdpkr", "us10y", "dxy")]:
        q = yahoo(ysym)
        s = stooq(ssym) if ssym else None
        print(f"  {iid:8s} yahoo {'ok ' + str(q['price']) + ' (' + str(len(q['closes'])) + ' closes)' if q else 'FAIL':32s} stooq {('ok ' + str(s['price'])) if s else ('-' if not ssym else 'FAIL')}")
    print(f"  kse      psx {psx_kse()}")
    for name, url in FEEDS:
        st, body = fetch(url)
        n = parse_feed(body, name) if st == 200 else []
        print(f"  feed {name:34s} http {st} items {len(n)}" + (f" | {n[0]['source']}: {n[0]['title'][:70]}" if n else ""))
    for name, url in (("calendar", CALENDAR), ("fear&greed", FNG)):
        st, body = fetch(url)
        print(f"  {name:12s} http {st} bytes {len(body)}")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    else:
        refresh(force=True)
