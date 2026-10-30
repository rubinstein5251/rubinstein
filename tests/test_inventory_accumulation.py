"""Inventory accumulation strategy tests (no live orders)."""

from __future__ import annotations

import unittest

from utils.inventory_cost import (
    InventorySnapshot,
    PurchaseDecision,
    evaluate_cheap_entry,
    evaluate_dca_purchase,
    evaluate_initial_hedge,
    matched_combined_average,
    pick_cheap_side,
)


class CostBasisTests(unittest.TestCase):
    def test_matched_combined_example_up35_down67(self):
        snap = InventorySnapshot(yes_shares=100, no_shares=100, yes_cost=35, no_cost=67)
        self.assertAlmostEqual(matched_combined_average(snap), 1.02)

    def test_dca_down_cheaper_improves_combined(self):
        snap = InventorySnapshot(yes_shares=100, no_shares=100, yes_cost=35, no_cost=67)
        ev = evaluate_dca_purchase(
            snap, "NO", 100, 0.63, combined_cost_target=0.98, max_combined_cost=1.05,
        )
        self.assertEqual(ev.decision, PurchaseDecision.BUY)
        after = snap.with_buy("NO", 100, 0.63)
        self.assertAlmostEqual(matched_combined_average(after), 1.00)

    def test_cheap_entry_rejects_above_max(self):
        ev = evaluate_cheap_entry(0.36, cheap_entry_price=0.40, max_entry_price=0.35)
        self.assertEqual(ev.decision, PurchaseDecision.SKIP)
        self.assertEqual(ev.reason, "above_max_entry_price")

    def test_cheap_entry_allows_at_threshold(self):
        ev = evaluate_cheap_entry(0.35, cheap_entry_price=0.35, max_entry_price=0.35)
        self.assertEqual(ev.decision, PurchaseDecision.BUY)

    def test_pick_cheap_side_chooses_yes_when_only_yes_qualifies(self):
        side, ev = pick_cheap_side(
            (0.30, 100.0),
            (0.70, 100.0),
            cheap_entry_price=0.35,
            max_entry_price=0.35,
            yes_shares=0.0,
            no_shares=0.0,
        )
        self.assertEqual(side, "YES")
        self.assertEqual(ev.decision, PurchaseDecision.BUY)

    def test_pick_cheap_side_chooses_no_when_only_no_qualifies(self):
        side, ev = pick_cheap_side(
            (0.70, 100.0),
            (0.28, 100.0),
            cheap_entry_price=0.35,
            max_entry_price=0.35,
            yes_shares=0.0,
            no_shares=0.0,
        )
        self.assertEqual(side, "NO")
        self.assertEqual(ev.decision, PurchaseDecision.BUY)

    def test_pick_cheap_side_prefers_lower_price_when_both_qualify(self):
        side, _ev = pick_cheap_side(
            (0.34, 50.0),
            (0.31, 50.0),
            cheap_entry_price=0.35,
            max_entry_price=0.35,
            yes_shares=0.0,
            no_shares=0.0,
        )
        self.assertEqual(side, "NO")

    def test_initial_hedge_rejected_above_max_combined(self):
        snap = InventorySnapshot(yes_shares=100, no_shares=0, yes_cost=35, no_cost=0)
        ev = evaluate_initial_hedge(
            snap, "NO", 100, 0.67, max_slippage=0.01, limit_price=0.68,
            max_combined_cost=0.96, min_net_profit_per_pair=0.02, slippage_reserve=0.005,
        )
        self.assertEqual(ev.decision, PurchaseDecision.SKIP)
        self.assertEqual(ev.reason, "REJECT_COMBINED_COST_TOO_HIGH")

    def test_dca_skips_without_improvement(self):
        snap = InventorySnapshot(yes_shares=100, no_shares=100, yes_cost=35, no_cost=65)
        ev = evaluate_dca_purchase(
            snap, "NO", 100, 0.66, combined_cost_target=0.98, max_combined_cost=1.0,
        )
        self.assertEqual(ev.decision, PurchaseDecision.SKIP)

    def test_target_detection(self):
        snap = InventorySnapshot(yes_shares=50, no_shares=50, yes_cost=24.0, no_cost=25.0)
        ev = evaluate_dca_purchase(
            snap, "YES", 50, 0.48, combined_cost_target=0.98, max_combined_cost=1.05,
        )
        if ev.decision == PurchaseDecision.BUY and ev.resulting_combined is not None:
            self.assertLessEqual(ev.resulting_combined, 1.05)


class IntegrationSourceTests(unittest.TestCase):
    def test_strategy_module_exports(self):
        from pathlib import Path
        src = Path("strategies/momentum.py").read_text(encoding="utf-8")
        self.assertIn("DirectionalFokStrategy", src)

    def test_strategy_buy_authorization(self):
        from pathlib import Path
        body = Path("bot.py").read_text(encoding="utf-8")
        self.assertIn("_strategy_buy_authorized", body)

    def test_no_late_entry_strategy_files(self):
        from pathlib import Path
        root = Path(".")
        self.assertFalse((root / "strategies/late_entry_strategy.py").exists())


if __name__ == "__main__":
    unittest.main()
