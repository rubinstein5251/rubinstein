"""Matched-pair cost basis and purchase simulation for inventory accumulation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


_EPS = 1e-9


@dataclass(frozen=True)
class InventorySnapshot:
    yes_shares: float
    no_shares: float
    yes_cost: float
    no_cost: float
    fee_rate: float = 0.0

    @classmethod
    def from_ledger(cls, inv, *, fee_rate: float = 0.0) -> "InventorySnapshot":
        return cls(
            yes_shares=float(inv.yes_shares),
            no_shares=float(inv.no_shares),
            yes_cost=float(inv.yes_cost),
            no_cost=float(inv.no_cost),
            fee_rate=float(fee_rate),
        )

    @property
    def matched_pairs(self) -> float:
        return min(self.yes_shares, self.no_shares)

    @property
    def unpaired_yes(self) -> float:
        return max(0.0, self.yes_shares - self.no_shares)

    @property
    def unpaired_no(self) -> float:
        return max(0.0, self.no_shares - self.yes_shares)

    def yes_avg(self) -> float:
        return self.yes_cost / self.yes_shares if self.yes_shares > _EPS else 0.0

    def no_avg(self) -> float:
        return self.no_cost / self.no_shares if self.no_shares > _EPS else 0.0

    def total_exposure_usd(self) -> float:
        return self.yes_cost + self.no_cost

    def with_buy(self, side: str, qty: float, price: float) -> "InventorySnapshot":
        q = float(qty)
        px = float(price)
        if q <= 0 or px <= 0:
            return self
        fee_mult = 1.0 + max(0.0, self.fee_rate)
        cost = q * px * fee_mult
        if side.upper() == "YES":
            return InventorySnapshot(
                self.yes_shares + q,
                self.no_shares,
                self.yes_cost + cost,
                self.no_cost,
                self.fee_rate,
            )
        return InventorySnapshot(
            self.yes_shares,
            self.no_shares + q,
            self.yes_cost,
            self.no_cost + cost,
            self.fee_rate,
        )


def matched_combined_average(snap: InventorySnapshot) -> Optional[float]:
    """Average acquisition cost per matched pair (YES avg + NO avg)."""
    if snap.matched_pairs <= _EPS:
        return None
    return snap.yes_avg() + snap.no_avg()


class PurchaseDecision(str, Enum):
    BUY = "BUY"
    SKIP = "SKIP"


@dataclass(frozen=True)
class PurchaseEvaluation:
    decision: PurchaseDecision
    reason: str
    executable_price: float
    resulting_combined: Optional[float]
    current_combined: Optional[float]
    target_achieved: bool = False


def evaluate_cheap_entry(
    executable_price: float,
    *,
    cheap_entry_price: float,
    max_entry_price: float,
) -> PurchaseEvaluation:
    px = float(executable_price)
    if px > cheap_entry_price + _EPS:
        return PurchaseEvaluation(
            PurchaseDecision.SKIP, "above_cheap_entry_threshold", px, None, None,
        )
    if px > max_entry_price + _EPS:
        return PurchaseEvaluation(
            PurchaseDecision.SKIP, "above_max_entry_price", px, None, None,
        )
    return PurchaseEvaluation(PurchaseDecision.BUY, "cheap_entry_ok", px, None, None)


def evaluate_initial_hedge(
    snap: InventorySnapshot,
    opposite_side: str,
    qty: float,
    executable_price: float,
    *,
    max_slippage: float,
    limit_price: float,
    max_combined_cost: float = 1.0,
    min_net_profit_per_pair: float = 0.0,
    slippage_reserve: float = 0.0,
) -> PurchaseEvaluation:
    from utils.profitability_gate import evaluate_initial_hedge_gate

    if qty <= _EPS:
        return PurchaseEvaluation(PurchaseDecision.SKIP, "zero_hedge_qty", executable_price, None, None)
    if executable_price <= 0:
        return PurchaseEvaluation(PurchaseDecision.SKIP, "no_executable_hedge_price", 0.0, None, None)
    if limit_price + _EPS < executable_price:
        return PurchaseEvaluation(PurchaseDecision.SKIP, "hedge_limit_below_executable", executable_price, None, None)
    gate = evaluate_initial_hedge_gate(
        snap,
        opposite_side,
        qty,
        executable_price,
        max_combined_cost=max_combined_cost,
        min_net_profit_per_pair=min_net_profit_per_pair,
        slippage_reserve=slippage_reserve,
    )
    current = matched_combined_average(snap)
    if not gate.allowed:
        return PurchaseEvaluation(
            PurchaseDecision.SKIP,
            gate.reason,
            executable_price,
            gate.projected_combined,
            current,
        )
    return PurchaseEvaluation(
        PurchaseDecision.BUY,
        "initial_hedge_allowed",
        executable_price,
        gate.projected_combined,
        current,
    )


def evaluate_dca_purchase(
    snap: InventorySnapshot,
    side: str,
    qty: float,
    executable_price: float,
    *,
    combined_cost_target: float,
    max_combined_cost: float,
    require_improvement: bool = True,
    min_net_profit_per_pair: float = 0.0,
    slippage_reserve: float = 0.0,
) -> PurchaseEvaluation:
    if qty <= _EPS or executable_price <= 0:
        return PurchaseEvaluation(PurchaseDecision.SKIP, "invalid_dca_qty_or_price", executable_price, None, None)

    current = matched_combined_average(snap)
    after = snap.with_buy(side, qty, executable_price)
    resulting = matched_combined_average(after)

    if resulting is None:
        return PurchaseEvaluation(
            PurchaseDecision.SKIP, "dca_does_not_create_matched_pair", executable_price, None, current,
        )

    if require_improvement and current is not None:
        if resulting >= current - _EPS:
            return PurchaseEvaluation(
                PurchaseDecision.SKIP, "no_cost_improvement", executable_price, resulting, current,
            )

    if resulting > max_combined_cost + _EPS:
        if current is None or resulting >= current - _EPS:
            return PurchaseEvaluation(
                PurchaseDecision.SKIP, "REJECT_COMBINED_COST_TOO_HIGH", executable_price, resulting, current,
            )

    from utils.profitability_gate import projected_net_profit_per_pair

    net_p = projected_net_profit_per_pair(resulting, slippage_reserve=slippage_reserve)
    if net_p + _EPS < min_net_profit_per_pair:
        return PurchaseEvaluation(
            PurchaseDecision.SKIP,
            "REJECT_NET_PROFIT_TOO_LOW",
            executable_price,
            resulting,
            current,
        )

    target_hit = resulting <= combined_cost_target + _EPS
    if target_hit and current is not None and current <= combined_cost_target + _EPS:
        if resulting >= current - _EPS:
            return PurchaseEvaluation(
                PurchaseDecision.SKIP, "target_already_achieved_no_improvement",
                executable_price, resulting, current, target_achieved=True,
            )

    return PurchaseEvaluation(
        PurchaseDecision.BUY,
        "dca_improves_cost",
        executable_price,
        resulting,
        current,
        target_achieved=target_hit,
    )


def evaluate_paired_dca_purchase(
    snap: InventorySnapshot,
    qty: float,
    yes_executable_price: float,
    no_executable_price: float,
    *,
    combined_cost_target: float,
    max_combined_cost: float,
    require_improvement: bool = True,
    min_net_profit_per_pair: float = 0.0,
    slippage_reserve: float = 0.0,
) -> PurchaseEvaluation:
    """Evaluate buying equal qty on YES and NO (matched pair expansion)."""
    if qty <= _EPS or yes_executable_price <= 0 or no_executable_price <= 0:
        return PurchaseEvaluation(
            PurchaseDecision.SKIP, "invalid_paired_dca_qty_or_price", 0.0, None, None,
        )
    current = matched_combined_average(snap)
    after = snap.with_buy("YES", qty, yes_executable_price).with_buy(
        "NO", qty, no_executable_price,
    )
    resulting = matched_combined_average(after)
    if resulting is None:
        return PurchaseEvaluation(
            PurchaseDecision.SKIP, "paired_dca_no_matched_pairs", 0.0, None, current,
        )
    if require_improvement and current is not None:
        if resulting >= current - _EPS:
            return PurchaseEvaluation(
                PurchaseDecision.SKIP, "no_cost_improvement", yes_executable_price, resulting, current,
            )
    if resulting > max_combined_cost + _EPS:
        if current is None or resulting >= current - _EPS:
            return PurchaseEvaluation(
                PurchaseDecision.SKIP, "REJECT_COMBINED_COST_TOO_HIGH", yes_executable_price, resulting, current,
            )
    from utils.profitability_gate import projected_net_profit_per_pair

    net_p = projected_net_profit_per_pair(resulting, slippage_reserve=slippage_reserve)
    if net_p + _EPS < min_net_profit_per_pair:
        return PurchaseEvaluation(
            PurchaseDecision.SKIP,
            "REJECT_NET_PROFIT_TOO_LOW",
            yes_executable_price,
            resulting,
            current,
        )
    target_hit = resulting <= combined_cost_target + _EPS
    return PurchaseEvaluation(
        PurchaseDecision.BUY,
        "paired_dca_improves_cost",
        yes_executable_price + no_executable_price,
        resulting,
        current,
        target_achieved=target_hit,
    )


def pick_cheap_side(
    yes_exe: Optional[Tuple[float, float]],
    no_exe: Optional[Tuple[float, float]],
    *,
    cheap_entry_price: float,
    max_entry_price: float,
    yes_shares: float,
    no_shares: float,
) -> Tuple[Optional[str], PurchaseEvaluation]:
    """Pick whichever leg (YES/NO) has the best cheap entry — not a fixed side.

    Both UP (YES) and DOWN (NO) are evaluated each tick. Preference order:
    lowest executable price, then deeper liquidity, then the leg that reduces
    inventory skew (not alphabetical side).
    """
    candidates = []
    for side, exe in (("YES", yes_exe), ("NO", no_exe)):
        if not exe:
            continue
        avg, liq = exe
        ev = evaluate_cheap_entry(avg, cheap_entry_price=cheap_entry_price, max_entry_price=max_entry_price)
        if ev.decision == PurchaseDecision.BUY:
            imbalance = abs(yes_shares - no_shares)
            skew_after = abs(
                (yes_shares + (1.0 if side == "YES" else 0.0))
                - (no_shares + (1.0 if side == "NO" else 0.0))
            )
            candidates.append((side, avg, liq, imbalance, skew_after))

    if not candidates:
        return None, PurchaseEvaluation(PurchaseDecision.SKIP, "no_cheap_side", 0.0, None, None)

    candidates.sort(key=lambda row: (row[1], -row[2], row[4], row[3]))
    side, avg, _, _, _ = candidates[0]
    return side, evaluate_cheap_entry(avg, cheap_entry_price=cheap_entry_price, max_entry_price=max_entry_price)
