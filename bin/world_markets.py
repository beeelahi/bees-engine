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

from venues import CURL_PROTO, curl_config_url

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(ROOT, "data", "world.db")
OUT = os.path.join(ROOT, "web_export", "world.json")
EVERY_S = 55 * 60
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MAX_BODY = 3_000_000

# (id, label, group, TradingView ticker(s), unit). "A|B|C": the first ticker that answers is used.   unit: "" index points, "$" price, "c$" quoted in US cents (shown in dollars), "%" yield, "fx" exchange rate
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
    ("wheat", "Wheat", "Commodities", "CBOT:ZW1!", "c$"),
    ("corn", "Corn", "Commodities", "CBOT:ZC1!", "c$"),
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
CALENDAR_NEXT = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"      # 404 since Oct 2026: kept as a fallback only
CAL_TV = "https://economic-calendar.tradingview.com/events"                      # this week and next (any range)
CAL_COUNTRIES = "US,EU,GB,JP,CN,DE,FR,IT,CA,AU,NZ,CH,IN"
US_SCAN = "https://scanner.tradingview.com/america/scan"
# the largest US-listed companies in each sector tile (TradingView's own classification, refreshed with the prices)
SECTOR_COS = {
    "xlk": {"sector": ["Electronic Technology", "Technology Services"], "not_industry": ["Internet Software/Services"]},
    "smh": {"industry": ["Semiconductors"]},
    "xlc": {"sector": ["Communications"], "industry_or": ["Internet Software/Services", "Cable/Satellite TV", "Movies/Entertainment",
                                                          "Broadcasting", "Advertising/Marketing Services", "Publishing: Newspapers"]},
    "xly": {"sector": ["Retail Trade", "Consumer Durables", "Consumer Services"], "not_industry": ["Food Retail", "Drugstore Chains", "Movies/Entertainment",
                                                                                                  "Broadcasting", "Cable/Satellite TV"]},
    "xlf": {"sector": ["Finance"], "not_industry": ["Real Estate Investment Trusts", "Real Estate Development"]},
    "xlv": {"sector": ["Health Technology", "Health Services"]},
    "xli": {"sector": ["Producer Manufacturing", "Industrial Services", "Transportation", "Commercial Services"],
            "not_industry": ["Oil & Gas Pipelines", "Contract Drilling", "Oilfield Services/Equipment"]},
    "xle": {"sector": ["Energy Minerals"], "industry_or": ["Oil & Gas Pipelines", "Oilfield Services/Equipment", "Contract Drilling"]},
    "xlb": {"sector": ["Non-Energy Minerals", "Process Industries"]},
    "xlp": {"sector": ["Consumer Non-Durables"], "industry_or": ["Food Retail", "Drugstore Chains"]},
    "xlu": {"sector": ["Utilities"]},
    "xlre": {"industry": ["Real Estate Investment Trusts", "Real Estate Development"]},
}
# corrections so the lists match the usual (GICS / sector ETF) grouping where TradingView's classification differs
SECTOR_FIX = {
    "xlk": {"add": ["NASDAQ:AMAT", "NASDAQ:LRCX", "NASDAQ:KLAC"]},
    "smh": {"add": ["NASDAQ:AMAT", "NASDAQ:LRCX", "NASDAQ:KLAC", "NASDAQ:ASML"]},
    "xli": {"drop": ["AMAT", "LRCX", "KLAC", "SHOP", "ASML"]},
    "xly": {"drop": ["WMT", "COST"]},
    "xlp": {"add": ["NASDAQ:WMT", "NYSE:WMT", "NASDAQ:COST"]},     # Walmart moved its listing to Nasdaq in Dec 2025
}
CO_COLS = ["name", "description", "close", "change", "Perf.W", "Perf.1M", "Perf.YTD", "market_cap_basic", "industry", "sector", "exchange"]
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
# outlets whose headlines lead the "most market-moving" view (others still appear in every topic and search)
MAJOR = r"^(reuters|bloomberg|wsj|the wall street journal|financial times|ft|cnbc|associated press|ap news|marketwatch|barron'?s|" \
        r"federal reserve|ecb|bbc|the economist|nikkei|axios|yahoo finance|cnn|the new york times|the guardian|politico|fortune|" \
        r"business insider|investing\.com|forbes|dawn|dawn business|business recorder|the express tribune|oilprice|coindesk|the block|" \
        r"south china morning post|scmp|bnn bloomberg|al jazeera|kitco|s&p global|morningstar)\b"
