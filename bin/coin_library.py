"""
Coin library: the top crypto projects by market cap, stablecoins and wrapped / staked copies removed, each mapped to
the exchanges we trust (Binance, KuCoin, Gate, MEXC). Python 3.9 stdlib only. Public data only, no keys.
"""
import json
import os
import sys
import time

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
