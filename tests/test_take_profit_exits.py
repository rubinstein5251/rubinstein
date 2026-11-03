"""Percentage take-profit and stop-loss exit tests."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from utils.take_profit import (
    approaching_take_profit,
    take_profit_price,
    take_profit_triggered,
    validate_take_profit_for_entry,
)
from utils.momentum_risk import stop_loss_price


class TakeProfitMathTests(unittest.TestCase):
    def test_entry_80c_target_88c_at_10pct(self):
        res = take_profit_price(0.80, 0.10)
        self.assertTrue(res.reachable)
        self.assertAlmostEqual(res.trigger_price, 0.88)

    def test_entry_70c_target_77c_at_10pct(self):
        res = take_profit_price(0.70, 0.10)
        self.assertTrue(res.reachable)
        self.assertAlmostEqual(res.trigger_price, 0.77)

    def test_no_trigger_before_target(self):
        res = take_profit_price(0.80, 0.10)
        self.assertFalse(take_profit_triggered(0.87, res.trigger_price))

    def test_trigger_at_target(self):
        res = take_profit_price(0.80, 0.10)
        self.assertTrue(take_profit_triggered(0.88, res.trigger_price))

    def test_unreachable_entry_rejected(self):
        ok, reason = validate_take_profit_for_entry(0.96, 0.10)
        self.assertFalse(ok)
        self.assertEqual(reason, "take_profit_above_max_token_price")

    def test_stop_loss_20pct_from_entry(self):
        self.assertAlmostEqual(stop_loss_price(0.80, 0.20), 0.64)
        self.assertAlmostEqual(stop_loss_price(0.70, 0.20), 0.56)


class TakeProfitExecutionTests(unittest.IsolatedAsyncioTestCase):
    def _worker(self, *, bid=0.88, entry=0.80, shares=5.0):
        inv = MagicMock()
        inv.yes_shares = shares
        inv.no_shares = 0.0
        inv.realized_pnl = 0.0
        inv.avg_cost = MagicMock(return_value=entry)
        inv.shares = MagicMock(return_value=shares)

        cfg = SimpleNamespace(
            key="btc:5m",
            take_profit_pct=0.10,
            stop_loss_pct=0.20,
            stop_loss_exit_max_retries=1,
            stop_loss_exit_retry_ms=10,
            estimated_fee_rate=0.0,
            momentum_strategy_enabled=True,
            live_trading_enabled=False,
            momentum_max_entry_price=0.92,
            min_net_edge=0.01,
            min_momentum_strength=0.1,
            momentum_volatility_cap=0.5,
            volume_confirmation_threshold=0.0,
            momentum_windows_seconds=(5, 15),
            external_max_data_age_seconds=30.0,
            execution_slippage_buffer=0.01,
            min_time_before_close_seconds=10,
            min_orderbook_depth=5.0,
            max_quote_age_seconds=3.0,
            interval_seconds=300,
        )

        book = {
            "bids": [{"price": bid, "size": shares + 10}],
            "asks": [],
        }
        worker = SimpleNamespace(
            worker_config=cfg,
            inventory=inv,
            prices={"YES": 0.5, "NO": 0.5},
            bids={"YES": bid, "NO": 0.0},
            dashboard={"bought_side": "YES"},
            order_state=SimpleNamespace(name="IDLE"),
            _order_lock=asyncio.Lock(),
            _stop_loss_lock=asyncio.Lock(),
            _stop_loss_exit_in_flight=False,
            _market_entry_guard=set(),
            entry_count=1,
            asset_type="btc",
            window_slug="5m",
            active_market={"condition_id": "m1", "start_ts": 1},
            _ws_market_data_stale=False,
            _book_cache={"YES": (0.0, book)},
            _exit_submit_guard=set(),
        )
        worker.canonical_market_id = lambda: "m1"
        worker.market_seconds_left = lambda: 120
        worker.best_bid = lambda s: worker.bids[s]
        worker.best_ask = lambda s: 0.5
        worker.max_quote_age_seconds = lambda: 0.1
        worker.refresh_order_book = lambda s: None
        worker.is_dry_run = lambda: True
        worker.record_fill_idempotent = lambda *a, **k: True
        worker.log_trade = lambda *a, **k: None
        worker.binance_signal = lambda: None
        return worker

    async def test_take_profit_handler_exists(self):
        from strategies import inventory_accumulation_strategy as mod

        self.assertTrue(hasattr(mod.MomentumDelayedAdjustmentStrategy, "_handle_take_profit"))

    async def test_pair_accumulation_handler_removed(self):
        from strategies import inventory_accumulation_strategy as mod

        self.assertFalse(hasattr(mod.MomentumDelayedAdjustmentStrategy, "_handle_pair_accumulation"))

    async def test_take_profit_fok_failure_keeps_position(self):
        from bot import OrderState
        from strategies.inventory_accumulation_strategy import MomentumDelayedAdjustmentStrategy

        w = self._worker(bid=0.88)
        w.order_state = OrderState.IDLE
        strat = MomentumDelayedAdjustmentStrategy()
        with patch(
            "strategies.inventory_accumulation_strategy.fok_sell_with_retries",
            new_callable=AsyncMock,
            return_value=(0.0, 0.0, None),
        ):
            await strat._handle_take_profit(w, "m1")
        self.assertGreater(w.inventory.shares("YES"), 0)


if __name__ == "__main__":
    unittest.main()
