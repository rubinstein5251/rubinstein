"""Matched-pair accumulation: opposite-side buy to reach target combined cost."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from utils.clob_helpers import clamp_buy_price
from utils.inventory_cost import InventorySnapshot, matched_combined_average


_EPS = 1e-9


@dataclass(frozen=True)
class PairAccumulationState:
    primary_side: str
    primary_avg_cost: float
    opposite_side: str
    opposite_ask: float
    max_opposite_price: float
    max_opposite_price_c: float
    target_combined_cost: float
    unmatched_quantity: float
    matched_pairs: float
    combined_if_filled: Optional[float]
    all_in_combined_if_filled: Optional[float]
    expected_net_pair_margin: Optional[float]
    eligible: bool
    reason: str


def max_opposite_price_before_fees(
    primary_avg_cost: float,
    target_combined_cost: float,
) -> float:
    """Conservative max opposite price (floor tick) so token costs stay <= target."""
    if primary_avg_cost >= target_combined_cost - _EPS:
        return 0.0
    raw = target_combined_cost - primary_avg_cost
    # Floor to 2 decimal cents (Polymarket 0.01 tick)
    floored = math.floor(raw * 100.0 - 1e-9) / 100.0
    return max(0.0, min(1.0, floored))


def evaluate_pair_accumulation(
    snap: InventorySnapshot,
    *,
    primary_side: str,
    opposite_executable_price: float,
    opposite_executable_qty: float,
    target_combined_cost: float,
    min_net_profit_per_pair: float,
    fee_rate: float,
    slippage_buffer: float,
) -> PairAccumulationState:
    primary = primary_side.upper()
    opposite = "NO" if primary == "YES" else "YES"
    primary_avg = snap.yes_avg() if primary == "YES" else snap.no_avg()
    unmatched = snap.unpaired_yes if primary == "YES" else snap.unpaired_no
    max_px = max_opposite_price_before_fees(primary_avg, target_combined_cost)

    if unmatched <= _EPS:
        return PairAccumulationState(
            primary, primary_avg, opposite, opposite_executable_price,
            max_px, round(max_px * 100, 2), target_combined_cost,
            0.0, snap.matched_pairs, None, None, None, False, "fully_matched",
        )
    if primary_avg >= target_combined_cost - _EPS:
        return PairAccumulationState(
            primary, primary_avg, opposite, opposite_executable_price,
            max_px, round(max_px * 100, 2), target_combined_cost,
            unmatched, snap.matched_pairs, None, None, None, False,
            "primary_cost_above_target",
        )
    if opposite_executable_price <= 0:
        return PairAccumulationState(
            primary, primary_avg, opposite, 0.0, max_px, round(max_px * 100, 2),
            target_combined_cost, unmatched, snap.matched_pairs, None, None, None,
            False, "no_opposite_quote",
        )

    limit_cap = clamp_buy_price(max_px)
    exe_px = opposite_executable_price
    if exe_px > limit_cap + _EPS:
        return PairAccumulationState(
            primary, primary_avg, opposite, exe_px, limit_cap, round(limit_cap * 100, 2),
            target_combined_cost, unmatched, snap.matched_pairs,
            primary_avg + exe_px, None, None, False, "opposite_price_above_max",
        )

    qty = min(unmatched, opposite_executable_qty)
    if qty <= _EPS:
        return PairAccumulationState(
            primary, primary_avg, opposite, exe_px, limit_cap, round(limit_cap * 100, 2),
            target_combined_cost, unmatched, snap.matched_pairs, None, None, None,
            False, "insufficient_opposite_depth",
        )

    after = snap.with_buy(opposite, qty, exe_px)
    combined = matched_combined_average(after)
    if combined is None:
        return PairAccumulationState(
            primary, primary_avg, opposite, exe_px, limit_cap, round(limit_cap * 100, 2),
            target_combined_cost, unmatched, snap.matched_pairs, None, None, None,
            False, "pair_not_formed",
        )

    fee_mult = 1.0 + max(0.0, fee_rate)
    all_in = combined + slippage_buffer + (fee_rate * combined * fee_mult)
    settlement = 1.0
    net_margin = settlement - all_in

    if combined > target_combined_cost + _EPS:
        return PairAccumulationState(
            primary, primary_avg, opposite, exe_px, limit_cap, round(limit_cap * 100, 2),
            target_combined_cost, unmatched, after.matched_pairs, combined, all_in, net_margin,
            False, "combined_cost_above_target",
        )
    if net_margin + _EPS < min_net_profit_per_pair:
        return PairAccumulationState(
            primary, primary_avg, opposite, exe_px, limit_cap, round(limit_cap * 100, 2),
            target_combined_cost, unmatched, after.matched_pairs, combined, all_in, net_margin,
            False, "net_pair_margin_below_minimum",
        )

    return PairAccumulationState(
        primary, primary_avg, opposite, exe_px, limit_cap, round(limit_cap * 100, 2),
        target_combined_cost, unmatched, after.matched_pairs, combined, all_in, net_margin,
        True, "pair_accumulation_eligible",
    )
