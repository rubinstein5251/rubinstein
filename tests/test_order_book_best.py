"""Top-of-book extraction from CLOB snapshots."""

from __future__ import annotations

import unittest

from utils.order_book_math import best_ask_from_levels, best_bid_from_levels


class OrderBookBestTests(unittest.TestCase):
    def test_best_ask_is_minimum_not_first_row(self):
        asks = [
            {"price": "0.99", "size": "100"},
            {"price": "0.52", "size": "50"},
            {"price": "0.55", "size": "25"},
        ]
        self.assertAlmostEqual(best_ask_from_levels(asks), 0.52)

    def test_best_bid_is_maximum(self):
        bids = [
            {"price": "0.01", "size": "10"},
            {"price": "0.48", "size": "20"},
        ]
        self.assertAlmostEqual(best_bid_from_levels(bids), 0.48)


if __name__ == "__main__":
    unittest.main()
