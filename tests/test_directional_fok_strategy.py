"""Directional FOK entry + stop-loss + market lockout tests."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from utils.directional_entry import EntryDecision, pick_entry_side
from utils.momentum_risk import stop_loss_price


class EntrySelectionTests(unittest.TestCase):
    def test_94c_no_buy(self):
        ch = pick_entry_side(0.94, 0.50, min_entry_price=0.95)
        self.assertEqual(ch.decision, EntryDecision.SKIP)

    def test_95c_eligible(self):
        ch = pick_entry_side(0.95, 0.50, min_entry_price=0.95)
        self.assertEqual(ch.decision, EntryDecision.BUY)

    def test_96c_eligible(self):
        ch = pick_entry_side(0.96, 0.50, min_entry_price=0.95)
        self.assertEqual(ch.decision, EntryDecision.BUY)

    def test_both_qualify_picks_lower_among_valid(self):
        ch = pick_entry_side(0.96, 0.97, min_entry_price=0.95)
        self.assertEqual(ch.side, "YES")


class StopLossMathTests(unittest.TestCase):
    def test_stop_from_actual_fill(self):
        self.assertAlmostEqual(stop_loss_price(0.95, 0.35), 0.6175)


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
            entry_price=0.95,
            stop_loss_pct=0.35,
            min_order_size=5.0,
            order_size_max=5.0,
            min_orderbook_depth=5.0,
            max_quote_age_seconds=3.0,
            stop_loss_exit_max_retries=2,
            stop_loss_exit_retry_ms=10,
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
            active_market={"condition_id": "m1"},
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

        def resolved_entry_fok_limit():
            from utils.entry_limit import resolve_minimum_entry_price
            return resolve_minimum_entry_price(cfg.entry_price)

        worker.resolved_entry_fok_limit = resolved_entry_fok_limit
        worker.record_fill_idempotent = lambda *a, **k: True
        worker.log_trade = lambda *a, **k: None
        return worker

    async def test_no_buy_when_ask_below_minimum(self):
        from bot import OrderState
        from strategies.inventory_accumulation_strategy import DirectionalFokStrategy

        w = self._worker(yes=0.94, no=0.50)
        w.order_state = OrderState.IDLE
        strat = DirectionalFokStrategy()
        with patch("bot.is_trading_allowed", return_value=True), patch(
            "strategies.inventory_accumulation_strategy.stopped_out_store.is_stopped_out",
            return_value=False,
        ), patch(
            "strategies.inventory_accumulation_strategy.fok_buy_limit",
            new_callable=AsyncMock,
        ) as mock_buy:
            await strat.on_market_tick(w)
            mock_buy.assert_not_called()

    async def test_buy_at_96_uses_executable_limit(self):
        from bot import OrderState
        from strategies.inventory_accumulation_strategy import DirectionalFokStrategy

        w = self._worker(yes=0.96, no=0.50)
        w.order_state = OrderState.IDLE
        strat = DirectionalFokStrategy()

        async def _fake_buy(worker, side, limit_px, size, *, purpose):
            self.assertAlmostEqual(limit_px, 0.96)
            return 0.0, 0.0, None

        with patch("bot.is_trading_allowed", return_value=True), patch(
            "strategies.inventory_accumulation_strategy.stopped_out_store.is_stopped_out",
            return_value=False,
        ), patch(
            "strategies.inventory_accumulation_strategy.fok_buy_limit",
            side_effect=_fake_buy,
        ) as mock_buy:
            await strat.on_market_tick(w)
            mock_buy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
