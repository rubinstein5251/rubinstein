"""Estimate P(YES) for crypto up/down markets from reference price and momentum.

Model (testable, no look-ahead):
  z = (rel_move / (vol * sqrt(time_frac))) + momentum tilt + acceleration tilt
  p_yes = sigmoid(2.2 * z), clipped to [0.01, 0.99]

``rel_move`` is (current - reference) / reference. Vol comes from realized
returns on the external feed. Momentum direction/strength nudge ``z`` but are
not treated as calibrated probabilities — net edge is always vs executable ask.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from utils.momentum_engine import MomentumDirection, MomentumSnapshot


@dataclass(frozen=True)
class ProbabilityEstimate:
    p_yes: float
    p_no: float
    yes_edge: float
    no_edge: float
    selected_side: Optional[str]
    reason: str


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def estimate_yes_probability(
    *,
    reference_price: float,
    current_price: float,
    seconds_remaining: float,
    interval_seconds: float,
    momentum: MomentumSnapshot,
) -> Optional[float]:
    if reference_price <= 0 or current_price <= 0:
        return None
    if seconds_remaining <= 0 or interval_seconds <= 0:
        return None

    rel_move = (current_price - reference_price) / reference_price
    vol = max(momentum.realized_volatility, 1e-6)
    time_frac = max(0.05, seconds_remaining / interval_seconds)
    scale = vol * math.sqrt(time_frac)
    z = rel_move / scale

    if momentum.direction == MomentumDirection.UP:
        z += 0.35 * momentum.strength
    elif momentum.direction == MomentumDirection.DOWN:
        z -= 0.35 * momentum.strength

    z += 0.15 * momentum.acceleration / max(vol, 1e-6)
    p = _sigmoid(2.2 * z)
    return max(0.01, min(0.99, p))


def compute_edges(
    *,
    p_yes: float,
    yes_ask: float,
    no_ask: float,
    fee_rate: float,
    slippage_buffer: float,
    min_net_edge: float,
    max_entry_price: float,
) -> ProbabilityEstimate:
    total_cost = fee_rate + slippage_buffer
    yes_edge = p_yes - yes_ask - total_cost
    no_edge = (1.0 - p_yes) - no_ask - total_cost

    best_side: Optional[str] = None
    reason = "no_edge"
    if yes_edge >= min_net_edge and 0 < yes_ask <= max_entry_price + 1e-9:
        if no_edge >= min_net_edge and 0 < no_ask <= max_entry_price + 1e-9:
            best_side = "YES" if yes_edge >= no_edge else "NO"
            reason = "dual_edge_pick_stronger"
        else:
            best_side = "YES"
            reason = "yes_edge"
    elif no_edge >= min_net_edge and 0 < no_ask <= max_entry_price + 1e-9:
        best_side = "NO"
        reason = "no_edge"
    elif yes_edge >= min_net_edge or no_edge >= min_net_edge:
        reason = "edge_but_price_above_max_or_no_liquidity"
    elif max(yes_edge, no_edge) > 0:
        reason = "edge_below_minimum"
    else:
        reason = "polymarket_already_adjusted"

    return ProbabilityEstimate(
        p_yes=p_yes,
        p_no=1.0 - p_yes,
        yes_edge=yes_edge,
        no_edge=no_edge,
        selected_side=best_side,
        reason=reason,
    )
