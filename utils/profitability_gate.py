"""Profitability, depth, and economics gates for inventory accumulation BUYs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from utils.inventory_cost import InventorySnapshot, matched_combined_average
from utils.pair_cost import net_edge

_EPS = 1e-9


@dataclass(frozen=True)
class BuyGateResult:
    allowed: bool
    reason: str
    projected_combined: Optional[float] = None
    projected_net_profit: Optional[float] = None
    diagnostics: Tuple[Tuple[str, Any], ...] = ()

    def log_fields(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "projected_combined": self.projected_combined,
            "projected_net_profit": self.projected_net_profit,
            **dict(self.diagnostics),
        }


def _fee_mult(fee_rate: float) -> float:
    return 1.0 + max(0.0, float(fee_rate))


def effective_leg_cost(price: float, *, fee_rate: float, slippage_reserve: float) -> float:
    px = float(price)
    if px <= 0:
        return 0.0
    return px * _fee_mult(fee_rate) + max(0.0, slippage_reserve)


def projected_net_profit_per_pair(
    combined_pair_cost: float,
    *,
    slippage_reserve: float,
) -> float:
    """Net $ profit per matched pair at settlement ($1 payout) after cost basis includes fees."""
    return net_edge(float(combined_pair_cost)) - max(0.0, slippage_reserve)


def hypothetical_pair_cost_from_legs(
    yes_px: float,
    no_px: float,
    *,
    fee_rate: float,
    slippage_reserve: float,
) -> float:
    return effective_leg_cost(yes_px, fee_rate=fee_rate, slippage_reserve=slippage_reserve / 2.0) + effective_leg_cost(
        no_px, fee_rate=fee_rate, slippage_reserve=slippage_reserve / 2.0
    )


def check_executable_depth(
    executable: Optional[Tuple[float, float]],
    qty: float,
    *,
    min_orderbook_depth: float,
) -> Optional[str]:
    if not executable:
        return "REJECT_INSUFFICIENT_DEPTH"
    _avg, filled = executable
    if filled + _EPS < float(qty):
        return "REJECT_INSUFFICIENT_DEPTH"
    if filled + _EPS < float(min_orderbook_depth):
        return "REJECT_INSUFFICIENT_DEPTH"
    return None


def check_pair_economics(
    combined: float,
    *,
    max_combined_cost: float,
    min_net_profit_per_pair: float,
    slippage_reserve: float,
) -> BuyGateResult:
    if not math.isfinite(combined) or combined <= 0:
        return BuyGateResult(False, "REJECT_INVALID_COMBINED_COST", projected_combined=combined)
    if combined > float(max_combined_cost) + _EPS:
        return BuyGateResult(
            False,
            "REJECT_COMBINED_COST_TOO_HIGH",
            projected_combined=combined,
            projected_net_profit=projected_net_profit_per_pair(combined, slippage_reserve=slippage_reserve),
        )
    net_p = projected_net_profit_per_pair(combined, slippage_reserve=slippage_reserve)
    if net_p + _EPS < float(min_net_profit_per_pair):
        return BuyGateResult(
            False,
            "REJECT_NET_PROFIT_TOO_LOW",
            projected_combined=combined,
            projected_net_profit=net_p,
        )
    return BuyGateResult(
        True,
        "approved",
        projected_combined=combined,
        projected_net_profit=net_p,
    )


def evaluate_single_leg_buy_gate(
    snap: InventorySnapshot,
    side: str,
    qty: float,
    executable_price: float,
    *,
    max_combined_cost: float,
    min_net_profit_per_pair: float,
    slippage_reserve: float,
    require_improvement: bool = False,
    combined_cost_target: Optional[float] = None,
) -> BuyGateResult:
    if qty <= _EPS or executable_price <= 0:
        return BuyGateResult(False, "REJECT_INVALID_QTY_OR_PRICE")
    after = snap.with_buy(side, qty, executable_price)
    resulting = matched_combined_average(after)
    current = matched_combined_average(snap)
    if resulting is None:
        return BuyGateResult(False, "REJECT_NO_MATCHED_PAIR", projected_combined=None)
    if require_improvement and current is not None and resulting >= current - _EPS:
        return BuyGateResult(
            False,
            "REJECT_NO_COST_IMPROVEMENT",
            projected_combined=resulting,
            projected_net_profit=projected_net_profit_per_pair(resulting, slippage_reserve=slippage_reserve),
        )
    econ = check_pair_economics(
        resulting,
        max_combined_cost=max_combined_cost,
        min_net_profit_per_pair=min_net_profit_per_pair,
        slippage_reserve=slippage_reserve,
    )
    if not econ.allowed:
        return econ
    if combined_cost_target is not None and resulting > float(combined_cost_target) + _EPS:
        if current is not None and current <= float(combined_cost_target) + _EPS:
            return BuyGateResult(
                False,
                "REJECT_TARGET_ALREADY_ACHIEVED",
                projected_combined=resulting,
                projected_net_profit=econ.projected_net_profit,
            )
    return econ


def evaluate_first_leg_gate(
    *,
    side: str,
    qty: float,
    leg_executable_price: float,
    opposite_executable_price: float,
    fee_rate: float,
    max_combined_cost: float,
    min_net_profit_per_pair: float,
    slippage_reserve: float,
    max_entry_price: float,
) -> BuyGateResult:
    if leg_executable_price <= 0 or opposite_executable_price <= 0 or qty <= _EPS:
        return BuyGateResult(False, "REJECT_INVALID_QTY_OR_PRICE")
    if leg_executable_price > float(max_entry_price) + _EPS:
        return BuyGateResult(False, "REJECT_ABOVE_MAX_ENTRY_PRICE")
    side = side.upper()
    yes_px = leg_executable_price if side == "YES" else opposite_executable_price
    no_px = opposite_executable_price if side == "YES" else leg_executable_price
    combined = hypothetical_pair_cost_from_legs(
        yes_px,
        no_px,
        fee_rate=fee_rate,
        slippage_reserve=slippage_reserve,
    )
    return check_pair_economics(
        combined,
        max_combined_cost=max_combined_cost,
        min_net_profit_per_pair=min_net_profit_per_pair,
        slippage_reserve=slippage_reserve,
    )


def evaluate_initial_hedge_gate(
    snap: InventorySnapshot,
    hedge_side: str,
    qty: float,
    hedge_executable_price: float,
    *,
    max_combined_cost: float,
    min_net_profit_per_pair: float,
    slippage_reserve: float,
) -> BuyGateResult:
    """Reject hedges that would lock in an unprofitable matched pair (e.g. YES 0.35 + NO 0.67)."""
    if qty <= _EPS or hedge_executable_price <= 0:
        return BuyGateResult(False, "REJECT_INVALID_QTY_OR_PRICE")
    after = snap.with_buy(hedge_side, qty, hedge_executable_price)
    resulting = matched_combined_average(after)
    if resulting is None:
        return BuyGateResult(False, "REJECT_NO_MATCHED_PAIR")
    return check_pair_economics(
        resulting,
        max_combined_cost=max_combined_cost,
        min_net_profit_per_pair=min_net_profit_per_pair,
        slippage_reserve=slippage_reserve,
    )


def evaluate_paired_buy_gate(
    snap: InventorySnapshot,
    qty: float,
    yes_executable_price: float,
    no_executable_price: float,
    *,
    max_combined_cost: float,
    min_net_profit_per_pair: float,
    slippage_reserve: float,
    require_improvement: bool = True,
) -> BuyGateResult:
    if qty <= _EPS or yes_executable_price <= 0 or no_executable_price <= 0:
        return BuyGateResult(False, "REJECT_INVALID_QTY_OR_PRICE")
    after = snap.with_buy("YES", qty, yes_executable_price).with_buy("NO", qty, no_executable_price)
    resulting = matched_combined_average(after)
    current = matched_combined_average(snap)
    if resulting is None:
        return BuyGateResult(False, "REJECT_NO_MATCHED_PAIR")
    if require_improvement and current is not None and resulting >= current - _EPS:
        return BuyGateResult(
            False,
            "REJECT_NO_COST_IMPROVEMENT",
            projected_combined=resulting,
        )
    return check_pair_economics(
        resulting,
        max_combined_cost=max_combined_cost,
        min_net_profit_per_pair=min_net_profit_per_pair,
        slippage_reserve=slippage_reserve,
    )
