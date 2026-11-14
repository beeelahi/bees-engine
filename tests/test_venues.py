"""Pairs tracked from another exchange: MEXC replies are reshaped into Binance rows, windows paged correctly."""
import json
import unittest

from tests import _paths  # noqa: F401
import venues as V

D = 86_400_000


class FakeMexc:
    """Daily candles from day 100 to day 1300; 500 bars per call max; needs start AND end (like MEXC)."""

    def __init__(self):
        self.calls = []

    def __call__(self, url):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append((u.path, q))
        if u.path == "/api/v3/klines":
            assert q["interval"] in ("1d", "60m", "4h", "1W")
            s, e, lim = int(q["startTime"]), int(q["endTime"]), int(q["limit"])
            assert lim <= 500
            rows = [[t, "1", "2", "0.5", "1.5", "10", t + D, "15"] for t in range(100 * D, 1301 * D, D) if s <= t <= e][:lim]
            return 200, {}, json.dumps(rows)
        if u.path == "/api/v3/ticker/24hr":
            return 200, {}, json.dumps({"symbol": "ZIGUSDT", "lastPrice": "0.0416", "priceChangePercent": "0.0109", "highPrice": "0.0425", "lowPrice": "0.0407", "quoteVolume": "3665080.7"})
        if u.path == "/api/v3/exchangeInfo":
            return 200, {}, json.dumps({"symbols": [{"symbol": "ZIGUSDT", "status": "1", "baseAsset": "ZIG", "quoteAsset": "USDT", "isSpotTradingAllowed": True}]})
        return 404, {}, "{}"


