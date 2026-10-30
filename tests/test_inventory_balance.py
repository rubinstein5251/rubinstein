"""Inventory balance enforcement tests."""

from __future__ import annotations

import unittest

from utils.inventory_balance import (
    BuyApproval,
    EffectiveInventory,
    max_qty_without_increasing_imbalance,
    plan_paired_buy,
    plan_single_leg_buy,
)
from utils.inventory_cost import (
    InventorySnapshot,
    PurchaseDecision,
    evaluate_dca_purchase,
    evaluate_paired_dca_purchase,
)


class InventoryBalanceTests(unittest.TestCase):
    def _snap_yes_heavy(self) -> InventorySnapshot:
        return InventorySnapshot(yes_shares=20, no_shares=5, yes_cost=16.0, no_cost=0.85)

    def test_rejects_yes_when_yes_heavy_strict(self):
        snap = self._snap_yes_heavy()
        eff = EffectiveInventory(yes_confirmed=20, no_confirmed=5)
        plan = plan_single_leg_buy(
            snap=snap,
            eff=eff,
            side="YES",
            requested_qty=5,
            min_order_size=5,
            max_share_imbalance=0,
            max_unmatched_shares=0,
            balance_inventory_before_buy=True,
            allow_unmatched_accumulation=False,
            purpose="dca_accumulation",
        )
        self.assertEqual(plan.approval, BuyApproval.REJECTED)
        self.assertIn("imbalance", plan.reason.lower())

    def test_allows_no_rebalance_when_yes_heavy(self):
        snap = self._snap_yes_heavy()
        eff = EffectiveInventory(yes_confirmed=20, no_confirmed=5)
        plan = plan_single_leg_buy(
            snap=snap,
            eff=eff,
            side="NO",
            requested_qty=5,
            min_order_size=5,
            max_share_imbalance=0,
            max_unmatched_shares=15,
            balance_inventory_before_buy=True,
            allow_unmatched_accumulation=False,
            purpose="dca_rebalance",
        )
        self.assertEqual(plan.approval, BuyApproval.APPROVED)
        self.assertAlmostEqual(plan.approved_qty, 5.0)

    def test_balanced_inventory_rejects_single_yes(self):
        snap = InventorySnapshot(yes_shares=10, no_shares=10, yes_cost=7.5, no_cost=2.5)
        eff = EffectiveInventory(yes_confirmed=10, no_confirmed=10)
        plan = plan_single_leg_buy(
            snap=snap,
            eff=eff,
            side="YES",
            requested_qty=5,
            min_order_size=5,
            max_share_imbalance=0,
            max_unmatched_shares=0,
            balance_inventory_before_buy=True,
            allow_unmatched_accumulation=False,
            purpose="dca_accumulation",
        )
        self.assertEqual(plan.approval, BuyApproval.REJECTED)
        self.assertIn("paired", plan.reason.lower())

    def test_paired_plan_approved_when_balanced(self):
        snap = InventorySnapshot(yes_shares=10, no_shares=10, yes_cost=7.5, no_cost=2.5)
        eff = EffectiveInventory(yes_confirmed=10, no_confirmed=10)
        paired = plan_paired_buy(
            snap=snap,
            eff=eff,
            requested_qty=5,
            min_order_size=5,
            max_share_imbalance=0,
            balance_inventory_before_buy=True,
        )
        self.assertEqual(paired.approval, BuyApproval.APPROVED)
        self.assertAlmostEqual(paired.qty, 5.0)

    def test_pending_orders_increase_effective_imbalance(self):
        eff = EffectiveInventory(yes_confirmed=10, no_confirmed=10, pending_yes=5, pending_no=0)
        self.assertAlmostEqual(eff.share_imbalance_effective, 5.0)
        plan = plan_single_leg_buy(
            snap=InventorySnapshot(yes_shares=10, no_shares=10, yes_cost=7.5, no_cost=2.5),
            eff=eff,
            side="YES",
            requested_qty=5,
            min_order_size=5,
            max_share_imbalance=0,
            max_unmatched_shares=0,
            balance_inventory_before_buy=True,
            allow_unmatched_accumulation=False,
            purpose="dca_accumulation",
        )
        self.assertEqual(plan.approval, BuyApproval.REJECTED)

    def test_max_qty_without_increasing_from_balanced_is_zero(self):
        eff = EffectiveInventory(yes_confirmed=10, no_confirmed=10)
        self.assertAlmostEqual(max_qty_without_increasing_imbalance(eff, "YES", 5), 0.0)

    def test_repeated_yes_proposals_cannot_worsen_20_vs_5(self):
        snap = self._snap_yes_heavy()
        eff = EffectiveInventory(yes_confirmed=20, no_confirmed=5)
        for _ in range(4):
            plan = plan_single_leg_buy(
                snap=snap,
                eff=eff,
                side="YES",
                requested_qty=5,
                min_order_size=5,
                max_share_imbalance=0,
                max_unmatched_shares=0,
                balance_inventory_before_buy=True,
                allow_unmatched_accumulation=False,
                purpose="dca_accumulation",
            )
            self.assertEqual(plan.approval, BuyApproval.REJECTED)

    def test_paired_dca_cost_evaluation(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=5, yes_cost=1.5, no_cost=3.35)
        ev = evaluate_paired_dca_purchase(
            snap, 5, 0.70, 0.28, combined_cost_target=0.98, max_combined_cost=1.05,
        )
        self.assertIn(ev.decision, (PurchaseDecision.BUY, PurchaseDecision.SKIP))

    def test_nonzero_imbalance_tolerance(self):
        snap = InventorySnapshot(yes_shares=10, no_shares=8, yes_cost=7.0, no_cost=2.0)
        eff = EffectiveInventory(yes_confirmed=10, no_confirmed=8)
        plan = plan_single_leg_buy(
            snap=snap,
            eff=eff,
            side="NO",
            requested_qty=1,
            min_order_size=1,
            max_share_imbalance=2,
            max_unmatched_shares=2,
            balance_inventory_before_buy=True,
            allow_unmatched_accumulation=True,
            purpose="dca_rebalance",
        )
        self.assertEqual(plan.approval, BuyApproval.APPROVED)

    def test_initial_hedge_no_for_yes5_no0(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=0, yes_cost=1.5, no_cost=0)
        eff = EffectiveInventory(yes_confirmed=5, no_confirmed=0)
        plan = plan_single_leg_buy(
            snap=snap,
            eff=eff,
            side="NO",
            requested_qty=5,
            min_order_size=5,
            max_share_imbalance=0,
            max_unmatched_shares=0,
            balance_inventory_before_buy=True,
            allow_unmatched_accumulation=False,
            purpose="initial_hedge",
        )
        self.assertEqual(plan.approval, BuyApproval.APPROVED)
        self.assertAlmostEqual(plan.approved_qty, 5.0)

    def test_initial_hedge_ignores_pending_that_would_fake_balance(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=0, yes_cost=1.5, no_cost=0)
        eff = EffectiveInventory(yes_confirmed=5, no_confirmed=0, pending_no=5)
        plan = plan_single_leg_buy(
            snap=snap,
            eff=eff,
            side="NO",
            requested_qty=5,
            min_order_size=5,
            max_share_imbalance=0,
            max_unmatched_shares=0,
            balance_inventory_before_buy=True,
            allow_unmatched_accumulation=False,
            purpose="initial_hedge",
        )
        self.assertEqual(plan.approval, BuyApproval.APPROVED)
        self.assertAlmostEqual(plan.approved_qty, 5.0)

    def test_first_leg_cheap_entry_from_flat(self):
        snap = InventorySnapshot(yes_shares=0, no_shares=0, yes_cost=0, no_cost=0)
        eff = EffectiveInventory(0, 0)
        plan = plan_single_leg_buy(
            snap=snap,
            eff=eff,
            side="NO",
            requested_qty=5,
            min_order_size=5,
            max_share_imbalance=0,
            max_unmatched_shares=0,
            balance_inventory_before_buy=True,
            allow_unmatched_accumulation=False,
            purpose="cheap_entry",
            allow_first_leg_entry=True,
        )
        self.assertEqual(plan.approval, BuyApproval.APPROVED)
        self.assertAlmostEqual(plan.approved_qty, 5.0)


if __name__ == "__main__":
    unittest.main()