HOT = r"\b(surge|plunge|crash|soar|slump|tumble|record|emergency|unexpected|shock|biggest|warns?|halt|collapse)\w*"


# ------------------------------------------------------------------ fetching
def fetch(url, timeout=15, data=None, jar=None, tv=False):
    """GET (or POST `data` as JSON) a public URL with curl (no credentials ever). Returns (status, body text).
    tv=True sends TradingView's origin on a GET too (its public calendar asks for it)."""
    try:
        cfg = curl_config_url(url) + f'header = "User-Agent: {UA}"\nheader = "Accept: */*"\n'
    except ValueError:
        return 0, ""
    if tv and data is None:
        cfg += 'header = "Origin: https://www.tradingview.com"\nheader = "Referer: https://www.tradingview.com/"\n'
    if data is not None:
        cfg += 'header = "Content-Type: application/json"\nheader = "Origin: https://www.tradingview.com"\n' \
               'header = "Referer: https://www.tradingview.com/"\n'
    args = ["curl", "-sS", "-L", "--max-redirs", "3", *CURL_PROTO, "--compressed", "--max-time", str(timeout),
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


JUNK = re.compile(r"(?i)\bstock price,? news,? quote\b|\bquote and history\b|\bprice chart\b|\blive (price|chart)s?\b|^\s*\S+ \(\S+=\S\)")   # quote pages, not news


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
        if title and link and when and not JUNK.search(title):
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
CREATE TABLE IF NOT EXISTS sector_cos(id TEXT PRIMARY KEY, data TEXT, fetched_utc TEXT);
CREATE TABLE IF NOT EXISTS last(id TEXT PRIMARY KEY, label TEXT, grp TEXT, ticker TEXT, unit TEXT, data TEXT, fetched_utc TEXT);
CREATE TABLE IF NOT EXISTS news(key TEXT PRIMARY KEY, t INTEGER, title TEXT, link TEXT, source TEXT, via TEXT, tags TEXT, score INTEGER, first_seen_utc TEXT);
CREATE TABLE IF NOT EXISTS calendar(key TEXT PRIMARY KEY, t INTEGER, country TEXT, title TEXT, impact TEXT, forecast TEXT, previous TEXT, actual TEXT, updated_utc TEXT);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sector_query(spec, n=12):
    """TradingView screener request for one sector tile: primary listings on NYSE / Nasdaq / NYSE American, by market cap."""
    base = [{"left": "type", "operation": "equal", "right": "stock"}, {"left": "is_primary", "operation": "equal", "right": True},
            {"left": "exchange", "operation": "in_range", "right": ["NYSE", "NASDAQ", "AMEX"]}]
    if spec.get("industry"):
        base.append({"left": "industry", "operation": "in_range", "right": spec["industry"]})
    if spec.get("not_industry"):
        base.append({"left": "industry", "operation": "not_in_range", "right": spec["not_industry"]})
    q = {"columns": CO_COLS, "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"}, "range": [0, n], "markets": ["america"]}
    if spec.get("sector") and spec.get("industry_or"):
        q["filter"] = base
        q["filter2"] = {"operator": "and", "operands": [{"operation": {"operator": "or", "operands": [
            {"expression": {"left": "sector", "operation": "in_range", "right": spec["sector"]}},
            {"expression": {"left": "industry", "operation": "in_range", "right": spec["industry_or"]}}]}}]}
    else:
        if spec.get("sector"):
            base.append({"left": "sector", "operation": "in_range", "right": spec["sector"]})
        q["filter"] = base
    return q


