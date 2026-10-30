import asyncio
import math
import sys
import types
import unittest

from utils.momentum_inventory import PositionInventory


class InventoryValidationTests(unittest.TestCase):
    def test_rejects_non_finite_buy_values(self):
        inv = PositionInventory()
        for shares, price in [(math.inf, 0.5), (1, math.nan), (1, math.inf)]:
            with self.subTest(shares=shares, price=price):
                with self.assertRaises(ValueError):
                    inv.record_buy("YES", shares, price)
        self.assertEqual(inv.yes_shares, 0.0)

    def test_rejects_non_finite_sell_values(self):
        inv = PositionInventory()
        inv.record_buy("YES", 2, 0.4)
        with self.assertRaises(ValueError):
            inv.record_sell("YES", math.nan, 0.5)
        with self.assertRaises(ValueError):
            inv.record_sell("YES", 1, math.inf)
        self.assertEqual(inv.yes_shares, 2.0)

    def test_rejects_invalid_mark_price(self):
        inv = PositionInventory()
        with self.assertRaises(ValueError):
            inv.mark_to_market("YES", math.nan)


class PaperFillTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_leg_does_not_fill_if_ask_never_crosses_limit(self):
        fake_bot = types.ModuleType("bot")
        fake_bot.MIN_FILL_DELTA = 1e-6
        sys.modules.setdefault("bot", fake_bot)
        from strategies.momentum_execution import _simulate_dry_leg

        class Config:
            fill_timeout_ms = 0
            fill_poll_ms = 50

        class Worker:
            worker_config = Config()
            def best_ask(self, side):
                return 0.60

        result = await _simulate_dry_leg(Worker(), "YES", 0.50, 5)
        self.assertIsNone(result)

    async def test_dry_leg_only_crosses_at_or_below_limit(self):
        fake_bot = types.ModuleType("bot")
        fake_bot.MIN_FILL_DELTA = 1e-6
        sys.modules.setdefault("bot", fake_bot)
        from strategies.momentum_execution import _simulate_dry_leg

        class Config:
            fill_timeout_ms = 0
            fill_poll_ms = 50

        class Worker:
            worker_config = Config()
            def best_ask(self, side):
                return 0.49

        result = await _simulate_dry_leg(Worker(), "YES", 0.50, 5)
        self.assertEqual(result, ("YES", 5.0, 0.49))

    async def test_cumulative_fill_snapshots_are_not_added_twice(self):
        fake_bot = types.ModuleType("bot")
        fake_bot.MIN_FILL_DELTA = 1e-6
        sys.modules.setdefault("bot", fake_bot)
        from strategies.momentum_execution import _monitor_live_fills

        class Config:
            fill_timeout_ms = 25
            fill_poll_ms = 5

        class Worker:
            worker_config = Config()
            calls = 0
            def _untrack_order(self, order_id):
                pass
            async def poll_order_fill(self, order_id, requested, price):
                self.calls += 1
                return (2.0 if self.calls < 3 else 3.0), 0.40
            async def cancel_order_confirmed(self, order_id, requested, price):
                return 3.0, 0.40  # cumulative final matched size

        result = await _monitor_live_fills(
            Worker(), types.SimpleNamespace(size=5), [("YES", 0.40)], [("order-1", 0.0)]
        )
        self.assertAlmostEqual(result["YES"][0], 3.0)
        self.assertAlmostEqual(result["YES"][1], 0.40)


if __name__ == "__main__":
    unittest.main()
