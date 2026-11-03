"""Dashboard/API alignment with canonical ledger and stop-loss accounting."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils.canonical_ledger import CanonicalLedger
from utils.momentum_inventory import PositionInventory


class DashboardLedgerAccountingTests(unittest.IsolatedAsyncioTestCase):
    def _ledger(self) -> CanonicalLedger:
        path = os.path.join(tempfile.gettempdir(), f"dash_ledger_{id(self)}.json")
        return CanonicalLedger(path=path, snapshot_path=path + ".pnl")

    def _worker_with_notify(self, ledger: CanonicalLedger):
        inv = PositionInventory()
        inv.record_buy("YES", 5.0, 0.70)
        cfg = SimpleNamespace(
            key="btc:5m",
            estimated_fee_rate=0.0,
            stop_loss_pct=0.20,
            take_profit_pct=0.10,
            stop_loss_exit_max_retries=1,
            stop_loss_exit_retry_ms=10,
        )

        def notify(**kw):
            ledger.record_fill(
                worker_key=cfg.key,
                market_id="m1",
                order_id=kw.get("order_id"),
                side=kw["side"],
                action=kw["action"],
                price=kw["price"],
                quantity=kw["quantity"],
                realized_pnl_delta=float(kw.get("realized_pnl_delta") or 0),
                count_win_loss=bool(kw.get("count_win_loss")),
            )

        from bot import OrderState

        worker = SimpleNamespace(
            worker_config=cfg,
            inventory=inv,
            dashboard={"bought_side": "YES"},
            position_side="YES",
            position_size=5.0,
            entry_price=0.70,
            cumulative_pnl=0.0,
            wins=0,
            losses=0,
            trade_count=0,
            order_state=OrderState.IDLE,
            _stop_loss_exit_in_flight=False,
            _stop_loss_lock=asyncio.Lock(),
            _order_lock=asyncio.Lock(),
        )
        worker.canonical_market_id = lambda: "m1"
        worker.best_bid = lambda s: 0.45
        worker.record_fill_idempotent = lambda oid, side, qty, px: True
        worker.log_trade = lambda *a, **k: None
        worker.notify_confirmed_fill = notify
        worker.sync_accounting_dashboard = lambda: None
        return worker, ledger

    async def test_trigger_without_fill_no_ledger_loss(self):
        from strategies.inventory_accumulation_strategy import MomentumDelayedAdjustmentStrategy
        from utils.momentum_risk import stop_loss_price

        ledger = self._ledger()
        worker, _ = self._worker_with_notify(ledger)
        strat = MomentumDelayedAdjustmentStrategy()
        worker.best_bid = lambda s: stop_loss_price(0.70, 0.20) - 0.01
        with patch(
            "strategies.inventory_accumulation_strategy.fok_sell_with_retries",
            new_callable=AsyncMock,
            return_value=(0.0, 0.0, None),
        ):
            await strat._handle_stop_loss(worker, "m1")
        self.assertAlmostEqual(ledger.worker_summary("btc:5m").realized_pnl, 0.0)

    async def test_confirmed_stop_loss_updates_ledger_and_overall(self):
        from strategies.inventory_accumulation_strategy import MomentumDelayedAdjustmentStrategy

        ledger = self._ledger()
        worker, _ = self._worker_with_notify(ledger)
        strat = MomentumDelayedAdjustmentStrategy()

        class W(SimpleNamespace):
            pass

        w = W(**worker.__dict__)
        w.sync_accounting_dashboard = lambda: None

        def sync():
            summary = ledger.worker_summary("btc:5m")
            w.cumulative_pnl = summary.realized_pnl
            w.dashboard = {
                "realized_pnl": summary.realized_pnl,
                "unrealized_pnl": 0.0,
                "total_net_pnl": summary.realized_pnl,
                "cumulative_realized_pnl": summary.realized_pnl,
            }

        w.sync_accounting_dashboard = sync

        with patch(
            "strategies.inventory_accumulation_strategy.fok_sell_with_retries",
            new_callable=AsyncMock,
            return_value=(5.0, 0.45, "o-sl"),
        ):
            await strat._record_exit_sell(
                w,
                "YES",
                5.0,
                0.45,
                "o-sl",
                exit_reason="stop_loss",
                entry=0.70,
                target_price=0.45,
            )
        summary = ledger.worker_summary("btc:5m")
        self.assertAlmostEqual(summary.realized_pnl, (0.45 - 0.70) * 5.0)
        self.assertEqual(summary.losses, 1)

    def test_restart_ledger_preserves_historical_losses(self):
        path = os.path.join(tempfile.gettempdir(), f"hydrate_{id(self)}.json")
        ledger = CanonicalLedger(path=path, snapshot_path=path + ".pnl")
        ledger.record_fill(
            worker_key="eth:5m",
            market_id="m2",
            order_id="s1",
            side="YES",
            action="SELL",
            price=0.40,
            quantity=5.0,
            realized_pnl_delta=-1.5,
            count_win_loss=True,
        )
        reloaded = CanonicalLedger(path=path, snapshot_path=path + ".pnl")
        self.assertAlmostEqual(reloaded.worker_summary("eth:5m").realized_pnl, -1.5)

    def test_pair_buy_does_not_realize_profit(self):
        ledger = self._ledger()
        ledger.record_fill(
            worker_key="btc:5m",
            market_id="m1",
            order_id="p1",
            side="NO",
            action="BUY",
            price=0.10,
            quantity=5.0,
        )
        self.assertAlmostEqual(ledger.worker_summary("btc:5m").realized_pnl, 0.0)

    def test_gross_inventory_does_not_overwrite_ledger_net(self):
        path = os.path.join(tempfile.gettempdir(), f"gross_{id(self)}.json")
        ledger = CanonicalLedger(path=path, snapshot_path=path + ".pnl")
        ledger.record_fill(
            worker_key="btc:5m",
            market_id="m1",
            order_id="s1",
            side="YES",
            action="SELL",
            price=0.45,
            quantity=5.0,
            realized_pnl_delta=-1.30,
            count_win_loss=True,
        )
        delta = ledger.backfill_worker_realized_if_missing("btc:5m", -1.25)
        self.assertAlmostEqual(delta, 0.0)
        self.assertAlmostEqual(ledger.worker_summary("btc:5m").realized_pnl, -1.30)

    def test_settlement_realized_once(self):
        ledger = self._ledger()
        ok1 = ledger.record_fill(
            worker_key="btc:5m",
            market_id="m1",
            order_id="settle:m1",
            side="SETTLE",
            action="SETTLE",
            price=1.0,
            quantity=1.0,
            realized_pnl_delta=0.25,
            count_win_loss=True,
        )
        ok2 = ledger.record_fill(
            worker_key="btc:5m",
            market_id="m1",
            order_id="settle:m1",
            side="SETTLE",
            action="SETTLE",
            price=1.0,
            quantity=1.0,
            realized_pnl_delta=0.25,
            count_win_loss=True,
        )
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertAlmostEqual(ledger.worker_summary("btc:5m").realized_pnl, 0.25)


if __name__ == "__main__":
    unittest.main()
