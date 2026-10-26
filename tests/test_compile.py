import compileall
import os
import unittest

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")


class CompileTest(unittest.TestCase):
    def test_all_modules_compile(self):
        self.assertTrue(compileall.compile_dir(BIN, quiet=1, force=True, legacy=False))


if __name__ == "__main__":
    unittest.main()
