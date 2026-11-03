"""Stop-loss and exit accounting: fills, partials, idempotency."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from utils.momentum_inventory import PositionInventory
from utils.momentum_risk import stop_loss_price


class StopLossAccountingTests(unittest.IsolatedAsyncioTestCase):
    def _strategy(self):
        from strategies.inventory_accumulation_strategy import MomentumDelayedAdjustmentStrategy

        return MomentumDelayedAdjustmentStrategy()

    def _worker(self, *, shares=5.0, entry=0.70, bid=0.44):
        inv = PositionInventory()
        inv.record_buy("YES", shares, entry)

        cfg = SimpleNamespace(
            key="btc:5m",
            estimated_fee_rate=0.0,
            take_profit_pct=0.10,
            stop_loss_pct=0.20,
            stop_loss_exit_max_retries=1,
            stop_loss_exit_retry_ms=10,
        )
        worker = SimpleNamespace(
            worker_config=cfg,
            inventory=inv,
            dashboard={"bought_side": "YES"},
            position_side="YES",
            position_size=shares,
            entry_price=entry,
            entry_count=1,
            asset_type="btc",
            window_slug="5m",
            _stop_loss_exit_in_flight=False,
            _stop_loss_lock=asyncio.Lock(),
            _order_lock=asyncio.Lock(),
            _exit_submit_guard=set(),
        )
        worker.canonical_market_id = lambda: "m1"
        worker.best_bid = lambda s: bid
        worker.record_fill_idempotent = lambda oid, side, qty, px: True
        worker.log_trade = lambda *a, **k: None
        return worker

    async def test_trigger_without_fill_keeps_position(self):
        from bot import OrderState

        strat = self._strategy()
        w = self._worker(bid=stop_loss_price(0.70, 0.20) - 0.01)
        w.order_state = OrderState.IDLE
        with patch(
            "strategies.inventory_accumulation_strategy.stopped_out_store.mark_stopped_out",
        ), patch(
            "strategies.inventory_accumulation_strategy.fok_sell_with_retries",
            new_callable=AsyncMock,
            return_value=(0.0, 0.0, None),
        ):
            await strat._handle_stop_loss(w, "m1")
        self.assertGreater(w.inventory.shares("YES"), 0)
        self.assertEqual(w.inventory.realized_pnl, 0.0)

    async def test_confirmed_stop_loss_updates_cumulative_via_notify(self):
        from utils.canonical_ledger import CanonicalLedger
        import os
        import tempfile

        path = os.path.join(tempfile.gettempdir(), f"sl_cum_{id(self)}.json")
        ledger = CanonicalLedger(path=path, snapshot_path=path + ".pnl")
        strat = self._strategy()
        entry = 0.70
        exit_px = 0.45
        w = self._worker(entry=entry, bid=exit_px)

        def notify(**kw):
            ledger.record_fill(
                worker_key="btc:5m",
                market_id="m1",
                order_id=kw.get("order_id"),
                side=kw["side"],
                action=kw["action"],
                price=kw["price"],
                quantity=kw["quantity"],
                realized_pnl_delta=float(kw.get("realized_pnl_delta") or 0),
                count_win_loss=bool(kw.get("count_win_loss")),
            )

        w.notify_confirmed_fill = notify
        w.cumulative_pnl = 0.0
        from bot import OrderState

        w.order_state = OrderState.IDLE
        with patch(
            "strategies.inventory_accumulation_strategy.stopped_out_store.mark_stopped_out",
        ), patch(
            "strategies.inventory_accumulation_strategy.fok_sell_with_retries",
            new_callable=AsyncMock,
            return_value=(5.0, exit_px, "dry-run"),
        ):
            await strat._handle_stop_loss(w, "m1")
        self.assertAlmostEqual(ledger.worker_summary("btc:5m").realized_pnl, (exit_px - entry) * 5.0)

    async def test_confirmed_stop_loss_realizes_loss(self):
        from bot import OrderState

        strat = self._strategy()
        entry = 0.70
        exit_px = 0.45
        w = self._worker(entry=entry, bid=exit_px)
        w.order_state = OrderState.IDLE
        with patch(
            "strategies.inventory_accumulation_strategy.stopped_out_store.mark_stopped_out",
        ), patch(
            "strategies.inventory_accumulation_strategy.fok_sell_with_retries",
            new_callable=AsyncMock,
            return_value=(5.0, exit_px, "dry-run"),
        ):
            await strat._handle_stop_loss(w, "m1")
        self.assertAlmostEqual(w.inventory.realized_pnl, (exit_px - entry) * 5.0)
        self.assertEqual(w.dashboard.get("exit_reason"), "STOP_LOSS")

    async def test_failed_fok_does_not_realize_pnl(self):
        strat = self._strategy()
        w = self._worker()
        await strat._record_exit_sell(
            w, "YES", 0.0, 0.45, None, exit_reason="stop_loss", entry=0.70, target_price=0.45,
        )
        self.assertGreater(w.inventory.shares("YES"), 0)
        self.assertEqual(w.inventory.realized_pnl, 0.0)

    async def test_partial_sell_keeps_remainder(self):
        strat = self._strategy()
        w = self._worker(shares=10.0)
        await strat._record_exit_sell(
            w, "YES", 5.0, 0.45, "oid1", exit_reason="stop_loss", entry=0.70, target_price=0.45,
        )
        self.assertAlmostEqual(w.inventory.shares("YES"), 5.0)
        self.assertAlmostEqual(w.inventory.realized_pnl, (0.45 - 0.70) * 5.0)
        self.assertEqual(w.position_size, 5.0)

    async def test_duplicate_fill_not_double_counted(self):
        strat = self._strategy()
        w = self._worker()
        seen = set()

        def idempotent(oid, side, qty, px):
            if oid in seen:
                return False
            seen.add(oid)
            return True

        w.record_fill_idempotent = idempotent
        await strat._record_exit_sell(
            w, "YES", 5.0, 0.45, "o1", exit_reason="stop_loss", entry=0.70, target_price=0.45,
        )
        pnl_after_first = w.inventory.realized_pnl
        await strat._record_exit_sell(
            w, "YES", 5.0, 0.45, "o1", exit_reason="stop_loss", entry=0.70, target_price=0.45,
        )
        self.assertEqual(w.inventory.realized_pnl, pnl_after_first)


if __name__ == "__main__":
    unittest.main()
