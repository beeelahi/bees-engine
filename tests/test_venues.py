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
            return 200, {}, json.dumps({"lastPrice": "0.0416", "priceChangePercent": "0.0109", "highPrice": "0.0425", "lowPrice": "0.0407", "quoteVolume": "3665080.7"})
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
