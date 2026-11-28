"""Pair chart files (web_export/pairs/sNN.json): small enough that opening one pair stays light."""
import unittest

from tests import _paths  # noqa: F401
import export_web as E


class PairShardTest(unittest.TestCase):
    def test_names(self):
        names = [E.shard_file(k) for k in range(E.SHARDS)]
        self.assertEqual(names[0], "s00.json")
        self.assertEqual(len(set(names)), E.SHARDS)
        self.assertEqual(names, sorted(names))           # two digits: s00 ... s95, so listings stay in order
        self.assertTrue(all(len(n) == len("s00.json") for n in names))

    def test_few_pairs_per_file(self):
        # 617 chart pairs on 28 Nov 2026 (499 universe + bStocks); room to grow to 700 at <= 8 pairs a file
        for n in (617, 700):
            per = [0] * E.SHARDS
            for i in range(n):
                per[i % E.SHARDS] += 1
            self.assertLessEqual(max(per), 8, n)
            self.assertLessEqual(max(per) - min(per), 1)


if __name__ == "__main__":
    unittest.main()
