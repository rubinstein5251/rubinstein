import unittest
from pathlib import Path


class ConfigurableEntryTests(unittest.TestCase):
    def test_entry_price_configurable(self):
        from config import load_worker_configs

        wc = load_worker_configs()[0]
        self.assertAlmostEqual(wc.momentum_max_entry_price, 0.92)
        self.assertGreater(wc.min_net_edge, 0)

    def test_polymarket_tick_drives_strategy(self):
        source = Path("bot.py").read_text(encoding="utf-8")
        self.assertIn("await self.strategy.on_market_tick(self)", source)


if __name__ == "__main__":
    unittest.main()
