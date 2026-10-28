"""Live view of the paper bots between closes: entries at the open after the signal, stops and profit-takes hourly."""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from tests import _paths  # noqa: F401
import bot_live as L

H = 3_600_000
D0 = L.day_ms("2026-10-28")          # today 00:00 UTC; the daily data runs through 2026-10-27


def db(bars):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE candles_1h(symbol TEXT, open_ms INTEGER, open REAL, high REAL, low REAL, close REAL, close_ms INTEGER)")
    for sym, rows in bars.items():
        for t, o, h, l, c in rows:
            con.execute("INSERT INTO candles_1h VALUES(?,?,?,?,?,?,?)", (sym, t, o, h, l, c, t + H))
    return con


def bot_json(orders=(), open_=()):
    return {"data_through": "2026-10-27", "bots": [{"id": "leaders", "paper": {"equity": 40000.0, "open": list(open_), "orders": list(orders)},
            "strategy": {"config": {"max_pos": 10, "risk": 0.01, "max_pos_frac": 0.2, "max_adv_frac": 0.005, "max_gross": 1.0},
                         "params": {"tp_r": 2.0, "tp_frac": 1 / 3, "be_after_tp": True}}}]}


class LiveTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def write(self, name, obj):
        with open(os.path.join(self.d, name), "w") as f:
            json.dump(obj, f)

    def test_new_entry_shows_at_todays_open_and_takes_profit_then_breakeven(self):
        o = {"symbol": "AAAUSDT", "side": "long", "signal_date": "2026-10-27", "stop_dist": 1.0, "adv": 50e6}
        bars = {"AAAUSDT": [(D0, 10.0, 10.5, 9.8, 10.4), (D0 + H, 10.4, 12.2, 10.3, 12.0), (D0 + 2 * H, 12.0, 12.1, 10.0, 10.1)]}
        self.write("bot.json", bot_json(orders=[o]))
        self.assertTrue(L.run(db(bars), self.d, now_ms=D0 + 3 * H + 1))
        out = json.load(open(os.path.join(self.d, "bot_live.json")))
        e = out["daily"]["bots"]["leaders"]["entries"][0]
        self.assertAlmostEqual(e["entry"], 10.0 * 1.0005)                 # today's open + slippage, not tomorrow
        self.assertEqual(e["entry_utc"], "2026-10-28T00:00:00Z")
        self.assertEqual(e["events"][0]["what"], "take_profit")             # +2R in hour two
        self.assertEqual(e["status"], "stopped")                            # then back to break-even in hour three
        self.assertAlmostEqual(e["exit"], e["entry"])
        self.assertFalse(L.run(db(bars), self.d, now_ms=D0 + 3 * H + 1))    # unchanged: not rewritten

    def test_open_position_stop_and_exit_at_open(self):
        pos = [{"symbol": "BBBUSDT", "side": "long", "entry": 5.0, "stop": 4.5, "stop0": 4.5, "qty_open": 100, "last": 5.0, "slip": 0.0},
               {"symbol": "CCCUSDT", "side": "long", "entry": 2.0, "stop": 1.8, "stop0": 1.8, "qty_open": 100, "last": 2.0, "slip": 0.0, "exit_next_open": True}]
        bars = {"BBBUSDT": [(D0, 4.9, 5.0, 4.4, 4.6)], "CCCUSDT": [(D0, 2.1, 2.2, 2.0, 2.1)]}
        self.write("bot.json", bot_json(open_=pos))
        L.run(db(bars), self.d, now_ms=D0 + H + 1)
        p = {x["symbol"]: x for x in json.load(open(os.path.join(self.d, "bot_live.json")))["daily"]["bots"]["leaders"]["positions"]}
        self.assertEqual(p["BBBUSDT"]["status"], "stopped")
        self.assertAlmostEqual(p["BBBUSDT"]["exit"], 4.5)
        self.assertEqual(p["CCCUSDT"]["status"], "sold at today's open")
        self.assertAlmostEqual(p["CCCUSDT"]["exit"], 2.1)

    def test_fade4h_entry_is_shown_at_the_next_candle_open(self):
        sig = D0 + 8 * H                                                    # signal candle 08:00-12:00
        p4 = {"equity": 10000.0, "rules": {"cost_pct": 0.15, "risk_pct": 1}, "open": [],
              "pending": [{"symbol": "INITUSDT", "signal_ms": sig, "stop": 0.235, "pump_pct": 23.0}]}
        bars = {"INITUSDT": [(sig + 4 * H, 0.2179, 0.2236, 0.215, 0.22), (sig + 5 * H, 0.22, 0.236, 0.219, 0.233)]}
        self.write("paper4h.json", p4)
        L.run(db(bars), self.d, now_ms=sig + 6 * H + 1)
        e = json.load(open(os.path.join(self.d, "bot_live.json")))["fade4h"]["entries"][0]
        self.assertEqual(e["entry_utc"], "2026-10-28T12:00:00Z")            # entered at 12:00, visible at once
        self.assertEqual(e["status"], "stopped")                            # the stop at 0.235 was hit at 13:00
        self.assertEqual(e["exit_utc"], "2026-10-28T13:00:00Z")


if __name__ == "__main__":
    unittest.main()
