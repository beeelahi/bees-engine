"""On-chain stocks: only the real issuers' tokens get in (copycats with the same name are everywhere), each gets its
company's sector and themes, and tokens far from the stock's price or with thin liquidity are flagged or dropped."""
import json
import os
import tempfile
import unittest

from tests import _paths  # noqa: F401
import onchain_stocks as O

NVDAX = {"id": "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh", "symbol": "NVDAx", "name": "NVIDIA xStock", "isVerified": True, "tags": ["xstocks", "stocks", "verified"],
         "usdPrice": 204.0, "liquidity": 2_500_000, "mcap": 6.5e7, "holderCount": 9000, "stats24h": {"buyVolume": 1e6, "sellVolume": 7e5, "priceChange": -0.6}}
SPYX = {"id": "XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W", "symbol": "SPYx", "name": "SP500 xStock", "isVerified": True, "tags": ["xstocks"],
        "usdPrice": 756.0, "liquidity": 3_400_000, "mcap": 7.2e7, "stats24h": {"buyVolume": 5e5, "sellVolume": 3e5, "priceChange": 0.03}}
COINON = {"id": "abcCoinOndoMintxxxxxxxxxxxxxxxxxxxxxxxxondo", "symbol": "COINon", "name": "Coinbase (Ondo Tokenized)", "isVerified": True, "tags": ["ondo", "verified"],
          "usdPrice": 300.0, "liquidity": 20_000, "mcap": 1e6, "stats24h": {"buyVolume": 100, "sellVolume": 50, "priceChange": 1.0}}
FAKE1 = dict(NVDAX, id="5qApz79WPjrmDcNWd952KPhXC2YzZp6uhPHSbnLBpump", isVerified=None, tags=["launchpad"], usdPrice=3.5e-06)   # pump.fun copycat
FAKE2 = dict(NVDAX, id="De6AWUU6PDL5yr1QQ5YqEUMzJ9iJHpEgLbQg9jrCXPFU", tags=["xstocks", "verified"])                             # right tag, wrong mint
AAPLX = dict(NVDAX, id="XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp", symbol="AAPLx", name="Apple xStock", usdPrice=500.0)       # 45% off the stock


def fake(url, timeout=15, data=None, jar=None, tv=False):
    if url.startswith(O.JUP_TAG + "xstocks"):
        return 200, json.dumps([NVDAX, SPYX, FAKE1, FAKE2, AAPLX])
    if url.startswith(O.JUP_TAG):
        return 200, json.dumps([COINON])
    if url.startswith(O.JUP_SEARCH):
        q = url[len(O.JUP_SEARCH):]
        pool = [NVDAX, SPYX, COINON, AAPLX, FAKE1]
        return 200, json.dumps([t for t in pool if t["id"] in q.split(",")] if "," in q or len(q) > 30 else [])
    if url == O.US_SCAN:
        rows = [("NASDAQ:NVDA", ["NVDA", "NVIDIA Corporation", 205.0, -0.5, 4.9e12, "Electronic Technology", "Semiconductors", "stock"]),
                ("AMEX:SPY", ["SPY", "SPDR S&P 500 ETF Trust", 755.0, 0.1, None, "Miscellaneous", "Investment Trusts/Mutual Funds", "fund"]),
                ("NASDAQ:COIN", ["COIN", "Coinbase Global, Inc.", 301.0, 1.0, 8e10, "Finance", "Investment Banks/Brokers", "stock"]),
                ("NASDAQ:AAPL", ["AAPL", "Apple Inc.", 345.0, -1.2, 5e12, "Electronic Technology", "Telecommunications Equipment", "stock"])]
        return 200, json.dumps({"data": [{"s": s, "d": d} for s, d in rows]})
    return 404, ""


class OnchainTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.out, self.db = os.path.join(d, "onchain.json"), os.path.join(d, "onchain.db")

    def test_issuer_check(self):
        self.assertEqual(O.issuer_of(NVDAX), "xstocks")
        self.assertEqual(O.issuer_of(COINON), "ondo")
        self.assertIsNone(O.issuer_of(FAKE1))                      # unverified copycat
        self.assertIsNone(O.issuer_of(FAKE2))                      # verified-looking but not the issuer's mint
        self.assertIsNone(O.issuer_of(dict(NVDAX, symbol="NVDA")))  # no issuer suffix

    def test_refresh_end_to_end(self):
        self.assertTrue(O.refresh(force=True, log=lambda *a: None, fetch=fake, out=self.out, db=self.db, cands=[]))
        d = json.load(open(self.out))
        got = {r["symbol"]: r for r in d["rows"]}
        self.assertEqual(set(got), {"NVDAx", "SPYx", "COINon"})    # AAPLx dropped: 45% away from the stock
        n = got["NVDAx"]
        self.assertEqual((n["ticker"], n["name"], n["sector"], n["type"]), ("NVDA", "NVIDIA Corporation", "Electronic Technology", "Stock"))
        self.assertIn("semis", n["themes"])
        self.assertIn("ai", n["themes"])
        self.assertIn("tech", n["themes"])
        self.assertEqual(n["vol24"], 1.7e6)
        self.assertAlmostEqual(n["premium_pct"], -0.49, places=2)
        self.assertTrue(n["tradable"])
        self.assertEqual(got["SPYx"]["type"], "ETF")
        self.assertEqual(got["SPYx"]["themes"], ["index"])
        self.assertIn("crypto", got["COINon"]["themes"])
        self.assertFalse(got["COINon"]["tradable"])               # $20K of liquidity: listed, but not used in baskets
        self.assertEqual([r["symbol"] for r in d["rows"]][0], "SPYx")   # deepest liquidity first

    def test_nothing_answers_keeps_old_file(self):
        O.refresh(force=True, log=lambda *a: None, fetch=fake, out=self.out, db=self.db, cands=[])
        before = open(self.out).read()
        self.assertFalse(O.refresh(force=True, log=lambda *a: None, fetch=lambda *a, **k: (0, ""), out=self.out, db=self.db, cands=[]))
        self.assertEqual(open(self.out).read(), before)


if __name__ == "__main__":
    unittest.main()
