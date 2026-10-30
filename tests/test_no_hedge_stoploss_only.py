import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InventoryStrategyIntegrationTests(unittest.TestCase):
    def test_no_legacy_stop_loss_monitor_in_tick_path(self):
        source = (ROOT / "bot.py").read_text(encoding="utf-8")
        start = source.index("    async def check_logic(self, timer: str):")
        end = source.index("    def binance_signal(self)", start)
        body = source[start:end]
        self.assertNotIn("_check_stop_loss", body)

    def test_inventory_cost_simulation_module_exists(self):
        self.assertTrue((ROOT / "utils/inventory_cost.py").is_file())


if __name__ == "__main__":
    unittest.main()
