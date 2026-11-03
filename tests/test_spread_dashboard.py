import unittest

from config import load_worker_configs


class DashboardConfigTests(unittest.TestCase):
    def test_entry_and_stop_defaults(self):
        wc = load_worker_configs()[0]
        self.assertAlmostEqual(wc.momentum_max_entry_price, 0.92)
        self.assertAlmostEqual(wc.stop_loss_pct, 0.20)
        self.assertEqual(wc.order_type, "FOK")
        self.assertAlmostEqual(wc.take_profit_pct, 0.10)
        self.assertGreater(wc.external_max_data_age_seconds, 0)


if __name__ == "__main__":
    unittest.main()
