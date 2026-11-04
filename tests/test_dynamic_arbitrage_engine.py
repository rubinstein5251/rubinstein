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

    def test_paired_average_down_improves_combined(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=5, yes_cost=2.75, no_cost=2.25)
        self.assertAlmostEqual(matched_combined_average(snap), 1.0)
        decision = evaluate_inventory_tick(
            snap,
            now=time.time(),
            hedge_deadline_at=0,
            first_leg_side="",
            yes_exe=(0.47, 5.0),
            no_exe=(0.49, 5.0),
            cfg=self.cfg,
        )
        self.assertEqual(decision.action, ArbitrageAction.AVERAGE_DOWN)
        self.assertTrue(decision.paired)
        after = snap.with_buy("YES", 5, 0.47).with_buy("NO", 5, 0.49)
        self.assertAlmostEqual(matched_combined_average(after), 0.98, places=2)

    def test_skip_dca_when_target_already_achieved(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=5, yes_cost=2.40, no_cost=2.50)
        self.assertAlmostEqual(matched_combined_average(snap), 0.98)
        decision = evaluate_inventory_tick(
            snap,
            now=time.time(),
            hedge_deadline_at=0,
            first_leg_side="",
            yes_exe=(0.40, 5.0),
            no_exe=(0.40, 5.0),
            cfg=self.cfg,
            target_achieved=True,
        )
        self.assertEqual(decision.action, ArbitrageAction.NONE)

    def test_endgame_rebalance_waives_combined_cap(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=10, yes_cost=2.75, no_cost=3.50)
        cfg = ArbitrageConfig(
            target_combined_cost=0.98,
            max_combined_cost=0.98,
            rebalance_max_combined_cost=1.0,
            endgame_rebalance_seconds=90.0,
            endgame_rebalance_max_combined_cost=1.03,
            min_order_size=5.0,
            max_slippage=0.0,
            max_first_leg_price=0.92,
        )
        blocked = evaluate_inventory_tick(
            snap,
            now=time.time(),
            hedge_deadline_at=0,
            first_leg_side="",
            yes_exe=(0.78, 5.0),
            no_exe=(0.25, 5.0),
            cfg=cfg,
            seconds_to_close=300.0,
        )
        self.assertEqual(blocked.action, ArbitrageAction.NONE)
        allowed = evaluate_inventory_tick(
            snap,
            now=time.time(),
            hedge_deadline_at=0,
            first_leg_side="",
            yes_exe=(0.78, 5.0),
            no_exe=(0.25, 5.0),
            cfg=cfg,
            seconds_to_close=60.0,
        )
        self.assertEqual(allowed.action, ArbitrageAction.REBALANCE)
        self.assertEqual(allowed.side, "YES")

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