class VenuesTest(unittest.TestCase):
    def test_klines_paged_and_reshaped(self):
        f = FakeMexc()
        rows = V.klines(f, {"symbol": "ZIGUSDT", "interval": "1d", "startTime": 0, "limit": 1000}, now_ms=2000 * D)
        self.assertEqual(len(rows), 1000)                              # caller asked 1000: paged in windows of 500
        self.assertEqual(rows[0][0], 100 * D)                          # empty windows before the listing skipped
        self.assertEqual([r[0] for r in rows], sorted({r[0] for r in rows}))
        self.assertEqual(rows[0][6], 101 * D - 1)                      # Binance close time convention
        self.assertEqual(rows[0][7], "15")
        self.assertIsNone(rows[0][8])                                  # no trade count / taker volume: None, not invented
        nxt = V.klines(f, {"symbol": "ZIGUSDT", "interval": "1d", "startTime": rows[-1][0] + D, "limit": 1000}, now_ms=2000 * D)
        self.assertEqual(len(nxt), 201)                                # the rest, then the caller stops (< limit)
        self.assertTrue(all(q.get("endTime") for p, q in f.calls))   # never a start-only call (MEXC returns latest bars)

    def test_end_time_respected(self):
        rows = V.klines(FakeMexc(), {"symbol": "ZIGUSDT", "interval": "1d", "startTime": 200 * D, "endTime": 205 * D, "limit": 1000})
        self.assertEqual([r[0] // D for r in rows], [200, 201, 202, 203, 204, 205])

    def test_ticker_percent_and_info(self):
        t = V.ticker24(FakeMexc(), "ZIGUSDT")
        self.assertAlmostEqual(float(t["priceChangePercent"]), 1.09)
        self.assertEqual(V.exchange_info(FakeMexc(), "ZIGUSDT")["status"], "TRADING")
        self.assertTrue(V.is_external("ZIGUSDT"))
        self.assertFalse(V.is_external("BTCUSDT"))

    def test_api_routes_external_klines(self):
        import binance_sync as B
        old = B._fetch
        B._fetch = FakeMexc()
        try:
            rows = B.api("/api/v3/klines", {"symbol": "ZIGUSDT", "interval": "1d", "startTime": 100 * D, "endTime": 101 * D, "limit": 1000})
            self.assertEqual(len(rows), 2)
            self.assertTrue(B._fetch.calls[0][0] == "/api/v3/klines")
        finally:
            B._fetch = old


class FakeGateKucoin:
    """Gate (1,000 points a call, refuses windows before day 300 as too old) and KuCoin (1,500 rows a call, newest
    first); daily candles from day 100 (KuCoin) / day 300 (Gate) to day 1300."""

    def __init__(self, gate_down=False):
        self.calls, self.gate_down = [], gate_down

    def __call__(self, url):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append((u.netloc, u.path, q))
        if "gateio" in u.netloc:
            if self.gate_down:
                return 503, {}, "down"
            if u.path == "/api/v4/spot/candlesticks":
                a, b = int(q["from"]), int(q["to"])
                assert (b - a) // 86400 + 1 <= 1000
                if a < 300 * 86400:
                    return 400, {}, '{"label":"INVALID_PARAM_VALUE","message":"too long ago"}'
                rows = [[str(t), "15", "1.5", "2", "0.5", "1", "10", "true"] for t in range(300 * 86400, 1301 * 86400, 86400) if a <= t <= b]
                return 200, {}, json.dumps(rows)
            if u.path == "/api/v4/spot/currency_pairs":
                return 200, {}, json.dumps([{"id": "GGG_USDT", "base": "GGG", "quote": "USDT", "trade_status": "tradable"}])
            if u.path == "/api/v4/spot/tickers":
                return 200, {}, json.dumps([{"currency_pair": "GGG_USDT", "last": "2", "change_percentage": "-3.5", "high_24h": "2.2", "low_24h": "1.9", "quote_volume": "250000"}])
        if "kucoin" in u.netloc:
            if u.path == "/api/v1/market/candles":
                a, b = int(q["startAt"]), int(q["endAt"])
                rows = [[str(t), "1", "1.5", "2", "0.5", "10", "15"] for t in range(100 * 86400, 1301 * 86400, 86400) if a <= t < b][::-1][:1500]
                return 200, {}, json.dumps({"code": "200000", "data": rows})
            if u.path == "/api/v2/symbols":
                return 200, {}, json.dumps({"code": "200000", "data": [{"symbol": "KKK-USDT", "baseCurrency": "KKK", "quoteCurrency": "USDT", "enableTrading": True}]})
            if u.path == "/api/v1/market/allTickers":
                return 200, {}, json.dumps({"code": "200000", "data": {"ticker": [{"symbol": "KKK-USDT", "last": "3", "changeRate": "0.021", "high": "3.1", "low": "2.8", "volValue": "180000"}]}})
        return 404, {}, "{}"


class GateKucoinTest(unittest.TestCase):
    def setUp(self):
        self.old = (dict(V.EXTERNAL), dict(V.NATIVE))
        V.EXTERNAL.update({"GGGUSDT": "gate", "KKKUSDT": "kucoin"})
        V.NATIVE.update({"GGGUSDT": "GGG_USDT", "KKKUSDT": "KKK-USDT"})

    def tearDown(self):
        V.EXTERNAL.clear(), V.EXTERNAL.update(self.old[0]), V.NATIVE.clear(), V.NATIVE.update(self.old[1])

    def test_gate_reshaped_and_old_windows_skipped(self):
        rows = V.klines(FakeGateKucoin(), {"symbol": "GGGUSDT", "interval": "1d", "startTime": 0, "limit": 5000}, now_ms=2000 * D)
        self.assertEqual(rows[0][0], 300 * D)                          # the windows Gate refused were skipped, not fatal
        self.assertEqual(len(rows), 1001)                              # days 300..1300, none lost at the boundary
        self.assertEqual(rows[0][1:6], ["1", "2", "0.5", "1.5", "10"])  # Gate's [t, qv, c, h, l, o, bv] -> o h l c vb
        self.assertEqual(rows[0][7], "15")
        self.assertEqual(rows[0][6], 301 * D - 1)

    def test_kucoin_reshaped_oldest_first(self):
        rows = V.klines(FakeGateKucoin(), {"symbol": "KKKUSDT", "interval": "1d", "startTime": 0, "limit": 5000}, now_ms=2000 * D)
        self.assertEqual(len(rows), 1201)
        self.assertEqual([r[0] for r in rows], sorted(r[0] for r in rows))
        self.assertEqual(rows[0][1:6], ["1", "2", "0.5", "1.5", "10"])  # KuCoin's [t, o, c, h, l, vb, vq]
        self.assertEqual(rows[-1][0], 1300 * D)

    def test_bulk_status_ticker_and_failed_venue(self):
        d, failed = V.bulk(FakeGateKucoin(), ["GGGUSDT", "KKKUSDT"])
        self.assertEqual(failed, set())
        self.assertEqual(d["GGGUSDT"]["status"], "TRADING")
        self.assertAlmostEqual(float(d["GGGUSDT"]["ticker"]["priceChangePercent"]), -3.5)   # Gate: already percent
        self.assertAlmostEqual(float(d["KKKUSDT"]["ticker"]["priceChangePercent"]), 2.1)    # KuCoin: a fraction
        self.assertEqual(d["KKKUSDT"]["ticker"]["quoteVolume"], "180000")
        sl, V.time.sleep = V.time.sleep, lambda x: None
        try:
            d, failed = V.bulk(FakeGateKucoin(gate_down=True), ["GGGUSDT", "KKKUSDT"])
        finally:
            V.time.sleep = sl
        self.assertEqual(failed, {"gate"})                             # caller keeps Gate's pairs as they were
        self.assertNotIn("GGGUSDT", d)

    def test_loads_library_file_safely(self):
        import os
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "x.json")
        json.dump({"AAAUSDT": {"venue": "gate", "native": "AAA_USDT"}, "BAD USDT": {"venue": "gate", "native": "x"},
                   "CCCUSDT": {"venue": "ftx", "native": "C"}, "ZIGUSDT": {"venue": "gate", "native": "ZIG_USDT"}}, open(p, "w"))
        ext, nat = V._load(p)
        self.assertEqual(ext["AAAUSDT"], "gate")
        self.assertNotIn("BAD USDT", ext)
        self.assertNotIn("CCCUSDT", ext)                               # only venues we trust
        self.assertEqual(ext["ZIGUSDT"], "mexc")                       # pinned entries can't be overridden
        self.assertEqual(V._load(p + ".missing")[0], V.PINNED)


if __name__ == "__main__":
    unittest.main()


class AnalyticsWithExternalPair(unittest.TestCase):
    """A pair without trade counts / taker volume (MEXC) goes through the whole analytics build."""

    def test_runs(self):
        import random
        import sqlite3
        from datetime import date, timedelta
        import binance_sync as B
        import analytics as A
        con = sqlite3.connect(":memory:")
        con.executescript(B.SCHEMA)
        con.execute("INSERT INTO meta VALUES('history_start','2017-01-01')")
        d0, rnd = date(2024, 1, 1), random.Random(1)
        for sym, ext in (("BTCUSDT", False), ("ZIGUSDT", True)):
            con.execute("INSERT INTO pairs(symbol, token, status, first_seen, backfilled, first_candle) VALUES(?,?,?,?,1,?)",
                        (sym, sym[:-4], "TRADING", "2024-01-01", "2024-01-01"))
            p = 100.0
            for i in range(400):
                d = d0 + timedelta(days=i)
                o, p = p, p * (1 + rnd.gauss(0, 0.03))
                ms = int(B.d2ms(d.isoformat()))
                con.execute("INSERT INTO daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (sym, d.isoformat(), o, max(o, p), min(o, p), p, 1e6, 5e6, None if ext else 1000,
                             None if ext else 2e5, None if ext else 2.5e6, ms, ms + D - 1, "x"))
        con.commit()
        A.run(con, verbose=False)
        n = con.execute("SELECT COUNT(*) FROM candles WHERE symbol='ZIGUSDT' AND tf='1w' AND trades IS NULL").fetchone()[0]
        self.assertGreater(n, 50)                                  # weekly candles built, trade count left empty
        self.assertEqual(con.execute("SELECT COUNT(*) FROM latest WHERE symbol='ZIGUSDT'").fetchone()[0], 1)


