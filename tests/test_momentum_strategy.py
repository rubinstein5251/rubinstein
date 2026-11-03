"""Momentum engine, probability, and strategy entry tests."""

from __future__ import annotations

import time
import unittest

from utils.momentum_engine import MomentumDirection, compute_momentum
from utils.momentum_strategy_entry import StrategyEntryDecision, evaluate_momentum_entry
from utils.market_reference import MarketReferenceContext, resolve_market_reference
from utils.probability_model import compute_edges, estimate_yes_probability
from utils.entry_limit import compute_momentum_buy_fok_limit_price


def _history_from_moves(base: float, moves: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """moves: list of (offset_sec, price)."""
    t0 = time.time() - max(o for o, _ in moves) - 1
    return [(t0 + off, px) for off, px in moves]


class MomentumEngineTests(unittest.TestCase):
    def test_strong_up_move(self):
        now = time.time()
        hist = [(now - 120 + i, 100.0 + i * 0.05) for i in range(121)]
        snap = compute_momentum(
            hist,
            windows_sec=(5, 15, 30, 60, 120),
            min_strength=0.5,
            max_data_age_sec=5.0,
            last_update_ts=now,
            order_book_imbalance=0.2,
            volume_confirmation_threshold=0.05,
            volatility_cap=0.5,
            now=now,
            require_volume_confirmation=False,
        )
        self.assertEqual(snap.direction, MomentumDirection.UP)

    def test_stale_data_blocks(self):
        snap = compute_momentum(
            [],
            windows_sec=(5, 15),
            min_strength=0.5,
            max_data_age_sec=1.0,
            last_update_ts=time.time() - 10,
            now=time.time(),
        )
        self.assertTrue(snap.stale)


class ProbabilityTests(unittest.TestCase):
    def test_yes_probability_rises_with_price(self):
        from utils.momentum_engine import MomentumSnapshot

        mom = MomentumSnapshot(
            returns={5: 0.01},
            realized_volatility=0.001,
            acceleration=0.0,
            volume_confirmed=True,
            direction=MomentumDirection.UP,
            strength=1.0,
            confidence=0.8,
            stale=False,
            reason="ok",
        )
        p_up = estimate_yes_probability(
            reference_price=100.0,
            current_price=101.0,
            seconds_remaining=200.0,
            interval_seconds=300,
            momentum=mom,
        )
        p_down = estimate_yes_probability(
            reference_price=100.0,
            current_price=99.0,
            seconds_remaining=200.0,
            interval_seconds=300,
            momentum=mom,
        )
        self.assertIsNotNone(p_up)
        self.assertIsNotNone(p_down)
        assert p_up is not None and p_down is not None
        self.assertGreater(p_up, p_down)

    def test_edge_requires_minimum(self):
        est = compute_edges(
            p_yes=0.70,
            yes_ask=0.69,
            no_ask=0.35,
            fee_rate=0.0,
            slippage_buffer=0.02,
            min_net_edge=0.05,
            max_entry_price=0.92,
        )
        self.assertIsNone(est.selected_side)


class EntrySelectionTests(unittest.TestCase):
    def _ref(self, *, sync: bool = True) -> MarketReferenceContext:
        return MarketReferenceContext(
            oracle_label="chainlink",
            reference_price=100.0,
            current_price=101.0,
            price_source="binance_spot_proxy",
            synchronized=sync,
            reason="ok" if sync else "missing",
        )

    def test_no_entry_without_reference(self):
        now = time.time()
        hist = [(now - i, 100.0 + i * 0.01) for i in range(30, 0, -1)]
        mom = compute_momentum(
            hist,
            windows_sec=(5, 15, 30),
            min_strength=0.3,
            max_data_age_sec=5.0,
            last_update_ts=now,
            order_book_imbalance=0.2,
            volume_confirmation_threshold=0.05,
            volatility_cap=0.5,
            now=now,
        )
        choice = evaluate_momentum_entry(
            yes_ask=0.60,
            no_ask=0.42,
            reference=self._ref(sync=False),
            momentum=mom,
            seconds_remaining=120,
            interval_seconds=300,
            min_net_edge=0.03,
            max_entry_price=0.92,
            fee_rate=0.0,
            slippage_buffer=0.01,
            min_time_remaining=30,
        )
        self.assertEqual(choice.decision, StrategyEntryDecision.SKIP)


class FokLimitTests(unittest.TestCase):
    def test_cap_enforced(self):
        limit, reason = compute_momentum_buy_fok_limit_price(0.94, 0.92)
        self.assertIsNone(limit)
        self.assertEqual(reason, "above_max_entry_price")


if __name__ == "__main__":
    unittest.main()