def sector_companies(fetch=fetch, n=12):
    """{sector id: [company rows]} for every sector tile (sectors that fail are left out). One share class per company."""
    out = {}
    adds = sorted({t for f in SECTOR_FIX.values() for t in f.get("add", [])})
    extra = {}
    if adds:
        st, body = fetch(SCAN, data={"symbols": {"tickers": adds}, "columns": CO_COLS}, timeout=20)
        if st == 200:
            try:
                extra = {d["s"]: d for d in json.loads(body).get("data", [])}
            except (ValueError, KeyError, TypeError):
                extra = {}
    for sid, spec in SECTOR_COS.items():
        fix = SECTOR_FIX.get(sid, {})
        st, body = fetch(US_SCAN, data=sector_query(spec, n + 8), timeout=20)
        if st != 200:
            continue
        try:
            rows, seen = [], set()
            data = json.loads(body).get("data", []) + [extra[t] for t in fix.get("add", []) if t in extra]
            for d in data:
                if d["s"].split(":")[-1] in fix.get("drop", []):
                    continue
                v = dict(zip(CO_COLS, d["d"]))
                if v.get("close") is None:
                    continue
                key = re.sub(r"[^a-z]", "", re.sub(r"(?i)\b(class [abc]|inc|corp|corporation|holdings?|new)\b", "", str(v.get("description") or v.get("name"))).lower())[:18]
                if key in seen:
                    continue                                       # BRK.A / BRK.B, GOOG / GOOGL: one line per company
                try:                                               # one bad row never drops the sector
                    row = {"sym": d["s"].split(":")[-1], "ticker": d["s"], "name": clean(v.get("description") or v.get("name"), 60),
                           "price": float(f"{float(v['close']):.6g}"), "d1": _f(v.get("change")), "w1": _f(v.get("Perf.W")), "m1": _f(v.get("Perf.1M")),
                           "ytd": _f(v.get("Perf.YTD")), "mcap": float(v["market_cap_basic"]) if v.get("market_cap_basic") is not None else None,
                           "industry": clean(v.get("industry"), 50)}
                except (ValueError, TypeError):
                    continue
                seen.add(key)
                rows.append(row)
            rows.sort(key=lambda r: -(r["mcap"] or 0))
            if rows:
                out[sid] = rows[:n]
        except (ValueError, KeyError, TypeError):
            continue
        time.sleep(0.3)
    return out


def week_start(now=None):
    """00:00 UTC of the Sunday that starts this calendar week (Forex Factory's weeks run Sunday to Saturday)."""
    d = datetime.fromtimestamp(now or time.time(), timezone.utc)
    d = d.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=(d.weekday() + 1) % 7)
    return int(d.timestamp())


