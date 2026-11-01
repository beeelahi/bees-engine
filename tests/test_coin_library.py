"""Coin library: stablecoins / tokenized assets / wrapped copies left out on tags confirmed by price; exchange pairs
matched by ticker AND price (a same-ticker different coin is never mapped); Binance first, else the busiest of
KuCoin / Gate / MEXC above the volume floor; tracked pairs stay tracked; market totals add up. Canned data only."""
import json
import os
import sqlite3
import tempfile
import unittest

from tests import _paths  # noqa: F401
import coin_library as L


def coin(i, sym, name, px, tags=(), c30=5.0, c7=3.0, mcap=1e9):
    return {"id": i, "cmcRank": i, "symbol": sym, "name": name, "slug": name.lower(), "tags": list(tags), "dateAdded": "2024-01-01T00:00:00Z",
            "platform": None, "quotes": [{"price": px, "marketCap": mcap / i, "volume24h": 1e7, "percentChange24h": 1.0, "percentChange7d": c7,
                                           "percentChange30d": c30, "percentChange1y": 50.0, "fullyDilluttedMarketCap": 2 * mcap / i}]}


ROWS = [coin(1, "BTC", "Bitcoin", 80000), coin(2, "USDT", "Tether USDt", 1.0, ["stablecoin", "fiat-stablecoin"], 0.01, 0.0),
        coin(3, "XMR", "Monero", 300), coin(4, "PAXG", "PAX Gold", 4000, ["tokenized-gold"]), coin(5, "NVDAB", "NVIDIA bStock", 180, ["tokenized-stock", "bstocks"]),
        coin(6, "WBTC", "Wrapped Bitcoin", 80000), coin(7, "TWIN", "Twin One", 2.0), coin(8, "TWIN", "Twin Two", 0.05),
        coin(9, "BEAM", "Beam", 0.004), coin(10, "PEG", "Pegged Thing", 1.0, (), 0.5, 0.2), coin(11, "THIN", "Thin Coin", 0.3),
        coin(12, "STBL", "Stable-tagged volatile", 0.40, ["stablecoin"]), coin(13, "BONK", "Bonk", 0.00002)]
BOOKS = {"binance": {"BTC": (80100, 2e9, "BTCUSDT"), "BEAMX": (0.00405, 3e6, "BEAMXUSDT"), "USDT": (1.0, 1e9, "USDTUSDT")},
         "kucoin": {"XMR": (301, 3.6e7, "XMR-USDT"), "TWIN": (0.051, 5e5, "TWIN-USDT"), "THIN": (0.3, 4e4, "THIN-USDT")},
         "gate": {"XMR": (299, 2e6, "XMR_USDT"), "TWIN": (2.02, 9e5, "TWIN_USDT"), "STBL": (0.41, 3e5, "STBL_USDT"), "1000BONK": (0.0201, 8e5, "1000BONK_USDT")},
         "mexc": {"TWIN": (3.5, 9e6, "TWINUSDT")},                     # a third "TWIN" priced like neither coin: never mapped
         "_binance_all": {"BTCUSDT", "BEAMXUSDT", "OLDUSDT"}}


def totals(con, log):
    con.executemany("INSERT OR REPLACE INTO global_daily VALUES(?,?,?,?,?,?,?)",
                    [(1_790_000_000 + k * 86400, 1000.0 + k, 600.0, 100.0, 100.0, 200.0 + k, 50.0) for k in range(40)])
    con.executemany("INSERT OR REPLACE INTO altseason VALUES(?,?,?)", [(1_790_000_000 + k * 86400, 30.0 + k, 0) for k in range(40)])
    return 40


