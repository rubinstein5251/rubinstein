"""Momentum-first inventory engine (paper / unit tests)."""

from __future__ import annotations

import time
import unittest

from utils.inventory_cost import InventorySnapshot
from utils.momentum_inventory_engine import (
    InventoryBuyAction,
    InventoryPhase,
    MomentumInventoryConfig,
    StabilizationPending,
    evaluate_momentum_inventory_tick,
)
from utils.order_book_momentum import evaluate_order_book_momentum


def _hist(base: float, deltas: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Build (ts, price) series from (seconds_ago, price) pairs."""
    now = 1000.0
    points = [(now - sec, px) for sec, px in sorted(deltas, key=lambda x: -x[0])]
    points.sort(key=lambda x: x[0])
    return points


class OrderBookMomentumTests(unittest.TestCase):
    def test_yes_momentum_confirmed(self):
        now = 1000.0
        yes = [
            (now - 35, 0.50),
            (now - 30, 0.50),
            (now - 15, 0.52),
            (now - 5, 0.53),
            (now, 0.55),
        ]
        no = [
            (now - 35, 0.48),
            (now - 30, 0.48),
            (now - 15, 0.46),
            (now - 5, 0.44),
            (now, 0.42),
        ]
        ev = evaluate_order_book_momentum(yes, no, now)
        self.assertEqual(ev.confirmed_side, "YES")
        self.assertFalse(ev.conflict)

    def test_conflict_when_both_rise(self):
        now = 1000.0
        yes = [(now - 30, 0.50), (now - 15, 0.52), (now, 0.55)]
        no = [(now - 30, 0.40), (now - 15, 0.42), (now, 0.44)]
        ev = evaluate_order_book_momentum(yes, no, now)
        self.assertIsNone(ev.confirmed_side)
        self.assertTrue(ev.conflict)


class MomentumInventoryEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = MomentumInventoryConfig(
            target_shares_per_side=10.0,
            min_order_size=5.0,
            max_purchase_size=10.0,
            stabilization_period_seconds=2.0,
            max_combined_entry_cost=0.97,
            max_combined_cost=0.97,
            hedge_pending_timeout_seconds=60.0,
        )
        self.now = 2000.0

    def test_entry_requires_stabilization(self):
        snap = InventorySnapshot(0, 0, 0, 0)
        yes = [
            (self.now - 30, 0.52),
            (self.now - 15, 0.53),
            (self.now - 5, 0.54),
            (self.now, 0.55),
        ]
        no = [
            (self.now - 30, 0.45),
            (self.now - 15, 0.44),
            (self.now - 5, 0.41),
            (self.now, 0.40),
        ]
        stab = StabilizationPending("YES", self.now - 0.5, evaluate_order_book_momentum(yes, no, self.now))
        d = evaluate_momentum_inventory_tick(
            snap,
            now=self.now,
            yes_history=yes,
            no_history=no,
            yes_exe=(0.55, 10.0),
            no_exe=(0.40, 10.0),
            cfg=self.cfg,
            stabilization=stab,
        )
        self.assertEqual(d.action, InventoryBuyAction.NONE)
        self.assertIn("stabilizing", d.reason)

    def test_momentum_entry_after_stabilization(self):
        snap = InventorySnapshot(0, 0, 0, 0)
        yes = [
            (self.now - 30, 0.52),
            (self.now - 15, 0.53),
            (self.now - 5, 0.54),
            (self.now, 0.55),
        ]
        no = [
            (self.now - 30, 0.45),
            (self.now - 15, 0.44),
            (self.now - 5, 0.41),
            (self.now, 0.40),
        ]
        mom = evaluate_order_book_momentum(yes, no, self.now)
        stab = StabilizationPending("YES", self.now - 3.0, mom)
        d = evaluate_momentum_inventory_tick(
            snap,
            now=self.now,
            yes_history=yes,
            no_history=no,
            yes_exe=(0.55, 10.0),
            no_exe=(0.40, 10.0),
            cfg=self.cfg,
            stabilization=stab,
        )
        self.assertEqual(d.action, InventoryBuyAction.MOMENTUM_ENTRY)
        self.assertEqual(d.side, "YES")

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
