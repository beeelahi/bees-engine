"""tools/live_check.py against a fake site (no network): price coverage is counted over the universe's own pairs."""
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

_P = os.path.join(os.path.dirname(__file__), "..", "tools", "live_check.py")
_spec = importlib.util.spec_from_file_location("live_check", _P)
live_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live_check)


def _site(universe, priced):
    now = datetime.now(timezone.utc)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    candle = lambda h: [[int((now - timedelta(hours=h)).timestamp()), 1, 1, 1, 1, 1]]
    files = {
        "data/overview.json": {"generated_utc": iso(now), "last_date": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
                               "counts": {"pairs_filtered": len(universe)}, "status": {"last_run_ok": True}},
        "data/screener.json": {"columns": ["symbol"], "rows": [[s] for s in universe]},
        "data/prices.json": {"as_of": iso(now), "rows": {s: [1, 0, 1, 1, 1] for s in priced}},
        "data/h4/index.json": {"ZIGUSDT": "a.json"}, "data/h4/a.json": {"pairs": {"ZIGUSDT": candle(5)}},
        "data/h1/index.json": {"ZIGUSDT": "b.json"}, "data/h1/b.json": {"pairs": {"ZIGUSDT": candle(1.5)}},
        "data/bot.json": {"portfolio": {"integrity": "ok"}},
    }
    for f in ("highlights", "signals", "moves", "shortlist", "pairs/index"):
        files[f"data/{f}.json"] = {}

    def get(base, path):
        if path in files:
            return 200, json.dumps(files[path])
        return 200, '<meta http-equiv="Content-Security-Policy" content="x"><nav id="hlNav"></nav>'
    return get


class LiveCheckCoverage(unittest.TestCase):
    def run_check(self, universe, priced):
        live_check.get, saved = _site(universe, priced), live_check.get
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                code = live_check.main("https://example.test")
            return code, out.getvalue()
        finally:
            live_check.get = saved

    def test_all_priced_passes(self):
        uni = ["ZIGUSDT"] + [f"C{i}USDT" for i in range(99)]
        code, out = self.run_check(uni, uni)
        self.assertEqual(code, 0, out)
        self.assertIn("(100 of 100 universe pairs)", out)

    def test_extra_stock_rows_cannot_hide_missing_pairs(self):
        uni = ["ZIGUSDT"] + [f"C{i}USDT" for i in range(99)]
        priced = uni[:80] + [f"STOCK{i}" for i in range(30)]          # 110 rows, but only 80% of the universe
        code, out = self.run_check(uni, priced)
        self.assertEqual(code, 1, out)
        self.assertIn("live prices cover only 80% of the universe", out)


if __name__ == "__main__":
    unittest.main()
