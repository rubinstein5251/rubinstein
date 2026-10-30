"""Entry limit price sync and asset cooldown removal."""

from __future__ import annotations

import unittest
from pathlib import Path

from utils.clob_helpers import clamp_buy_price
from utils.entry_limit import compute_buy_fok_limit_price, resolve_minimum_entry_price


class EntryLimitTests(unittest.TestCase):
    def test_minimum_resolves_to_tick(self):
        res = resolve_minimum_entry_price(0.95)
        self.assertAlmostEqual(res.minimum_price, clamp_buy_price(0.95))

    def test_executable_at_or_above_minimum(self):
        res = resolve_minimum_entry_price(0.95)
        limit, reason = compute_buy_fok_limit_price(0.96, res.minimum_price)
        self.assertEqual(reason, "ok")
        self.assertGreaterEqual(limit, res.minimum_price)

    def test_below_minimum_rejected(self):
        res = resolve_minimum_entry_price(0.95)
        limit, reason = compute_buy_fok_limit_price(0.94, res.minimum_price)
        self.assertIsNone(limit)
        self.assertEqual(reason, "below_minimum_entry")

    def test_dashboard_and_worker_share_resolver(self):
        source = Path("bot.py").read_text(encoding="utf-8")
        self.assertIn("def resolved_entry_fok_limit", source)
        self.assertIn("resolve_minimum_entry_price", source)


class AssetCooldownRemovedTests(unittest.TestCase):
    def test_no_asset_cooldown_manager(self):
        source = Path("bot.py").read_text(encoding="utf-8")
        self.assertNotIn("AssetCooldownManager", source)
        self.assertNotIn("asset_cooldown", source)

    def test_config_has_no_asset_cooldown_env(self):
        source = Path("config.py").read_text(encoding="utf-8")
        self.assertNotIn("ASSET_COOLDOWN_MINUTES", source)

    def test_strategy_has_no_cooldown_gate(self):
        source = Path("strategies/inventory_accumulation_strategy.py").read_text(encoding="utf-8")
        self.assertNotIn("asset_cooldown", source)


if __name__ == "__main__":
    unittest.main()
