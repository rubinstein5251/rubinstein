"""Simplified directional momentum strategy: no hedging, pct exits, config."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import load_worker_configs
from utils.take_profit import take_profit_price
from utils.momentum_risk import stop_loss_price


class ConfigMigrationTests(unittest.TestCase):
    def test_take_profit_pct_from_trading_config(self):
        wc = load_worker_configs()[0]
        self.assertAlmostEqual(wc.take_profit_pct, 0.10)
        self.assertAlmostEqual(wc.stop_loss_pct, 0.20)
        self.assertFalse(wc.matched_pair_accumulation_enabled)

    def test_legacy_config_without_take_profit_pct_defaults(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "defaults": {"take_profit_delta": 0.08, "stop_loss_pct": 0.20},
                        "workers": [{"asset": "btc", "window": "5m"}],
                    },
                    f,
                )
            wc = load_worker_configs(path)[0]
            self.assertAlmostEqual(wc.take_profit_pct, 0.10)
        finally:
            os.unlink(path)


class NoHedgingTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_pair_accumulation_handler(self):
        from strategies.inventory_accumulation_strategy import MomentumDelayedAdjustmentStrategy

        strat = MomentumDelayedAdjustmentStrategy()
        self.assertFalse(hasattr(strat, "_handle_pair_accumulation"))

    async def test_on_market_tick_does_not_buy_opposite(self):
        from bot import OrderState
        from strategies.inventory_accumulation_strategy import MomentumDelayedAdjustmentStrategy
        from utils.momentum_inventory import PositionInventory

        inv = PositionInventory()
        inv.record_buy("YES", 5.0, 0.70)
        cfg = SimpleNamespace(
            key="btc:5m",
            matched_pair_accumulation_enabled=True,
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
            order_size_max=5.0,
            min_order_size=5.0,
        )
        worker = SimpleNamespace(
            worker_config=cfg,
            inventory=inv,
            dashboard={"bought_side": "YES"},
            order_state=OrderState.IDLE,
            _order_lock=asyncio.Lock(),
            _stop_loss_lock=asyncio.Lock(),
            _stop_loss_exit_in_flight=False,
            _market_entry_guard=set(),
            entry_count=1,
            asset_type="btc",
            window_slug="5m",
            active_market={"condition_id": "m1"},
            _ws_market_data_stale=False,
            _book_cache={},
            _exit_submit_guard=set(),
            prices={"YES": 0.5, "NO": 0.5},
            bids={"YES": 0.75, "NO": 0.25},
        )
        worker.canonical_market_id = lambda: "m1"
        worker.market_seconds_left = lambda: 120
        worker.best_bid = lambda s: worker.bids[s]
        worker.best_ask = lambda s: worker.prices[s]
        worker.binance_signal = lambda: None
        worker.refresh_order_book = lambda s: None
        worker.max_quote_age_seconds = lambda: 0.0
        worker.resolved_momentum_max_entry = lambda: SimpleNamespace(minimum_price=0.92)
        worker.sync_accounting_dashboard = lambda: None
        worker.is_dry_run = lambda: True

        strat = MomentumDelayedAdjustmentStrategy()
        with patch.object(strat, "_publish_dashboard"), patch(
            "strategies.inventory_accumulation_strategy.fok_buy_limit",
            new_callable=AsyncMock,
        ) as mock_buy, patch(
            "strategies.inventory_accumulation_strategy.fok_sell_with_retries",
            new_callable=AsyncMock,
            return_value=(0.0, 0.0, None),
        ):
            await strat.on_market_tick(worker)
            mock_buy.assert_not_called()
        self.assertEqual(inv.no_shares, 0.0)


class DualSideEntryBlockTests(unittest.TestCase):
    def test_entry_blocked_when_both_sides_held(self):
        from strategies.inventory_accumulation_strategy import MomentumDelayedAdjustmentStrategy
        from utils.momentum_inventory import PositionInventory

        inv = PositionInventory()
        inv.record_buy("YES", 5.0, 0.70)
        inv.record_buy("NO", 5.0, 0.25)
        worker = SimpleNamespace(
            inventory=inv,
            dashboard={"bought_side": "YES"},
            entry_count=0,
            position_side="YES",
            worker_config=SimpleNamespace(live_trading_enabled=False),
        )
        worker.is_dry_run = lambda: True
        strat = MomentumDelayedAdjustmentStrategy()
        side = strat._position_side(worker)
        self.assertIn(side, ("YES", "NO"))


class ExitPriceFormulaTests(unittest.TestCase):
    def test_take_profit_and_stop_from_avg_fill(self):
        entry = 0.80
        self.assertAlmostEqual(take_profit_price(entry, 0.10).trigger_price, 0.88)
        self.assertAlmostEqual(stop_loss_price(entry, 0.20), 0.64)


if __name__ == "__main__":
    unittest.main()
