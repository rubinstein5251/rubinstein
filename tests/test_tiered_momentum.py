"""Tiered momentum trigger/confirm and rejection counter tests."""

from __future__ import annotations

import time
import unittest

from utils.entry_rejection_counters import EntryRejectionCounterStore
from utils.momentum_engine import MomentumDirection, compute_momentum


class TieredMomentumTests(unittest.TestCase):
    def test_down_trigger_confirm_with_flat_context(self):
        now = time.time()
        # Recent down move: trigger+confirm negative; 60s flat/slightly down.
        hist = [(now - 60 + i, 100.0 - i * 0.05) for i in range(61)]
        snap = compute_momentum(
            hist,
            windows_sec=(5, 15, 30, 60, 120),
            min_strength=0.1,
            max_data_age_sec=5.0,
            last_update_ts=now,
            order_book_imbalance=0.0,
            volume_confirmation_threshold=0.05,
            volatility_cap=0.5,
            now=now,
            require_volume_confirmation=False,
        )
        self.assertEqual(snap.direction, MomentumDirection.DOWN)

    def test_conflict_when_context_reverses_trigger(self):
        now = time.time()
        hist = [(now - 60, 100.0), (now - 30, 99.0), (now - 5, 100.5), (now, 100.8)]
        snap = compute_momentum(
            hist,
            windows_sec=(5, 15, 30, 60),
            min_strength=0.1,
            max_data_age_sec=5.0,
            last_update_ts=now,
            volatility_cap=0.5,
            now=now,
            context_reversal_min_abs_return=0.001,
            require_volume_confirmation=False,
        )
        self.assertIn(
            snap.direction,
            (MomentumDirection.CONFLICT, MomentumDirection.NEUTRAL, MomentumDirection.UP),
        )


class RejectionCounterTests(unittest.TestCase):
    def test_reason_counts(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            store = EntryRejectionCounterStore(path=path)
            store.record("btc:5m", stage="momentum", reason="conflicting_timeframes", candidate=True)
            store.record("btc:5m", stage="signal_quality", reason="edge_below_minimum", candidate=True)
            snap = store.snapshot("btc:5m")
            self.assertEqual(snap.by_reason["conflicting_timeframes"], 1)
            self.assertEqual(snap.candidates_evaluated, 2)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
