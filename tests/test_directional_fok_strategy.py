"""Momentum strategy integration: FOK entry, stop-loss, market lockout."""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from utils.momentum_risk import stop_loss_price


class StopLossMathTests(unittest.TestCase):
    def test_stop_from_actual_fill(self):
        self.assertAlmostEqual(stop_loss_price(0.95, 0.20), 0.76)


class StrategyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _worker(self, *, yes=0.0, no=0.0):
        inv = MagicMock()
        inv.yes_shares = 0.0
        inv.no_shares = 0.0
        inv.realized_pnl = 0.0
        inv.avg_cost = MagicMock(return_value=0.0)
        inv.shares = MagicMock(return_value=0.0)

        cfg = SimpleNamespace(
            key="btc:5m",
            momentum_strategy_enabled=True,
            live_trading_enabled=False,
            momentum_max_entry_price=0.92,
            min_net_edge=0.01,
            min_momentum_strength=0.1,
            momentum_volatility_cap=0.5,
            volume_confirmation_threshold=0.0,
            momentum_windows_seconds=(5, 15, 30),
            external_max_data_age_seconds=30.0,
            estimated_fee_rate=0.0,
            execution_slippage_buffer=0.01,
            min_time_before_close_seconds=10,
            max_holding_time_seconds=600.0,
            momentum_reversal_exit_enabled=False,
            stop_loss_pct=0.20,
            take_profit_pct=0.10,
            min_order_size=5.0,
            order_size_max=5.0,
            min_orderbook_depth=5.0,
            max_quote_age_seconds=3.0,
            stop_loss_exit_max_retries=2,
            stop_loss_exit_retry_ms=10,
            interval_seconds=300,
            momentum_trigger_window_seconds=5,
            momentum_confirm_window_seconds=15,
            momentum_support_window_seconds=30,
            momentum_context_window_seconds=60,
            momentum_trigger_min_abs_return=0.00005,
            momentum_confirm_tolerance=0.0001,
            momentum_support_opposing_tolerance=0.002,
            momentum_context_reversal_min_abs_return=0.004,
            momentum_require_volume_confirmation=False,
            min_signal_quality_score=60.0,
            signal_gate_windows_seconds=(5, 15, 30),
            signal_require_timeframe_agreement=False,
            signal_max_spread=0.06,
            signal_max_slippage=0.03,
            signal_max_token_move_since_open=0.12,
            signal_min_persistence_ratio=0.55,
            signal_reversal_acceleration_threshold=-0.0005,
            signal_quality_weights={},
        )

        worker = SimpleNamespace(
            worker_config=cfg,
            inventory=inv,
            prices={"YES": yes, "NO": no},
            bids={"YES": 0.0, "NO": 0.0},
            dashboard={},
            order_state=SimpleNamespace(name="IDLE"),
            _order_lock=asyncio.Lock(),
            _stop_loss_lock=asyncio.Lock(),
            _stop_loss_exit_in_flight=False,
            _market_entry_guard=set(),
            entry_count=0,
            asset_type="btc",
            window_slug="5m",
            active_market={
                "condition_id": "m1",
                "start_ts": int(time.time()) - 60,
                "oracle_label": "chainlink",
            },
            _ws_market_data_stale=False,
        )
        worker.canonical_market_id = lambda: "m1"
        worker.market_seconds_left = lambda: 120
        worker.can_enter = lambda: True
        worker.best_ask = lambda s: worker.prices[s]
        worker.max_quote_age_seconds = lambda: 0.1
        worker.is_dry_run = lambda: True
        worker.entry_order_size = lambda _legs: 5.0
        worker.executable_buy = lambda s, sz: (worker.prices[s], sz) if worker.prices[s] > 0 else None

        signal = MagicMock()
        now = time.time()
        signal.last_trade_price = 101.0
        signal.last_trade_time = now
        signal.imbalance = 0.2
        signal._price_history = [(now - 30 + i, 100.0 + i * 0.03) for i in range(31)]
        worker.binance_signal = lambda: signal

        def resolved_momentum_max_entry():
            from utils.entry_limit import resolve_max_entry_price
            return resolve_max_entry_price(cfg.momentum_max_entry_price)

        worker.resolved_momentum_max_entry = resolved_momentum_max_entry
        worker.record_fill_idempotent = lambda *a, **k: True
        worker.log_trade = lambda *a, **k: None
        return worker

    async def test_no_fixed_95c_entry_without_momentum_edge(self):
        from bot import OrderState
        from strategies.inventory_accumulation_strategy import MomentumDelayedAdjustmentStrategy

        w = self._worker(yes=0.96, no=0.50)
        w.order_state = OrderState.IDLE
        strat = MomentumDelayedAdjustmentStrategy()
        with patch("bot.is_trading_allowed", return_value=True), patch(
            "strategies.inventory_accumulation_strategy.stopped_out_store.is_stopped_out",
            return_value=False,
        ), patch(
            "strategies.inventory_accumulation_strategy.fok_buy_limit",
            new_callable=AsyncMock,
        ) as mock_buy:
            await strat.on_market_tick(w)
            mock_buy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
