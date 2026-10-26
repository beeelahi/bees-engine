"""Picks are added and closed only through tools/pick.py, with entry/stop taken from published data."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class PickToolTest(unittest.TestCase):
    def setUp(self):
        self.backup = open(os.path.join(ROOT, "picks", "picks.json")).read()
        self.d = tempfile.mkdtemp()
        json.dump({"columns": ["symbol", "last_close", "d_atr14_pct"], "rows": [["ABCUSDT", 2.0, 5.0]]}, open(self.d + "/screener.json", "w"))
        json.dump({"last_date": "2026-10-26"}, open(self.d + "/overview.json", "w"))

    def tearDown(self):
        open(os.path.join(ROOT, "picks", "picks.json"), "w").write(self.backup)
        shutil.rmtree(self.d, ignore_errors=True)

    def run_tool(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "tools", "pick.py"), *args, "--data", self.d], capture_output=True, text=True)

    def test_add_twice_and_close(self):
        self.assertEqual(self.run_tool("add", "abc").returncode, 0)
        self.assertNotEqual(self.run_tool("add", "ABC").returncode, 0)          # no duplicate active pick
        self.assertNotEqual(self.run_tool("add", "ZZZ").returncode, 0)          # unknown symbol
        self.assertEqual(self.run_tool("close", "ABC", "--reason", "Stop hit").returncode, 0)
        rows = json.load(open(os.path.join(ROOT, "picks", "picks.json")))["rows"]
        self.assertEqual(rows[-1]["status"], "closed")
        self.assertAlmostEqual(rows[-1]["stop"], 1.8)


if __name__ == "__main__":
    unittest.main()
