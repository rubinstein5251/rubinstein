"""Matched-pair accumulation math and eligibility tests."""

from __future__ import annotations

import unittest

from utils.inventory_cost import InventorySnapshot
from utils.matched_pair_accumulation import (
    evaluate_pair_accumulation,
    max_opposite_price_before_fees,
)


class MatchedPairTests(unittest.TestCase):
    def test_max_opposite_from_87c_entry(self):
        # Conservative floor tick: 0.98 - 0.87 = 0.11 floored to $0.10
        self.assertAlmostEqual(max_opposite_price_before_fees(0.87, 0.98), 0.10)

    def test_eligible_when_opposite_cheap_enough(self):
        snap = InventorySnapshot(yes_shares=5.0, no_shares=0.0, yes_cost=4.35, no_cost=0.0)
        state = evaluate_pair_accumulation(
            snap,
            primary_side="YES",
            opposite_executable_price=0.10,
            opposite_executable_qty=5.0,
            target_combined_cost=0.98,
            min_net_profit_per_pair=0.005,
            fee_rate=0.0,
            slippage_buffer=0.01,
        )
        self.assertTrue(state.eligible)
        self.assertAlmostEqual(state.combined_if_filled or 0, 0.97, places=2)

    def test_reject_when_opposite_too_expensive(self):
        snap = InventorySnapshot(yes_shares=5.0, no_shares=0.0, yes_cost=4.35, no_cost=0.0)
        state = evaluate_pair_accumulation(
            snap,
            primary_side="YES",
            opposite_executable_price=0.15,
            opposite_executable_qty=5.0,
            target_combined_cost=0.98,
            min_net_profit_per_pair=0.005,
            fee_rate=0.0,
            slippage_buffer=0.01,
        )
        self.assertFalse(state.eligible)
        self.assertEqual(state.reason, "opposite_price_above_max")


if __name__ == "__main__":
    unittest.main()
