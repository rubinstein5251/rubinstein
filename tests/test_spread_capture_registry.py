"""Legacy spread registry removed — Binance FOK config and signal tests."""

import unittest

from config import WORKER_CONFIGS
from utils.btc_signal import detect_btc_signal, parse_signal_windows_ms
from utils.pair_cost import combined_pair_cost


class BinanceFokConfigTests(unittest.TestCase):
    def test_worker_has_binance_fok_settings(self):
        wc = WORKER_CONFIGS[0]
        self.assertTrue(wc.binance_fok_enabled)
        self.assertGreater(wc.binance_fok_up_move_threshold_pct, 0)
        self.assertGreater(wc.binance_fok_max_combined_pair_cost, 0)

    def test_signal_windows_parse(self):
        w = parse_signal_windows_ms("1000,3000,5000")
        self.assertEqual(len(w), 3)


class PairCostTests(unittest.TestCase):
    def test_profitable_pair(self):
        pc = combined_pair_cost(0.52, 0.45, fee_rate=0.0, slippage=0.0)
        self.assertAlmostEqual(pc, 0.97)


if __name__ == "__main__":
    unittest.main()
