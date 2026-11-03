"""Dynamic arbitrage engine: hedge timeout, DCA, rebalance scenarios."""

from __future__ import annotations

import time
import unittest

from utils.dynamic_arbitrage_engine import ArbitrageAction, ArbitrageConfig, evaluate_inventory_tick
from utils.inventory_cost import InventorySnapshot, matched_combined_average
from utils.matched_pair_accumulation import max_opposite_price_before_fees


class DynamicArbitrageScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = ArbitrageConfig(
            target_combined_cost=0.98,
            max_combined_cost=0.98,
            emergency_max_combined_cost=1.0,
            min_order_size=5.0,
            max_slippage=0.0,
            max_first_leg_price=0.92,
        )

    def test_max_opposite_after_55c_up_entry(self):
        from utils.dynamic_arbitrage_engine import _exact_max_opposite_price

        self.assertAlmostEqual(_exact_max_opposite_price(0.55, 0.98), 0.43)
        # Conservative cap (used elsewhere) stays 1¢ below exact for float-safe floors
        self.assertAlmostEqual(max_opposite_price_before_fees(0.55, 0.98), 0.42)

    def test_ideal_hedge_at_43c(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=0, yes_cost=2.75, no_cost=0)
        now = time.time()
        decision = evaluate_inventory_tick(
            snap,
            now=now,
            hedge_deadline_at=now + 10,
            first_leg_side="YES",
            yes_exe=(0.55, 5.0),
            no_exe=(0.43, 5.0),
            cfg=self.cfg,
        )
        self.assertEqual(decision.action, ArbitrageAction.IDEAL_HEDGE)
        self.assertEqual(decision.side, "NO")
        self.assertAlmostEqual(decision.projected_combined or 0, 0.98, places=2)

    def test_force_hedge_above_100c_after_timeout(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=0, yes_cost=2.75, no_cost=0)
        now = 1000.0
        decision = evaluate_inventory_tick(
            snap,
            now=now,
            hedge_deadline_at=now - 0.1,
            first_leg_side="YES",
            yes_exe=(0.55, 5.0),
            no_exe=(0.52, 5.0),
            cfg=self.cfg,
        )
        self.assertEqual(decision.action, ArbitrageAction.EMERGENCY_HEDGE)
        self.assertAlmostEqual(decision.projected_combined or 0, 1.07, places=2)
        self.assertEqual(decision.reason, "force_hedge_after_timeout")

    def test_emergency_hedge_at_45c_after_timeout(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=0, yes_cost=2.75, no_cost=0)
        now = 1000.0
        decision = evaluate_inventory_tick(
            snap,
            now=now,
            hedge_deadline_at=now - 0.1,
            first_leg_side="YES",
            yes_exe=(0.55, 5.0),
            no_exe=(0.45, 5.0),
            cfg=self.cfg,
        )
        self.assertEqual(decision.action, ArbitrageAction.EMERGENCY_HEDGE)
        self.assertAlmostEqual(decision.projected_combined or 0, 1.0, places=2)

    def test_average_down_lowers_combined_to_90c(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=5, yes_cost=2.75, no_cost=2.25)
        self.assertAlmostEqual(matched_combined_average(snap), 1.0)
        decision = evaluate_inventory_tick(
            snap,
            now=time.time(),
            hedge_deadline_at=0,
            first_leg_side="",
            yes_exe=(0.75, 5.0),
            no_exe=(0.25, 5.0),
            cfg=self.cfg,
        )
        self.assertEqual(decision.action, ArbitrageAction.AVERAGE_DOWN)
        self.assertEqual(decision.side, "NO")
        after = snap.with_buy("NO", 5, 0.25)
        self.assertAlmostEqual(matched_combined_average(after), 0.90, places=2)

    def test_rebalance_five_up_ten_down_at_75c_up(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=10, yes_cost=2.75, no_cost=3.50)
        self.assertAlmostEqual(snap.no_avg(), 0.35)
        decision = evaluate_inventory_tick(
            snap,
            now=time.time(),
            hedge_deadline_at=0,
            first_leg_side="",
            yes_exe=(0.75, 5.0),
            no_exe=(0.25, 5.0),
            cfg=self.cfg,
        )
        self.assertEqual(decision.action, ArbitrageAction.REBALANCE)
        self.assertEqual(decision.side, "YES")
        after = snap.with_buy("YES", 5, 0.75)
        self.assertAlmostEqual(matched_combined_average(after), 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
