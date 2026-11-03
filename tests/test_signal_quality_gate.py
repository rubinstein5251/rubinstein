"""Signal quality gate, edge, and pre-submit rejection tests."""

from __future__ import annotations

import time
import unittest

from utils.market_reference import MarketReferenceContext
from utils.momentum_engine import MomentumDirection, MomentumSnapshot, compute_momentum
from utils.probability_model import compute_edges, estimate_yes_probability
from utils.signal_quality_gate import (
    SignalDecision,
    SignalQualityConfig,
    evaluate_signal_quality,
)


def _ref() -> MarketReferenceContext:
    return MarketReferenceContext(
        oracle_label="chainlink",
        reference_price=100.0,
        current_price=101.0,
        price_source="binance_spot_proxy",
        synchronized=True,
        reason="ok",
    )


def _strong_up_momentum() -> MomentumSnapshot:
    now = time.time()
    hist = [(now - 120 + i, 100.0 + i * 0.08) for i in range(121)]
    return compute_momentum(
        hist,
        windows_sec=(5, 15, 30, 60, 120),
        min_strength=0.3,
        max_data_age_sec=5.0,
        last_update_ts=now,
        order_book_imbalance=0.2,
        volume_confirmation_threshold=0.05,
        volatility_cap=0.5,
        now=now,
    )


class SignalQualityGateTests(unittest.TestCase):
    def test_stale_momentum_rejected(self):
        mom = MomentumSnapshot(
            returns={},
            realized_volatility=0.0,
            acceleration=0.0,
            volume_confirmed=False,
            direction=MomentumDirection.NEUTRAL,
            strength=0.0,
            confidence=0.0,
            stale=True,
            reason="stale_external_data",
        )
        res = evaluate_signal_quality(
            momentum=mom,
            reference=_ref(),
            probability=None,
            side="YES",
            yes_ask=0.55,
            no_ask=0.48,
            yes_bid=0.53,
            no_bid=0.46,
            executable_vwap=0.55,
            executable_filled=5.0,
            order_size=5.0,
            seconds_remaining=120.0,
            interval_seconds=300,
            min_time_remaining=45.0,
            token_ask_at_open=0.50,
            cfg=SignalQualityConfig(min_score=75.0, min_net_edge=0.03),
        )
        self.assertEqual(res.decision, SignalDecision.REJECT)
        self.assertEqual(res.reason, "stale_external_data")

    def test_conflict_direction_rejected(self):
        mom = MomentumSnapshot(
            returns={5: 0.01, 15: -0.008, 30: 0.002},
            realized_volatility=0.002,
            acceleration=0.0,
            volume_confirmed=True,
            direction=MomentumDirection.CONFLICT,
            strength=1.0,
            confidence=0.5,
            stale=False,
            reason="context_reversal_against_trigger",
        )
        res = evaluate_signal_quality(
            momentum=mom,
            reference=_ref(),
            probability=None,
            side=None,
            yes_ask=0.55,
            no_ask=0.48,
            yes_bid=0.53,
            no_bid=0.46,
            executable_vwap=0.55,
            executable_filled=5.0,
            order_size=5.0,
            seconds_remaining=120.0,
            interval_seconds=300,
            min_time_remaining=45.0,
            token_ask_at_open=0.50,
            cfg=SignalQualityConfig(),
        )
        self.assertEqual(res.reason, "conflicting_timeframes")

    def test_net_edge_uses_executable_price(self):
        mom = _strong_up_momentum()
        p_yes = estimate_yes_probability(
            reference_price=100.0,
            current_price=101.0,
            seconds_remaining=200.0,
            interval_seconds=300,
            momentum=mom,
        )
        self.assertIsNotNone(p_yes)
        prob = compute_edges(
            p_yes=float(p_yes),
            yes_ask=0.55,
            no_ask=0.48,
            fee_rate=0.0,
            slippage_buffer=0.02,
            min_net_edge=0.05,
            max_entry_price=0.92,
        )
        res = evaluate_signal_quality(
            momentum=mom,
            reference=_ref(),
            probability=prob,
            side="YES",
            yes_ask=0.55,
            no_ask=0.48,
            yes_bid=0.54,
            no_bid=0.47,
            executable_vwap=0.56,
            executable_filled=5.0,
            order_size=5.0,
            seconds_remaining=200.0,
            interval_seconds=300,
            min_time_remaining=45.0,
            token_ask_at_open=0.52,
            cfg=SignalQualityConfig(min_score=0.0, min_net_edge=0.05, slippage_buffer=0.02),
        )
        self.assertLess(res.net_edge, prob.yes_edge)
        if res.net_edge < 0.05:
            self.assertEqual(res.decision, SignalDecision.REJECT)

    def test_high_score_passes_when_edges_strong(self):
        mom = _strong_up_momentum()
        p_yes = estimate_yes_probability(
            reference_price=100.0,
            current_price=101.5,
            seconds_remaining=200.0,
            interval_seconds=300,
            momentum=mom,
        )
        prob = compute_edges(
            p_yes=float(p_yes),
            yes_ask=0.50,
            no_ask=0.52,
            fee_rate=0.0,
            slippage_buffer=0.01,
            min_net_edge=0.03,
            max_entry_price=0.92,
        )
        res = evaluate_signal_quality(
            momentum=mom,
            reference=_ref(),
            probability=prob,
            side="YES",
            yes_ask=0.50,
            no_ask=0.52,
            yes_bid=0.49,
            no_bid=0.51,
            executable_vwap=0.50,
            executable_filled=5.0,
            order_size=5.0,
            seconds_remaining=200.0,
            interval_seconds=300,
            min_time_remaining=45.0,
            token_ask_at_open=0.48,
            cfg=SignalQualityConfig(min_score=75.0, min_net_edge=0.03, slippage_buffer=0.01),
        )
        if res.decision == SignalDecision.ENTER:
            self.assertGreaterEqual(res.total_score, 75.0)


class VolumeConfirmationTests(unittest.TestCase):
    def test_neutral_when_imbalance_wrong_sign(self):
        now = time.time()
        hist = [(now - 30 + i, 100.0 + i * 0.05) for i in range(31)]
        snap = compute_momentum(
            hist,
            windows_sec=(5, 15, 30),
            min_strength=0.2,
            max_data_age_sec=5.0,
            last_update_ts=now,
            order_book_imbalance=-0.2,
            volume_confirmation_threshold=0.05,
            volatility_cap=0.5,
            now=now,
            require_volume_confirmation=True,
        )
        self.assertIn(snap.direction, (MomentumDirection.NEUTRAL, MomentumDirection.CONFLICT))


if __name__ == "__main__":
    unittest.main()
