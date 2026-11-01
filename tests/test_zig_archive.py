"""ZIG archive: trade flow splits windows that hit MEXC's 1,000-row cap (no trade lost), taker side is read right,
the book's depth bands and the 15m backfill/keep-up work; canned MEXC replies only."""
import json
import os
import tempfile
import unittest
from urllib.parse import urlparse, parse_qs

from tests import _paths  # noqa: F401
import zig_archive as Z

NOW = 1_793_520_000_000 - 1_793_520_000_000 % Z.H + 1800_000   # mid-hour
FIRST_15M = NOW - 1200 * Z.M15
KEPT_FROM = NOW - 26 * Z.H                                       # MEXC keeps ~a day of trades: older ones are gone


def trades_in(a, b):
    """Two trades a minute; buyers take on even minutes. A busy hour has 2,400 trades (over the cap)."""
    out, t = [], a - a % 30_000 + (30_000 if a % 30_000 else 0)
    while t < b:
        out.append({"p": "0.05", "q": "100", "T": t, "m": (t // 60_000) % 2 == 1})
        t += 30_000 if (t // Z.H) % 2 == 0 else 1_500   # every other hour is busy (one trade each 1.5 s)
    return out


def fake(url):
    u = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if u.path == "/api/v3/aggTrades":
        rows = [t for t in trades_in(int(q["startTime"]), int(q["endTime"]) + 1) if t["T"] >= KEPT_FROM]
        return 200, {}, json.dumps(rows[:1000])
    if u.path == "/api/v3/klines" and q["interval"] == "60m":     # the full hour's volume, including trades no longer served
        a, b = int(q["startTime"]), int(q["endTime"])
        rows = [[t, "0.05", "0.05", "0.05", "0.05", "0", t + Z.H, str(sum(float(x["q"]) * float(x["p"]) for x in trades_in(t, t + Z.H)))]
                for t in range(a - a % Z.H, min(b + 1, NOW_CLOSED[0]), Z.H)]
        return 200, {}, json.dumps(rows)
    if u.path == "/api/v3/klines":
        a, b = int(q["startTime"]), int(q["endTime"])
        rows = [[t, "0.05", "0.051", "0.049", "0.05", "1000", t + Z.M15, "50"] for t in range(max(a, FIRST_15M) - max(a, FIRST_15M) % Z.M15, b + 1, Z.M15) if t >= FIRST_15M][:500]
        return 200, {}, json.dumps(rows)
    if u.path == "/api/v3/depth":
        return 200, {}, json.dumps({"bids": [["0.0499", "10000"], ["0.049", "10000"], ["0.047", "10000"]],
                                    "asks": [["0.0501", "10000"], ["0.051", "10000"], ["0.054", "10000"]]})
    if u.path == "/api/v3/ticker/24hr":
        return 200, {}, json.dumps({"lastPrice": "0.05", "priceChangePercent": "0.0123", "highPrice": "0.052", "lowPrice": "0.048", "quoteVolume": "250000"})
    return 404, {}, ""


NOW_CLOSED = [NOW - NOW % Z.H]                                    # candles exist only for closed hours


class ZigTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.out, self.db = os.path.join(d, "zig.json"), os.path.join(d, "zig.db")

    def test_refresh(self):
        self.assertTrue(Z.refresh(force=True, log=lambda *a: None, fetch=fake, out=self.out, db=self.db, now=NOW))
        d = json.load(open(self.out))
        fl = {r[0]: r for r in d["flow"]}
        busy = next(h for h in fl if (h // Z.H) % 2 == 1)
        self.assertEqual(fl[busy][1], 2400)                           # 2,400 trades: the capped window was split, none lost
        self.assertAlmostEqual(fl[busy][2] + fl[busy][3], 2400 * 100 * 0.05)
        quiet = next(h for h in fl if (h // Z.H) % 2 == 0)
        self.assertEqual(fl[quiet][1], 120)
        self.assertAlmostEqual(fl[quiet][2], 60 * 100 * 0.05)         # taker buys: the even minutes
        self.assertEqual(d["archive"]["flow_hours"], 25)               # 25 whole hours MEXC still serves
        self.assertEqual(d["archive"]["flow_incomplete"], 1)           # the hour cut by MEXC's retention: kept, flagged
        self.assertEqual(len(d["flow"]), 25)                           # older hours: not stored as "no trades"
        b = d["now"]["book"]
        self.assertEqual(round(b[3], 1), 40.0)                        # spread 0.0499 / 0.0501 = 40 bp
        self.assertAlmostEqual(b[4], 0.0499 * 10000)                  # within 1%: only the best bid
        self.assertAlmostEqual(b[6], (0.0499 + 0.049) * 10000)         # within 5% of the 0.05 mid: not 0.047 (6% away)
        self.assertAlmostEqual(b[9], (0.0501 + 0.051) * 10000)         # asks within 5%: not 0.054
        self.assertEqual(d["now"]["ticker"][3], 1.23)
        self.assertEqual(d["archive"]["k15_bars"], 1200)              # backfill reached the first bar
        before = d["archive"]["k15_bars"]
        NOW_CLOSED[0] = NOW - NOW % Z.H + 2 * Z.H
        Z.refresh(force=True, log=lambda *a: None, fetch=fake, out=self.out, db=self.db, now=NOW + 2 * Z.H)
        d2 = json.load(open(self.out))
        self.assertEqual(d2["archive"]["k15_bars"], before + 8)       # keeps up: two more hours of 15m bars
        self.assertEqual(d2["archive"]["flow_hours"], 27)
        NOW_CLOSED[0] = NOW - NOW % Z.H

    def test_nothing_answers(self):
        self.assertFalse(Z.refresh(force=True, log=lambda *a: None, fetch=lambda u: (400, {}, "down"), out=self.out, db=self.db, now=NOW))


if __name__ == "__main__":
    unittest.main()