class CurlUrlGuardTest(unittest.TestCase):
    """The URL goes into a curl --config on stdin: a quote, backslash or line break could add curl options of its own
    (an output file, a header), and only http(s) may be fetched."""

    def test_plain_urls_pass(self):
        u = "https://api.mexc.com/api/v3/klines?symbol=ZIGUSDT&interval=1d&limit=500"
        self.assertEqual(V.curl_config_url(u), 'url = "%s"\n' % u)
        self.assertTrue(V.curl_config_url("http://example.com/a b").startswith('url = "http://'))

    def test_injection_and_other_schemes_refused(self):
        for bad in ('https://x.example/"\noutput = "/tmp/pwn', "https://x.example/a\\b", "https://x.example/\r\nheader = X",
                    "https://x.example/\x00", "file:///etc/passwd", "ftp://x.example/", "", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                V.curl_config_url(bad)

    def test_curl_limited_to_http_on_redirects_too(self):
        self.assertEqual(V.CURL_PROTO, ["--proto", "=http,https", "--proto-redir", "=http,https"])

    def test_world_fetch_refuses_without_running_curl(self):
        import subprocess
        import world_markets as W
        real, calls = subprocess.run, []
        subprocess.run = lambda *a, **k: calls.append(a) or real(["false"])
        try:
            self.assertEqual(W.fetch('https://x.example/"\noutput = "/tmp/pwn'), (0, ""))
            self.assertEqual(W.fetch("file:///etc/passwd"), (0, ""))
        finally:
            subprocess.run = real
        self.assertEqual(calls, [])