def tv_calendar(fetch=fetch, now=None):
    """High and medium impact events from TradingView's public calendar for this week and next: [(key, t, ccy, ...)]."""
    a = week_start(now)
    frm = datetime.fromtimestamp(a, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to = datetime.fromtimestamp(a + 14 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    st, body = fetch(f"{CAL_TV}?from={frm}&to={to}&countries={CAL_COUNTRIES}", timeout=20, tv=True)
    if st != 200:
        return None
    try:
        res = json.loads(body).get("result") or []
    except (ValueError, AttributeError):
        return None
    out = []
    for e in res:
        imp = {1: "High", 0: "Medium"}.get(e.get("importance"))
        if not imp or not e.get("date"):
            continue
        try:
            t = int(datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp())
        except ValueError:
            continue
        ccy = clean(e.get("currency") or e.get("country"), 8)
        num = lambda k: "" if e.get(k) is None else clean(f"{e[k]}{e.get('unit') or ''}" if isinstance(e[k], (int, float)) else e[k], 20)  # noqa: E731
        out.append((f"tv|{e.get('id') or (ccy + str(t) + str(e.get('title')))}", t, ccy, clean(e.get("title"), 120), imp, num("forecast"), num("previous"), num("actual")))
    return out


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
    k = 0.01 if unit == "c$" else 1.0          # CBOT grains are quoted in cents a bushel
    unit = "$" if unit == "c$" else unit
    px = float(px) * k
    if k != 1.0:
        d = dict(d, price_52_week_high=None if d.get("price_52_week_high") is None else float(d["price_52_week_high"]) * k,
                 price_52_week_low=None if d.get("price_52_week_low") is None else float(d["price_52_week_low"]) * k)
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
    for t in ("hourly", "daily"):                # grains stored in cents before 31 Oct 2026: now dollars (no grain trades at $50+ a bushel)
        con.execute(f"UPDATE {t} SET {'price' if t == 'hourly' else 'close'} = {'price' if t == 'hourly' else 'close'} / 100 "
                    f"WHERE id IN ('wheat', 'corn') AND {'price' if t == 'hourly' else 'close'} > 50")
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
    tvc = tv_calendar(fetch)
    if tvc:
        con.execute("DELETE FROM calendar WHERE key LIKE 'tv|%' AND t >= ?", (week_start(),))
        con.executemany("INSERT OR REPLACE INTO calendar VALUES(?,?,?,?,?,?,?,?,?)", [r + (now_iso(),) for r in tvc])
        cal_ok = True
    for url in (CALENDAR, CALENDAR_NEXT):
        st, body = fetch(url)
        if st != 200:
            continue
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
        time.sleep(1)
    con.commit()
    cos = sector_companies(fetch)
    for sid, rows in cos.items():
        con.execute("INSERT OR REPLACE INTO sector_cos VALUES(?,?,?)", (sid, json.dumps(rows), now_iso()))
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
        news.append({"title": t, "link": lk, "source": s_, "t": tt, "tags": json.loads(tg), "score": sc,
                     "major": bool(re.search(MAJOR, (s_ or "").strip().lower()))})
    news = news[:300]
    a = week_start(now)                          # this week and next, Sunday to Saturday
    tvn = con.execute("SELECT COUNT(*) FROM calendar WHERE key LIKE 'tv|%' AND t>=? AND t<?", (a, a + 14 * 86400)).fetchone()[0]
    src = "key LIKE 'tv|%'" if tvn else "key NOT LIKE 'tv|%'"        # one source at a time: the two name events differently
    cal = [{"t": t, "country": c, "title": ti, "impact": im, "forecast": f, "previous": p, "actual": a_}
           for t, c, ti, im, f, p, a_ in con.execute(
               f"SELECT t, country, title, impact, forecast, previous, actual FROM calendar WHERE t>=? AND t<? AND impact IN ('High','Medium') AND {src} ORDER BY t",
               (a, a + 14 * 86400))]
    cos = {sid: json.loads(d) for sid, d in con.execute("SELECT id, data FROM sector_cos")}
    return {"generated_utc": now_iso(), "groups": groups, "summary": summary(by_id, groups.get("Sectors", [])),
            "news": news, "news_count": len(news), "calendar": cal, "calendar_from": a, "calendar_source": "TradingView" if tvn else "Forex Factory",
            "sector_companies": cos, "fear_greed": fng,
            "credits": "Prices: TradingView screener (some delayed up to 15 min). News: publishers' public RSS feeds and Google News. "
                       "Calendar: TradingView (Forex Factory as a fallback). Crypto sentiment: alternative.me."}


def summary(by, sectors):
    """A plain reading of the latest session from the numbers only (no opinion): each block's direction, the leading
    and lagging sectors, and a risk-on / risk-off tally."""
    g = lambda k, f="d1": (by.get(k) or {}).get(f)     # noqa: E731

    def mv(k, name):
        v = g(k, "d1_bp") if (by.get(k) or {}).get("unit") == "%" else g(k)
        if v is None:
            return None
        return f"{name} {'+' if v > 0 else ''}{v:.1f} bp" if (by.get(k) or {}).get("unit") == "%" else f"{name} {'+' if v > 0 else ''}{v:.2f}%"
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
    for name, url in (("calendar", CALENDAR), ("next week", CALENDAR_NEXT), ("fear&greed", FNG)):
        st, body = fetch(url)
        print(f"  {name:12s} http {st} bytes {len(body)}")
    tvc = tv_calendar()
    print(f"  TradingView calendar (this week + next): {len(tvc) if tvc is not None else 'FAILED'} high/medium events")
    for r in (tvc or [])[:4]:
        print("     ", datetime.fromtimestamp(r[1], timezone.utc).strftime("%a %m-%d %H:%M"), r[2], r[3], r[4], r[5], r[6])
    if tvc:
        days = sorted({datetime.fromtimestamp(r[1], timezone.utc).strftime("%m-%d") for r in tvc})
        print("      days covered:", days[0], "to", days[-1])
    st, body = fetch(US_SCAN, data=sector_query(SECTOR_COS["xlk"], 3), timeout=20)
    print(f"  sector scan http {st}: {body[:300]}")
    cos = sector_companies(n=5)
    for sid in SECTOR_COS:
        print(f"  {sid:5s} " + (", ".join(f"{r['sym']} ({r['industry']})" for r in cos.get(sid, [])) or "NONE"))


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    else:
        refresh(force=True)
