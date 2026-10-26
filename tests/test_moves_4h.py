"""Big 4-hour moves: classification and the moves.json feed (new events reported once)."""
import json
import os
import sqlite3
import tempfile
import unittest

from tests import _paths  # noqa: F401
import moves_4h as M

H = 4 * 3600 * 1000


class MovesTest(unittest.TestCase):
    def test_feed(self):
        con = sqlite3.connect(":memory:")
        con.executescript("""CREATE TABLE candles_4h(symbol, open_ms, open, high, low, close, volume_usdt, close_ms);
                             CREATE TABLE h4_state(symbol, backfilled);
                             INSERT INTO h4_state VALUES('ABCUSDT', 1), ('USDCUSDT', 1);""")
        t0, rows = 1_760_000_000_000, []
        for k in range(200):
            p, v = 10.0, 1_000_000.0
            if k == 199:
                p, v = 12.0, 5_000_000.0                            # +20% on 5x volume
            rows.append(("ABCUSDT", t0 + k * H, 10.0, p * 1.01, 9.9, p, v, t0 + (k + 1) * H - 1))
            rows.append(("USDCUSDT", t0 + k * H, 1, 1.2, 0.8, 1.0 if k < 199 else 1.2, 9e9, t0 + (k + 1) * H - 1))
        con.executemany("INSERT INTO candles_4h VALUES(?,?,?,?,?,?,?,?)", rows)
        d = tempfile.mkdtemp()
        now = t0 + 200 * H
        new = M.run(con, d, now)
        self.assertEqual([(x["symbol"], x["kind"]) for x in new], [("ABCUSDT", "pump_15")])
        self.assertEqual(new[0]["ret_pct"], 20.0)
        self.assertEqual(M.run(con, d, now), [])                   # same event is not new again
        out = json.load(open(os.path.join(d, "moves.json")))
        self.assertEqual(len(out["rows"]), 1)


if __name__ == "__main__":
    unittest.main()
