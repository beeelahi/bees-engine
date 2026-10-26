import json
import os
import sqlite3
import tempfile
import unittest

from tests import _paths  # noqa: F401
import prices


class PricesTest(unittest.TestCase):
    def test_universe_only_and_guard(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE latest(symbol TEXT, in_universe INTEGER)")
        con.executemany("INSERT INTO latest VALUES(?,?)", [(f"C{i}USDT", 1) for i in range(12)] + [("OFFUSDT", 0)])
        tick = [{"symbol": f"C{i}USDT", "lastPrice": "1.5", "priceChangePercent": "2.345", "highPrice": "1.6",
                 "lowPrice": "1.4", "quoteVolume": "1000000.4"} for i in range(12)]
        tick.append({"symbol": "OFFUSDT", "lastPrice": "1"})
        d = tempfile.mkdtemp()
        self.assertEqual(prices.run(con, lambda path: tick, d), 12)
        data = json.load(open(os.path.join(d, "prices.json")))
        self.assertNotIn("OFFUSDT", data["rows"])
        self.assertEqual(data["rows"]["C0USDT"], [1.5, 2.35, 1.6, 1.4, 1000000])
        self.assertEqual(prices.run(con, lambda path: tick[:3], d), 0)       # partial reply keeps the old file


if __name__ == "__main__":
    unittest.main()
