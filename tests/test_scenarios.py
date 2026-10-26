"""End-to-end scenarios of the live trading engine and the market maker against simulated exchanges."""
import contextlib
import io
import os
import runpy
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _run(name):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        runpy.run_path(os.path.join(HERE, "scenarios", name), run_name="__main__")
    return out.getvalue()


class Scenarios(unittest.TestCase):
    def test_live_engine(self):
        """Entry sizing, no duplicate entries, OCO take-profit, exit sells only the bot's own coins (never the
        owner's holdings), gap through stop, flatten switch, drawdown halt, shadow mode, price-drift guard."""
        self.assertIn("ALL TESTS PASSED", _run("live_scenario.py"))

    def test_market_maker(self):
        """Quote planning, inventory skew, band, minimum inventory, no churn, fills recorded, kill switch."""
        self.assertIn("MM TESTS PASSED", _run("mm_scenario.py"))


if __name__ == "__main__":
    unittest.main()
