"""The public copy of the owner's shortlist keeps only whitelisted fields and never the owner's notes."""
import json
import os
import tempfile
import unittest

from tests import _paths  # noqa: F401
import public_sl as P


class PublicShortlistTest(unittest.TestCase):
    def test_sanitize_and_change_detection(self):
        d = tempfile.mkdtemp()
        src, dst = os.path.join(d, "in.json"), os.path.join(d, "out.json")
        json.dump({"as_of": "2026-10-26T11:00:00Z", "rows": [
            {"symbol": "ABCUSDT", "status": "active", "entry": 2.0, "entry_date": "2026-10-24", "stop": 1.8,
             "note": "private plan", "approved_by": "u_x", "reasons": ["1d:x"]},
            {"symbol": "BAD<script>", "status": "active", "entry": 1, "entry_date": "2026-10-24", "stop": 0.5},
            {"symbol": "XYZUSDT", "status": "closed", "entry": 200, "entry_date": "2026-10-01", "stop": 180,
             "exit": 220, "exit_date": "2026-10-20", "close_reason": "my private reason"},
            {"symbol": "QQQUSDT", "status": "active", "entry": 1, "entry_date": "2026-10-24", "stop": 2},
            {"symbol": "TUSDT", "status": "active", "entry": 1, "entry_date": "2026-10-23", "stop": 0.9}]}, open(src, "w"))
        self.assertTrue(P.sync(lambda *a: None, src, dst))
        self.assertFalse(P.sync(lambda *a: None, src, dst))
        out = json.load(open(dst))
        self.assertEqual([r["symbol"] for r in out["rows"]], ["ABCUSDT", "TUSDT", "XYZUSDT"])
        text = open(dst).read()
        for secret in ("private plan", "u_x", "my private reason", "reasons"):
            self.assertNotIn(secret, text)
        self.assertEqual(out["rows"][2]["close_reason"], "Closed")


if __name__ == "__main__":
    unittest.main()
