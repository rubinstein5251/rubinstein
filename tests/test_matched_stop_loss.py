"""Matched-pair stop-loss scope and inventory-aware protection tests."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils.momentum_inventory import PositionInventory
from utils.momentum_risk import stop_loss_price
from utils.matched_stop_loss import compute_stop_loss_scope


class StopLossScopeTests(unittest.TestCase):
    def test_fully_matched_suppresses_directional_stop(self):
        inv = PositionInventory()
        inv.record_buy("YES", 5.0, 0.87)
        inv.record_buy("NO", 5.0, 0.10)
        scope = compute_stop_loss_scope(
            inv, market_id="m1", primary_side="YES", fee_rate=0.0,
        )
        self.assertAlmostEqual(scope.economics.matched_quantity, 5.0)
        self.assertFalse(scope.directional_stop_eligible)
        self.assertEqual(scope.suppress_reason, "matched_inventory_protected")
        self.assertEqual(scope.stop_loss_quantity, 0.0)

    def test_partial_match_only_unmatched_eligible(self):
        inv = PositionInventory()
        inv.record_buy("YES", 10.0, 0.70)
        inv.record_buy("NO", 5.0, 0.25)
        scope = compute_stop_loss_scope(
            inv, market_id="m1", primary_side="YES", fee_rate=0.0,
        )
        self.assertAlmostEqual(scope.economics.matched_quantity, 5.0)
        self.assertAlmostEqual(scope.stop_loss_quantity, 5.0)
        self.assertTrue(scope.directional_stop_eligible)

    def test_no_opposite_side_full_unmatched_eligible(self):
        inv = PositionInventory()
        inv.record_buy("YES", 5.0, 0.70)
        scope = compute_stop_loss_scope(
            inv, market_id="m1", primary_side="YES", fee_rate=0.0,
        )
        self.assertTrue(scope.directional_stop_eligible)
        self.assertAlmostEqual(scope.stop_loss_quantity, 5.0)

    def test_restart_scope_from_inventory_snapshot(self):
        inv = PositionInventory()
        inv.record_buy("YES", 5.0, 0.87)
        inv.record_buy("NO", 5.0, 0.10)
        snap = inv.snapshot()
        inv2 = PositionInventory()
        inv2.record_buy("YES", snap["YES"]["shares"], snap["YES"]["avg_cost"])
        inv2.record_buy("NO", snap["NO"]["shares"], snap["NO"]["avg_cost"])
        a = compute_stop_loss_scope(inv, market_id="m1", primary_side="YES")
        b = compute_stop_loss_scope(inv2, market_id="m1", primary_side="YES")
        self.assertEqual(a.suppress_reason, b.suppress_reason)
        self.assertAlmostEqual(a.economics.matched_quantity, b.economics.matched_quantity)


class MatchedStopLossExecutionTests(unittest.IsolatedAsyncioTestCase):
    def _worker_matched(self, *, bid=0.40):
        inv = PositionInventory()
        inv.record_buy("YES", 5.0, 0.87)
        inv.record_buy("NO", 5.0, 0.10)
        cfg = SimpleNamespace(
            key="btc:5m",
            estimated_fee_rate=0.0,
            stop_loss_pct=0.20,
            stop_loss_exit_max_retries=1,
            stop_loss_exit_retry_ms=10,
        )
        worker = SimpleNamespace(
            worker_config=cfg,
            inventory=inv,
            dashboard={"bought_side": "YES"},
            _stop_loss_exit_in_flight=False,
            _stop_loss_lock=asyncio.Lock(),
            _order_lock=asyncio.Lock(),
            _exit_submit_guard=set(),
        )
        worker.canonical_market_id = lambda: "m1"
        worker.best_bid = lambda s: bid
        worker.record_fill_idempotent = lambda *a, **k: True
        worker.log_trade = lambda *a, **k: None
        return worker

    async def test_matched_inventory_still_directional_stop_on_primary(self):
        from bot import OrderState
        from strategies.inventory_accumulation_strategy import MomentumDelayedAdjustmentStrategy

        w = self._worker_matched(bid=0.40)
        w.order_state = OrderState.IDLE
        strat = MomentumDelayedAdjustmentStrategy()
        with patch(
            "strategies.inventory_accumulation_strategy.stopped_out_store.mark_stopped_out",
        ), patch(
            "strategies.inventory_accumulation_strategy.fok_sell_with_retries",
            new_callable=AsyncMock,
            return_value=(5.0, 0.40, "dry-run"),
        ) as mock_sell:
            await strat._handle_stop_loss(w, "m1")
            mock_sell.assert_called_once()
            self.assertAlmostEqual(mock_sell.call_args[0][2], 5.0)

    async def test_dual_side_sells_full_primary_on_stop(self):
        from bot import OrderState
        from strategies.inventory_accumulation_strategy import MomentumDelayedAdjustmentStrategy

        inv = PositionInventory()
        inv.record_buy("YES", 10.0, 0.70)
        inv.record_buy("NO", 5.0, 0.25)
        cfg = SimpleNamespace(
            key="btc:5m",
            estimated_fee_rate=0.0,
            stop_loss_pct=0.20,
            stop_loss_exit_max_retries=1,
            stop_loss_exit_retry_ms=10,
        )
        w = SimpleNamespace(
            worker_config=cfg,
            inventory=inv,
            dashboard={"bought_side": "YES"},
            _stop_loss_exit_in_flight=False,
            _stop_loss_lock=asyncio.Lock(),
            _order_lock=asyncio.Lock(),
            _exit_submit_guard=set(),
        )
        w.canonical_market_id = lambda: "m1"
        w.best_bid = lambda s: 0.40
        w.order_state = OrderState.IDLE
        w.record_fill_idempotent = lambda *a, **k: True
        w.log_trade = lambda *a, **k: None
        strat = MomentumDelayedAdjustmentStrategy()
        with patch(
            "strategies.inventory_accumulation_strategy.stopped_out_store.mark_stopped_out",
        ), patch(
            "strategies.inventory_accumulation_strategy.fok_sell_with_retries",
            new_callable=AsyncMock,
            return_value=(5.0, 0.40, "dry-run"),
        ) as mock_sell:
            await strat._handle_stop_loss(w, "m1")
            mock_sell.assert_called_once()
            self.assertAlmostEqual(mock_sell.call_args[0][2], 10.0)
        self.assertAlmostEqual(w.inventory.yes_shares, 5.0)
        self.assertAlmostEqual(w.inventory.no_shares, 5.0)


if __name__ == "__main__":
    unittest.main()
