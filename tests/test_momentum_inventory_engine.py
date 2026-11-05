"""Momentum-first inventory engine (65¢ entry, 93¢ pair target, 5/side)."""

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
    max_acceptable_opposite_price,
)
from utils.order_book_momentum import evaluate_order_book_momentum


class OrderBookMomentumTests(unittest.TestCase):
    def test_yes_upward_momentum(self):
        now = 1000.0
        yes = [(now - 6, 0.50), (now, 0.68)]
        no = [(now - 6, 0.45), (now, 0.44)]
        ev = evaluate_order_book_momentum(yes, no, now)
        self.assertEqual(ev.momentum_side, "YES")

    def test_no_upward_momentum(self):
        now = 1000.0
        yes = [(now - 6, 0.24), (now, 0.23)]
        no = [(now - 6, 0.65), (now, 0.70)]
        ev = evaluate_order_book_momentum(yes, no, now)
        self.assertEqual(ev.momentum_side, "NO")

    def test_no_momentum_when_flat(self):
        now = 1000.0
        yes = [(now - 6, 0.50), (now, 0.50)]
        no = [(now - 6, 0.40), (now, 0.40)]
        ev = evaluate_order_book_momentum(yes, no, now)
        self.assertIsNone(ev.momentum_side)


class MomentumInventoryEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = MomentumInventoryConfig(
            target_shares_per_side=5.0,
            min_order_size=5.0,
            max_purchase_size=5.0,
            momentum_entry_min_price=0.65,
            target_combined_pair_cost=0.93,
            hedge_pending_timeout_seconds=60.0,
        )
        self.now = 2000.0

    def test_max_opposite_price_after_68_up(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=0, yes_cost=3.40, no_cost=0)
        self.assertAlmostEqual(max_acceptable_opposite_price(snap, "YES", 0.93), 0.25)

    def test_waits_below_65c(self):
        snap = InventorySnapshot(0, 0, 0, 0)
        yes = [(self.now - 6, 0.60), (self.now, 0.64)]
        no = [(self.now - 6, 0.35), (self.now, 0.34)]
        d = evaluate_momentum_inventory_tick(
            snap,
            now=self.now,
            yes_history=yes,
            no_history=no,
            yes_exe=(0.64, 5.0),
            no_exe=(0.34, 5.0),
            cfg=self.cfg,
        )
        self.assertEqual(d.action, InventoryBuyAction.NONE)
        self.assertEqual(d.reason, "momentum_side_below_min_entry")

    def test_up_momentum_entry_at_68c(self):
        snap = InventorySnapshot(0, 0, 0, 0)
        yes = [(self.now - 6, 0.60), (self.now, 0.68)]
        no = [(self.now - 6, 0.30), (self.now, 0.25)]
        d = evaluate_momentum_inventory_tick(
            snap,
            now=self.now,
            yes_history=yes,
            no_history=no,
            yes_exe=(0.68, 5.0),
            no_exe=(0.25, 5.0),
            cfg=self.cfg,
        )
        self.assertEqual(d.action, InventoryBuyAction.MOMENTUM_ENTRY)
        self.assertEqual(d.side, "YES")
        self.assertEqual(d.entry_status, EntryStatus.ELIGIBLE.value)

    def test_down_momentum_entry_at_70c(self):
        snap = InventorySnapshot(0, 0, 0, 0)
        yes = [(self.now - 6, 0.24), (self.now, 0.23)]
        no = [(self.now - 6, 0.65), (self.now, 0.70)]
        d = evaluate_momentum_inventory_tick(
            snap,
            now=self.now,
            yes_history=yes,
            no_history=no,
            yes_exe=(0.23, 5.0),
            no_exe=(0.70, 5.0),
            cfg=self.cfg,
        )
        self.assertEqual(d.action, InventoryBuyAction.MOMENTUM_ENTRY)
        self.assertEqual(d.side, "NO")

    def test_opposite_buy_at_25_after_up_68(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=0, yes_cost=3.40, no_cost=0)
        now = 3000.0
        d = evaluate_momentum_inventory_tick(
            snap,
            now=now,
            yes_history=[(now, 0.68)],
            no_history=[(now, 0.25)],
            yes_exe=(0.68, 5.0),
            no_exe=(0.25, 5.0),
            cfg=self.cfg,
            first_fill_at=now - 10.0,
        )
        self.assertEqual(d.action, InventoryBuyAction.ACCUMULATE_OPPOSITE)
        self.assertEqual(d.side, "NO")
        self.assertAlmostEqual(d.projected_combined or 0, 0.93, places=2)

    def test_opposite_rejected_above_target(self):
        snap = InventorySnapshot(yes_shares=5, no_shares=0, yes_cost=3.40, no_cost=0)
        now = 3000.0
        d = evaluate_momentum_inventory_tick(
            snap,
            now=now,
            yes_history=[(now, 0.68)],
            no_history=[(now, 0.30)],
            yes_exe=(0.68, 5.0),
            no_exe=(0.30, 5.0),
            cfg=self.cfg,
            first_fill_at=now - 10.0,
        )
        self.assertEqual(d.action, InventoryBuyAction.NONE)

    def test_no_emergency_hedge_after_timeout(self):
        snap = InventorySnapshot(5, 0, 3.40, 0)
        now = 3000.0
        d = evaluate_momentum_inventory_tick(
            snap,
            now=now,
            yes_history=[(now, 0.68)],
            no_history=[(now, 0.52)],
            yes_exe=(0.68, 5.0),
            no_exe=(0.52, 5.0),
            cfg=self.cfg,
            first_fill_at=now - 120.0,
        )
        self.assertEqual(d.action, InventoryBuyAction.NONE)
        self.assertTrue(d.hedge_timeout_reassess)

    def test_target_reached_at_5_and_5(self):
        snap = InventorySnapshot(5, 5, 3.40, 1.25)
        d = evaluate_momentum_inventory_tick(
            snap,
            now=time.time(),
            yes_history=[],
            no_history=[],
            yes_exe=(0.68, 5.0),
            no_exe=(0.25, 5.0),
            cfg=self.cfg,
            first_fill_at=100.0,
        )
        self.assertEqual(d.phase, InventoryPhase.TARGET_REACHED)


if __name__ == "__main__":
    unittest.main()
