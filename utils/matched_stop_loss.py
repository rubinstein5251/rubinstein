"""Inventory-aware stop-loss scope: protect confirmed matched pairs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import MIN_SHARES
from utils.inventory_cost import InventorySnapshot, matched_combined_average


_EPS = 1e-9


@dataclass(frozen=True)
class MatchedInventoryEconomics:
    matched_quantity: float
    unmatched_yes: float
    unmatched_no: float
    yes_shares: float
    no_shares: float
    matched_combined_cost_per_pair: Optional[float]
    matched_total_cost: float
    expected_settlement_proceeds: float
    expected_gross_settlement_margin: float
    estimated_fees_on_matched: float
    expected_net_settlement_margin: float


@dataclass(frozen=True)
class StopLossScope:
    market_id: str
    primary_side: str
    economics: MatchedInventoryEconomics
    stop_loss_side: Optional[str]
    stop_loss_quantity: float
    directional_stop_eligible: bool
    suppress_reason: str

    def dashboard_fields(self) -> dict:
        e = self.economics
        return {
            "stop_loss_inventory_aware": True,
            "stop_loss_matched_quantity": round(e.matched_quantity, 4),
            "stop_loss_unmatched_yes": round(e.unmatched_yes, 4),
            "stop_loss_unmatched_no": round(e.unmatched_no, 4),
            "stop_loss_sell_quantity": round(self.stop_loss_quantity, 4),
            "stop_loss_directional_eligible": self.directional_stop_eligible,
            "stop_loss_suppress_reason": self.suppress_reason,
            "matched_pair_combined_cost": (
                round(e.matched_combined_cost_per_pair, 4)
                if e.matched_combined_cost_per_pair is not None else None
            ),
            "matched_pair_total_cost": round(e.matched_total_cost, 4),
            "matched_expected_settlement": round(e.expected_settlement_proceeds, 4),
            "matched_expected_gross_margin": round(e.expected_gross_settlement_margin, 4),
            "matched_estimated_fees": round(e.estimated_fees_on_matched, 4),
            "matched_expected_net_margin": round(e.expected_net_settlement_margin, 4),
        }


def compute_matched_economics(
    inv,
    *,
    fee_rate: float = 0.0,
) -> MatchedInventoryEconomics:
    snap = InventorySnapshot.from_ledger(inv, fee_rate=fee_rate)
    matched = snap.matched_pairs
    combined = matched_combined_average(snap)
    matched_total_cost = matched * combined if combined is not None else 0.0
    settlement = matched * 1.0
    gross_margin = settlement - matched_total_cost
    # Entry fees already in cost basis when fee_rate used in with_buy; approximate exit/settle fees
    est_fees = matched_total_cost * fee_rate * 0.5 + settlement * fee_rate * 0.5
    net_margin = gross_margin - est_fees
    return MatchedInventoryEconomics(
        matched_quantity=matched,
        unmatched_yes=snap.unpaired_yes,
        unmatched_no=snap.unpaired_no,
        yes_shares=snap.yes_shares,
        no_shares=snap.no_shares,
        matched_combined_cost_per_pair=combined,
        matched_total_cost=matched_total_cost,
        expected_settlement_proceeds=settlement,
        expected_gross_settlement_margin=gross_margin,
        estimated_fees_on_matched=est_fees,
        expected_net_settlement_margin=net_margin,
    )


def compute_stop_loss_scope(
    inv,
    *,
    market_id: str,
    primary_side: str,
    fee_rate: float = 0.0,
) -> StopLossScope:
    """Only confirmed inventory counts; pending orders are ignored by design."""
    primary = (primary_side or "").upper()
    econ = compute_matched_economics(inv, fee_rate=fee_rate)
    if primary not in ("YES", "NO"):
        return StopLossScope(
            market_id=market_id,
            primary_side=primary,
            economics=econ,
            stop_loss_side=None,
            stop_loss_quantity=0.0,
            directional_stop_eligible=False,
            suppress_reason="no_primary_side",
        )

    unmatched = econ.unmatched_yes if primary == "YES" else econ.unmatched_no
    if unmatched >= MIN_SHARES - _EPS:
        return StopLossScope(
            market_id=market_id,
            primary_side=primary,
            economics=econ,
            stop_loss_side=primary,
            stop_loss_quantity=unmatched,
            directional_stop_eligible=True,
            suppress_reason="",
        )

    if econ.matched_quantity >= MIN_SHARES - _EPS and unmatched <= _EPS:
        return StopLossScope(
            market_id=market_id,
            primary_side=primary,
            economics=econ,
            stop_loss_side=None,
            stop_loss_quantity=0.0,
            directional_stop_eligible=False,
            suppress_reason="matched_inventory_protected",
        )

    return StopLossScope(
        market_id=market_id,
        primary_side=primary,
        economics=econ,
        stop_loss_side=None,
        stop_loss_quantity=0.0,
        directional_stop_eligible=False,
        suppress_reason="unmatched_below_min_size",
    )
