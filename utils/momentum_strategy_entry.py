"""Momentum + delayed-adjustment entry selection (replaces fixed 95c floor)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from utils.momentum_engine import MomentumDirection, MomentumSnapshot
from utils.market_reference import MarketReferenceContext
from utils.probability_model import ProbabilityEstimate, compute_edges, estimate_yes_probability


class StrategyEntryDecision(Enum):
    ENTER = auto()
    SKIP = auto()


@dataclass(frozen=True)
class MomentumEntryChoice:
    decision: StrategyEntryDecision
    side: Optional[str]
    best_ask: float
    limit_cap: float
    probability: Optional[ProbabilityEstimate]
    momentum: Optional[MomentumSnapshot]
    reference: Optional[MarketReferenceContext]
    reason: str


def evaluate_momentum_entry(
    *,
    yes_ask: float,
    no_ask: float,
    reference: MarketReferenceContext,
    momentum: MomentumSnapshot,
    seconds_remaining: float,
    interval_seconds: int,
    min_net_edge: float,
    max_entry_price: float,
    fee_rate: float,
    slippage_buffer: float,
    min_time_remaining: int,
) -> MomentumEntryChoice:
    if momentum.stale:
        return MomentumEntryChoice(
            StrategyEntryDecision.SKIP, None, 0.0, max_entry_price, None, momentum, reference,
            momentum.reason,
        )
    if seconds_remaining < min_time_remaining:
        return MomentumEntryChoice(
            StrategyEntryDecision.SKIP, None, 0.0, max_entry_price, None, momentum, reference,
            "insufficient_time_remaining",
        )
    if not reference.synchronized:
        return MomentumEntryChoice(
            StrategyEntryDecision.SKIP, None, 0.0, max_entry_price, None, momentum, reference,
            reference.reason,
        )
    if momentum.direction not in (MomentumDirection.UP, MomentumDirection.DOWN):
        return MomentumEntryChoice(
            StrategyEntryDecision.SKIP, None, 0.0, max_entry_price, None, momentum, reference,
            momentum.reason,
        )

    p_yes = estimate_yes_probability(
        reference_price=reference.reference_price,
        current_price=reference.current_price,
        seconds_remaining=float(seconds_remaining),
        interval_seconds=interval_seconds,
        momentum=momentum,
    )
    if p_yes is None:
        return MomentumEntryChoice(
            StrategyEntryDecision.SKIP, None, 0.0, max_entry_price, None, momentum, reference,
            "probability_unavailable",
        )

    prob = compute_edges(
        p_yes=p_yes,
        yes_ask=yes_ask,
        no_ask=no_ask,
        fee_rate=fee_rate,
        slippage_buffer=slippage_buffer,
        min_net_edge=min_net_edge,
        max_entry_price=max_entry_price,
    )

    if momentum.direction == MomentumDirection.UP and prob.selected_side != "YES":
        return MomentumEntryChoice(
            StrategyEntryDecision.SKIP, None, 0.0, max_entry_price, prob, momentum, reference,
            f"momentum_yes_mismatch:{prob.reason}",
        )
    if momentum.direction == MomentumDirection.DOWN and prob.selected_side != "NO":
        return MomentumEntryChoice(
            StrategyEntryDecision.SKIP, None, 0.0, max_entry_price, prob, momentum, reference,
            f"momentum_no_mismatch:{prob.reason}",
        )
    if not prob.selected_side:
        return MomentumEntryChoice(
            StrategyEntryDecision.SKIP, None, 0.0, max_entry_price, prob, momentum, reference,
            prob.reason,
        )

    ask = yes_ask if prob.selected_side == "YES" else no_ask
    return MomentumEntryChoice(
        StrategyEntryDecision.ENTER,
        prob.selected_side,
        ask,
        max_entry_price,
        prob,
        momentum,
        reference,
        "entry_eligible",
    )
