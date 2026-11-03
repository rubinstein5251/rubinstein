"""Inventory pair accumulation strategy wiring."""

from __future__ import annotations

import unittest
from pathlib import Path


class InventoryPairStrategyTests(unittest.TestCase):
    def test_inventory_strategy_class_is_not_momentum_alias(self):
        from strategies.inventory_accumulation_strategy import (
            InventoryAccumulationStrategy,
            MomentumDelayedAdjustmentStrategy,
        )
        from strategies.inventory_pair_accumulation import InventoryPairAccumulationStrategy

        self.assertIs(InventoryAccumulationStrategy, InventoryPairAccumulationStrategy)
        self.assertIsNot(InventoryAccumulationStrategy, MomentumDelayedAdjustmentStrategy)

    def test_engine_module_present(self):
        self.assertTrue(Path("utils/dynamic_arbitrage_engine.py").is_file())

    def test_momentum_module_still_directional(self):
        src = Path("strategies/inventory_accumulation_strategy.py").read_text(encoding="utf-8")
        self.assertIn("class MomentumDelayedAdjustmentStrategy", src)
        self.assertNotIn("_try_opposite_entry", src)


if __name__ == "__main__":
    unittest.main()
