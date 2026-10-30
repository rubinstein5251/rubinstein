"""Minimum entry price (best ask >= threshold) and FOK limit selection."""

from __future__ import annotations

import unittest

from utils.clob_helpers import clamp_buy_price
from utils.directional_entry import EntryDecision, pick_entry_side
from utils.entry_limit import compute_buy_fok_limit_price, resolve_minimum_entry_price


class MinimumEntryThresholdTests(unittest.TestCase):
    def test_94c_no_entry(self):
        ch = pick_entry_side(0.94, 0.50, min_entry_price=0.95)
        self.assertEqual(ch.decision, EntryDecision.SKIP)

    def test_95c_eligible(self):
        ch = pick_entry_side(0.95, 0.50, min_entry_price=0.95)
        self.assertEqual(ch.decision, EntryDecision.BUY)
        self.assertEqual(ch.side, "YES")

    def test_96_97_99_eligible(self):
        for px in (0.96, 0.97, 0.99):
            with self.subTest(px=px):
                ch = pick_entry_side(px, 0.50, min_entry_price=0.95)
                self.assertEqual(ch.decision, EntryDecision.BUY)

    def test_limit_price_matches_executable(self):
        min_res = resolve_minimum_entry_price(0.95)
        limit, reason = compute_buy_fok_limit_price(0.97, min_res.minimum_price)
        self.assertEqual(reason, "ok")
        self.assertAlmostEqual(limit, 0.97)

    def test_limit_never_below_minimum(self):
        limit, reason = compute_buy_fok_limit_price(0.94, 0.95)
        self.assertIsNone(limit)
        self.assertEqual(reason, "below_minimum_entry")

    def test_above_max_binary_price_rejected(self):
        ch = pick_entry_side(1.0, 0.50, min_entry_price=0.95)
        self.assertEqual(ch.decision, EntryDecision.SKIP)
        limit, reason = compute_buy_fok_limit_price(1.05, 0.95)
        self.assertIsNone(limit)
        self.assertEqual(reason, "above_max_token_price")


class DashboardLimitSyncTests(unittest.TestCase):
    def test_clamp_is_idempotent_for_submission(self):
        px = 0.97
        self.assertAlmostEqual(clamp_buy_price(px), clamp_buy_price(clamp_buy_price(px)))


if __name__ == "__main__":
    unittest.main()
