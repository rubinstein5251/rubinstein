"""Momentum-first inventory engine (simplified 65¢ entry)."""

from __future__ import annotations

import time
import unittest

from utils.inventory_cost import InventorySnapshot
from utils.momentum_inventory_engine import (
    EntryStatus,
    InventoryBuyAction,
    InventoryPhase,
    MomentumInventoryConfig,
    evaluate_momentum_inventory_tick,
)
from utils.order_book_momentum import evaluate_order_book_momentum


class OrderBookMomentumTests(unittest.TestCase):
    def test_yes_upward_momentum(self):
        now = 1000.0
        yes = [(now - 6, 0.50), (now, 0.55)]
        no = [(now - 6, 0.45), (now, 0.44)]
        ev = evaluate_order_book_momentum(yes, no, now)
        self.assertEqual(ev.momentum_side, "YES")

    def test_no_momentum_when_flat(self):
        now = 1000.0
        yes = [(now - 6, 0.50), (now, 0.50)]
        no = [(now - 6, 0.40), (now, 0.40)]
        ev = evaluate_order_book_momentum(yes, no, now)
        self.assertIsNone(ev.momentum_side)


class MomentumInventoryEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = MomentumInventoryConfig(
            target_shares_per_side=10.0,
            min_order_size=5.0,
            max_purchase_size=10.0,
            momentum_entry_min_price=0.65,
            max_combined_entry_cost=0.97,
            max_combined_cost=0.97,
            hedge_pending_timeout_seconds=60.0,
        )
        self.now = 2000.0

    def _rising_yes_hist(self):
        return [
            (self.now - 6, 0.60),
            (self.now, 0.66),
        ]

    def test_waits_below_65c(self):
        snap = InventorySnapshot(0, 0, 0, 0)
        yes = [(self.now - 6, 0.60), (self.now, 0.64)]
        no = [(self.now - 6, 0.35), (self.now, 0.34)]
        d = evaluate_momentum_inventory_tick(
            snap,
            now=self.now,
            yes_history=yes,
            no_history=no,
            yes_exe=(0.64, 10.0),
            no_exe=(0.34, 10.0),
            cfg=self.cfg,
        )
        self.assertEqual(d.action, InventoryBuyAction.NONE)
        self.assertEqual(d.reason, "momentum_side_below_min_entry")
        self.assertEqual(d.entry_status, EntryStatus.WAITING.value)

    def test_eligible_at_65c_or_above(self):
        snap = InventorySnapshot(0, 0, 0, 0)
        yes = self._rising_yes_hist()
        no = [(self.now - 6, 0.35), (self.now, 0.34)]
        d = evaluate_momentum_inventory_tick(
            snap,
            now=self.now,
            yes_history=yes,
            no_history=no,
            yes_exe=(0.66, 10.0),
            no_exe=(0.30, 10.0),
            cfg=self.cfg,
        )
        self.assertEqual(d.action, InventoryBuyAction.MOMENTUM_ENTRY)
        self.assertEqual(d.side, "YES")
        self.assertEqual(d.entry_status, EntryStatus.ELIGIBLE.value)

    def test_no_emergency_hedge_after_timeout(self):
        snap = InventorySnapshot(10, 0, 5.5, 0)
        now = 3000.0
        d = evaluate_momentum_inventory_tick(
            snap,
            now=now,
            yes_history=[(now, 0.55)],
            no_history=[(now, 0.52)],
            yes_exe=(0.55, 10.0),
            no_exe=(0.52, 10.0),
            cfg=self.cfg,
            first_fill_at=now - 120.0,
        )
        self.assertEqual(d.action, InventoryBuyAction.NONE)
        self.assertTrue(d.hedge_timeout_reassess)
        self.assertEqual(d.phase, InventoryPhase.STATE_2_HEDGE_PENDING)

    def test_target_reached(self):
        snap = InventorySnapshot(10, 10, 5.5, 3.5)
        d = evaluate_momentum_inventory_tick(
            snap,
            now=time.time(),
            yes_history=[],
            no_history=[],
            yes_exe=(0.55, 10.0),
            no_exe=(0.35, 10.0),
            cfg=self.cfg,
            first_fill_at=100.0,
        )
        self.assertEqual(d.phase, InventoryPhase.TARGET_REACHED)


if __name__ == "__main__":
    unittest.main()
