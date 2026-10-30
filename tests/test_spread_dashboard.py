import unittest

from config import load_worker_configs


class DashboardConfigTests(unittest.TestCase):
    def test_entry_and_stop_defaults(self):
        wc = load_worker_configs()[0]
        self.assertAlmostEqual(wc.entry_price, 0.95)
        self.assertAlmostEqual(wc.stop_loss_pct, 0.35)
        self.assertEqual(wc.order_type, "FOK")


if __name__ == "__main__":
    unittest.main()