class LibraryTest(unittest.TestCase):
    def run_build(self, ext_prev=None):
        d = tempfile.mkdtemp()
        self.ext = os.path.join(d, "ext.json")
        if ext_prev is not None:
            json.dump(ext_prev, open(self.ext, "w"))
        self.lib, self.caps = os.path.join(d, "lib.json"), os.path.join(d, "caps.json")
        L.build(log=lambda *a: None, db=os.path.join(d, "l.db"), rows=ROWS, books=BOOKS, tr=[("CMC", "XMR", "Monero", 300, 3)],
                ext_file=self.ext, out_lib=self.lib, out_caps=self.caps, totals=totals)
        out = json.load(open(self.lib))
        ix = {c: i for i, c in enumerate(out["columns"])}
        return out, {r[ix["name"]]: dict(zip(out["columns"], r)) for r in out["rows"]}, json.load(open(self.ext))

    def test_exclusions(self):
        out, by, _ = self.run_build()
        why = {x[2]: x[3] for x in out["excluded"]}
        self.assertEqual(why["Tether USDt"], "stablecoin")
        self.assertEqual(why["PAX Gold"], "tokenized gold or commodity")
        self.assertEqual(why["NVIDIA bStock"], "tokenized stock or ETF")
        self.assertEqual(why["Wrapped Bitcoin"], "wrapped or staked copy of another coin")
        self.assertEqual(why["Pegged Thing"], "stablecoin (price held at $1)")
        self.assertIn("Stable-tagged volatile", by)              # tagged stablecoin but not priced like one: kept
        self.assertEqual(len(out["rows"]) + len(out["excluded"]), len(ROWS))

    def test_matching_and_venues(self):
        out, by, ext = self.run_build()
        self.assertEqual(by["Bitcoin"]["primary"], "Binance")
        self.assertEqual(by["Beam"]["primary"], "Binance")         # CMC "BEAM" trades on Binance as BEAMX (price within 3%)
        self.assertEqual(by["Monero"]["primary"], "KuCoin")        # busiest of KuCoin / Gate
        self.assertEqual(by["Monero"]["venues"], "KG")
        self.assertEqual(by["Monero"]["pair"], "XMRUSDT")
        self.assertEqual(by["Twin One"]["venues"], "G")            # the $2 TWIN is Gate's; KuCoin's $0.05 TWIN is the other coin
        self.assertEqual(by["Twin Two"]["venues"], "K")
        self.assertEqual(by["Twin One"]["pair"], "TWINUSDT")       # one symbol, one coin: Twin Two does not borrow Twin One's chart
        self.assertIsNone(by["Twin Two"]["pair"])
        self.assertNotIn("M", by["Twin One"]["venues"] + by["Twin Two"]["venues"])   # MEXC's TWIN matches neither price
        self.assertEqual(by["Bonk"]["venues"], "G")                # 1000BONK priced per 1,000
        self.assertEqual(by["Thin Coin"]["primary"], "KuCoin")
        self.assertIsNone(by["Thin Coin"]["pair"])                 # $40k a day: listed, not tracked
        self.assertEqual(ext["XMRUSDT"]["native"], "XMR-USDT")
        self.assertEqual(ext["1000BONKUSDT"]["native"], "1000BONK_USDT")
        self.assertNotIn("THINUSDT", ext)
        self.assertEqual(by["Monero"]["trending"], 1)
        self.assertEqual(out["counts"]["binance"], 2)

    def test_tracked_pairs_stay(self):
        _, by, ext = self.run_build({"THINUSDT": {"venue": "kucoin", "native": "THIN-USDT", "base": "THIN", "cmc_id": 11, "name": "Thin Coin", "added": "2026-10-01"}})
        self.assertEqual(ext["THINUSDT"]["added"], "2026-10-01")   # once tracked, kept (its history keeps growing)
        self.assertEqual(by["Thin Coin"]["pair"], "THINUSDT")

    def test_caps(self):
        self.run_build()
        c = json.load(open(self.caps))
        n = c["now"]
        self.assertEqual(n["total"], 1039.0)
        self.assertEqual(n["total2"], 1039.0 - 600)
        self.assertEqual(n["total3"], 1039.0 - 700)
        self.assertEqual(n["alts_ex_stables"], 239.0)
        self.assertAlmostEqual(n["btc_d"], 100 * 600 / 1039)
        self.assertEqual(c["d7"]["total"], 1032.0)
        self.assertEqual(c["altseason"]["now"], 69.0)
        self.assertEqual(len(c["series"]), 40)

    def test_global_history_rejects_parts_not_adding_up(self):
        con = sqlite3.connect(":memory:")
        con.executescript(L.SCHEMA)
        old = L._json
        L._json = lambda url, **k: {"data": {"points": [
            {"marketCap": 100, "btcValue": 60, "ethValue": 10, "stableValue": 10, "otherValue": 20, "volume": 5, "timestamp": "1790000000"},
            {"marketCap": 100, "btcValue": 60, "ethValue": 10, "stableValue": 10, "otherValue": 50, "volume": 5, "timestamp": "1790086400"}]}}
        try:
            L.global_history(con, log=lambda *a: None)
        finally:
            L._json = old
        self.assertEqual([r[0] for r in con.execute("SELECT ts FROM global_daily")], [1790000000])


if __name__ == "__main__":
    unittest.main()
