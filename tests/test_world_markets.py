"""World markets tab: screener parsing, yields in basis points, news parsing and hygiene, impact scoring, calendar,
the hourly throttle and the summary. Canned responses only (no network)."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

from tests import _paths  # noqa: F401
import world_markets as W

NOW = time.time()
RFC = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")  # noqa: E731

RSS = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Fed holds rates, Powell signals &lt;b&gt;patience&lt;/b&gt;</title><link>https://example.com/a</link><pubDate>{RFC(NOW - 600)}</pubDate></item>
<item><title>Oil prices surge as OPEC cuts output</title><link>https://example.com/b</link><pubDate>{RFC(NOW - 1200)}</pubDate></item>
<item><title>Bad link</title><link>javascript:alert(1)</link><pubDate>{RFC(NOW - 60)}</pubDate></item>
<item><title>Old story about stocks</title><link>https://example.com/old</link><pubDate>{RFC(NOW - 5 * 86400)}</pubDate></item>
</channel></rss>"""
GNEWS = f"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Wall Street closes sharply lower on tariff fears - Reuters</title><link>https://news.google.com/x</link><pubDate>{RFC(NOW - 300)}</pubDate><source url="https://reuters.com">Reuters</source></item>
<item><title>Fed holds rates, Powell signals patience - WSJ</title><link>https://news.google.com/y</link><pubDate>{RFC(NOW - 900)}</pubDate><source url="https://wsj.com">WSJ</source></item>
</channel></rss>"""
ATOM = f"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>ECB keeps deposit rate unchanged</title>
<link href="https://ecb.example/p"/><updated>{datetime.fromtimestamp(NOW - 3600, timezone.utc).isoformat()}</updated></entry></feed>"""


def scan_reply(tickers):
    data = []
    for i, t in enumerate(tickers):
        if t == "PSX:KSE100":
            continue                                       # one symbol missing
        chg = -1.8 if t == "SP:SPX" else 0.5 + (i % 5) * 0.3
        data.append({"s": t, "d": [100.0 + i, chg, 0.07 if "US10Y" in t else 1.0, 1.0, 2.0, 3.0, 4.0, 5.0, 120.0, 80.0, "streaming", "USD"]})
    return {"totalCount": len(data), "data": data}


def fake(url, timeout=15, data=None, jar=None):
    if url == W.SCAN:
        return 200, json.dumps(scan_reply(data["symbols"]["tickers"]))
    if "news.google.com" in url and "markets" not in url and "stock" in url:
        return 200, GNEWS
    if "ecb" in url:
        return 200, ATOM
    if url == W.CALENDAR:
        t = datetime.fromtimestamp(NOW + 86400, timezone.utc).astimezone().isoformat()
        return 200, json.dumps([{"title": "CPI m/m", "country": "USD", "date": t, "impact": "High", "forecast": "0.3%", "previous": "0.4%"},
                                {"title": "Bank Holiday", "country": "JPY", "date": t, "impact": "Holiday", "forecast": "", "previous": ""}])
    if url == W.FNG:
        return 200, json.dumps({"data": [{"value": "28", "value_classification": "Fear"}] + [{"value": "40", "value_classification": "Fear"}] * 7})
    if "federalreserve" in url:
        return 200, RSS
    return 403, "denied"


class WorldTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.out, self.db = os.path.join(d, "world.json"), os.path.join(d, "world.db")

    def run_(self, force=True):
        return W.refresh(force=force, log=lambda *a: None, fetch=fake, out=self.out, db=self.db)

    def test_refresh_end_to_end(self):
        self.assertTrue(self.run_())
        d = json.load(open(self.out))
        g = d["groups"]
        for grp in ("Americas", "Europe", "Asia & Middle East", "Commodities", "Currencies", "Rates", "Sectors", "Companies"):
            self.assertIn(grp, g)
        self.assertNotIn("kse", [r["id"] for r in g["Asia & Middle East"]])
        self.assertIn("kse", d["sources"]["prices_missing"])
        us10 = next(r for r in g["Rates"] if r["id"] == "us10y")
        self.assertEqual(us10["d1_bp"], 7.0)                       # yield move in basis points
        spx = next(r for r in g["Americas"] if r["id"] == "spx")
        self.assertEqual(spx["d1"], -1.8)
        self.assertTrue(spx["spark"])
        self.assertIn("S&P 500 -1.80%", d["summary"]["lines"][0])
        self.assertEqual(len(d["summary"]["sector_leaders"]), 3)
        self.assertEqual(d["fear_greed"]["value"], 28)
        self.assertEqual([c["title"] for c in d["calendar"]], ["CPI m/m"])   # holidays and low impact left out

    def test_news_hygiene_scoring_and_dedupe(self):
        self.run_()
        n = json.load(open(self.out))["news"]
        titles = [x["title"] for x in n]
        self.assertNotIn("Bad link", titles)                       # javascript: link dropped
        self.assertNotIn("Old story about stocks", titles)         # older than 48 h
        self.assertTrue(all(x["link"].startswith("https://") for x in n))
        self.assertTrue(all("<" not in x["title"] for x in n))
        self.assertEqual(sum(1 for t in titles if t.startswith("Fed holds rates")), 1)   # same story from two feeds once
        wsj = next(x for x in n if "Wall Street" in x["title"])
        self.assertEqual(wsj["source"], "Reuters")                 # the publisher, not "Google News"
        self.assertFalse(wsj["title"].endswith("Reuters"))
        oil = next(x for x in n if x["title"].startswith("Oil"))
        self.assertIn("Energy", oil["tags"])
        self.assertEqual(oil["score"], 3)                          # energy (2) + a strong word (surge)
        self.assertTrue(any("ECB" in t for t in titles))           # Atom works

    def test_hourly_throttle(self):
        self.assertTrue(self.run_(force=False))
        self.assertFalse(self.run_(force=False))

    def test_nothing_answers_keeps_the_old_file(self):
        self.run_()
        before = open(self.out).read()
        self.assertFalse(W.refresh(force=True, log=lambda *a: None, fetch=lambda *a, **k: (0, ""), out=self.out, db=self.db))
        self.assertEqual(open(self.out).read(), before)

    def test_score(self):
        self.assertEqual(W.score("Powell says rate cut possible")[1], 3)
        self.assertEqual(W.score("Bitcoin edges higher")[0], ["Crypto"])
        self.assertEqual(W.score("A recipe for apple pie")[1], 0)


if __name__ == "__main__":
    unittest.main()
