"""Legacy sequential-pair tests removed — strategy is directional FOK only."""

import unittest
from pathlib import Path


class LegacyStrategyRemovedTests(unittest.TestCase):
    def test_no_opposite_leg_in_strategy_module(self):
        src = Path("strategies/inventory_accumulation_strategy.py").read_text(encoding="utf-8")
        self.assertNotIn("_try_opposite_entry", src)
        self.assertNotIn("initial_hedge", src)
        self.assertNotIn("paired_dca", src)


if __name__ == "__main__":
    unittest.main()
