"""Central YES/NO inventory balance checks before every BUY."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Tuple

from utils.inventory_cost import InventorySnapshot, matched_combined_average

_LOG = logging.getLogger("inventory_balance")
_EPS = 1e-9
_QTY_SNAP = 1e-3


def normalize_share_qty(qty: float, *, min_order_size: float = 0.0) -> float:
    """Snap near-integer share qty (binary search can return 4.999999999 which prints as 5.00)."""
    if qty <= 0:
        return 0.0
    q = round(float(qty), 4)
    if min_order_size > 0 and abs(q - min_order_size) < _QTY_SNAP:
        return float(min_order_size)
    if abs(q - round(q)) < _QTY_SNAP:
        return float(round(q))
    return q


class BuyApproval(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class EffectiveInventory:
    """Confirmed fills plus optional pending BUY exposure."""

    yes_confirmed: float
    no_confirmed: float
    pending_yes: float = 0.0
    pending_no: float = 0.0

    @property
    def effective_yes(self) -> float:
        return self.yes_confirmed + self.pending_yes

    @property
    def effective_no(self) -> float:
        return self.no_confirmed + self.pending_no

    @property
    def matched_confirmed(self) -> float:
        return min(self.yes_confirmed, self.no_confirmed)

    @property
    def unmatched_yes_confirmed(self) -> float:
        return max(0.0, self.yes_confirmed - self.no_confirmed)

    @property
    def unmatched_no_confirmed(self) -> float:
        return max(0.0, self.no_confirmed - self.yes_confirmed)

    @property
    def share_imbalance_effective(self) -> float:
        return abs(self.effective_yes - self.effective_no)

    @property
    def share_imbalance_confirmed(self) -> float:
        return abs(self.yes_confirmed - self.no_confirmed)


@dataclass(frozen=True)
class BuySizePlan:
    approval: BuyApproval
    side: str
    requested_qty: float
    approved_qty: float
    reason: str
    effective: EffectiveInventory
    projected_yes: float
    projected_no: float
    projected_imbalance: float
    current_combined: Optional[float]
    projected_combined: Optional[float]
    underrepresented_side: Optional[str]

    def log_payload(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "yes_confirmed": round(self.effective.yes_confirmed, 4),
            "no_confirmed": round(self.effective.no_confirmed, 4),
            "pending_yes": round(self.effective.pending_yes, 4),
            "pending_no": round(self.effective.pending_no, 4),
            "effective_yes": round(self.effective.effective_yes, 4),
            "effective_no": round(self.effective.effective_no, 4),
            "matched_confirmed": round(self.effective.matched_confirmed, 4),
            "unmatched_yes": round(self.effective.unmatched_yes_confirmed, 4),
            "unmatched_no": round(self.effective.unmatched_no_confirmed, 4),
            "requested_qty": round(self.requested_qty, 4),
            "approved_qty": round(self.approved_qty, 4),
            "projected_yes": round(self.projected_yes, 4),
            "projected_no": round(self.projected_no, 4),
            "projected_imbalance": round(self.projected_imbalance, 4),
            "current_combined": self.current_combined,
            "projected_combined": self.projected_combined,
            "approval": self.approval.value,
            "reason": self.reason,
        }


def underrepresented_side(eff: EffectiveInventory) -> Optional[str]:
    if eff.effective_yes + _EPS < eff.effective_no:
        return "YES"
    if eff.effective_no + _EPS < eff.effective_yes:
        return "NO"
    return None


def _project_after_buy(eff: EffectiveInventory, side: str, qty: float) -> Tuple[float, float]:
    side = side.upper()
    y, n = eff.effective_yes, eff.effective_no
    if side == "YES":
        y += qty
    else:
        n += qty
    return y, n


def max_qty_without_increasing_imbalance(eff: EffectiveInventory, side: str, cap: float) -> float:
    """Max qty on ``side`` that does not increase effective share imbalance."""
    side = side.upper()
    if cap <= 0:
        return 0.0
    before = eff.share_imbalance_effective
    if before <= _EPS:
        # Balanced — single-leg buy always creates imbalance unless qty=0.
        return 0.0
    under = underrepresented_side(eff)
    if under is None:
        return 0.0
    if side != under:
        return 0.0
    # Buying underrepresented side: imbalance shrinks until balanced then would grow again.
    gap = abs(eff.effective_yes - eff.effective_no)
    return min(cap, gap)


def max_qty_within_imbalance_limit(
    eff: EffectiveInventory,
    side: str,
    qty: float,
    max_imbalance: float,
) -> float:
    """Clamp ``qty`` so effective imbalance after buy stays <= ``max_imbalance``."""
    if qty <= 0:
        return 0.0
    lo, hi = 0.0, qty
    best = 0.0
    for _ in range(32):
        mid = (lo + hi) / 2.0
        py, pn = _project_after_buy(eff, side, mid)
        if abs(py - pn) <= max_imbalance + _EPS:
            best = mid
            lo = mid
        else:
            hi = mid
    if best <= 0:
        return 0.0
    if abs(best - qty) < _QTY_SNAP:
        return normalize_share_qty(qty)
    return normalize_share_qty(best)


def plan_single_leg_buy(
    *,
    snap: InventorySnapshot,
    eff: EffectiveInventory,
    side: str,
    requested_qty: float,
    min_order_size: float,
    max_share_imbalance: float,
    max_unmatched_shares: float,
    balance_inventory_before_buy: bool,
    allow_unmatched_accumulation: bool,
    purpose: str,
    allow_first_leg_entry: bool = False,
) -> BuySizePlan:
    """Central sizing + balance gate for one-sided BUY proposals."""
    side = side.upper()
    req = max(0.0, float(requested_qty))
    current_combined = matched_combined_average(snap)

    # Hedge matches confirmed fills; pending hedge orders must not look "already balanced".
    if purpose == "initial_hedge":
        eff = EffectiveInventory(
            yes_confirmed=eff.yes_confirmed,
            no_confirmed=eff.no_confirmed,
            pending_yes=0.0,
            pending_no=0.0,
        )

    under = underrepresented_side(eff)
    before_imb = eff.share_imbalance_effective

    def _finish(qty: float, approval: BuyApproval, reason: str) -> BuySizePlan:
        py, pn = _project_after_buy(eff, side, qty)
        proj_snap = snap
        if qty > _EPS:
            proj_snap = snap.with_buy(side, qty, 1.0)  # price not used for share math
        # Recompute combined only when both legs have matched inventory
        proj_combined = matched_combined_average(proj_snap) if qty > 0 else current_combined
        return BuySizePlan(
            approval=approval,
            side=side,
            requested_qty=req,
            approved_qty=max(0.0, qty),
            reason=reason,
            effective=eff,
            projected_yes=py,
            projected_no=pn,
            projected_imbalance=abs(py - pn),
            current_combined=current_combined,
            projected_combined=proj_combined,
            underrepresented_side=under,
        )

    if req <= _EPS:
        return _finish(0.0, BuyApproval.REJECTED, "zero_requested_qty")

    approved = req

    if (
        allow_first_leg_entry
        and purpose in ("cheap_entry", "first_leg")
        and eff.matched_confirmed <= _EPS
        and (eff.yes_confirmed + eff.no_confirmed) <= _EPS
    ):
        approved = min(req, max(req, min_order_size))
        if max_unmatched_shares > _EPS:
            approved = min(approved, max_unmatched_shares)
        if approved < min_order_size - _EPS:
            return _finish(
                0.0,
                BuyApproval.REJECTED,
                f"SKIP BUY: first leg qty {approved:.2f} below min_order_size",
            )
        return _finish(approved, BuyApproval.APPROVED, "first_leg_approved")

    if balance_inventory_before_buy:
        if allow_first_leg_entry and purpose in ("cheap_entry", "first_leg"):
            pass
        elif purpose == "initial_hedge":
            hedge_side = under
            if hedge_side and side != hedge_side:
                return _finish(
                    0.0,
                    BuyApproval.REJECTED,
                    "SKIP BUY: hedge must buy underrepresented side",
                )
            approved = min(approved, max_qty_without_increasing_imbalance(eff, side, approved))
            if approved <= _EPS:
                return _finish(
                    0.0,
                    BuyApproval.REJECTED,
                    "SKIP BUY: opposite-side inventory is insufficient for requested quantity",
                )
        else:
            if under is None:
                return _finish(
                    0.0,
                    BuyApproval.REJECTED,
                    "SKIP BUY: balanced inventory requires paired purchase",
                )
            if side != under:
                py, pn = eff.effective_yes, eff.effective_no
                return _finish(
                    0.0,
                    BuyApproval.REJECTED,
                    f"SKIP BUY: YES={py:.2f}, NO={pn:.2f}, proposed {side}={req:.2f}, "
                    f"projected imbalance would exceed maximum YES/NO share imbalance",
                )
            approved = min(
                approved,
                max_qty_without_increasing_imbalance(eff, side, approved),
            )
            if approved <= _EPS:
                return _finish(
                    0.0,
                    BuyApproval.REJECTED,
                    "SKIP BUY: would exceed maximum YES/NO share imbalance",
                )

    if purpose == "initial_hedge":
        gap = abs(eff.yes_confirmed - eff.no_confirmed)
        approved = min(approved, gap)
        approved = normalize_share_qty(approved, min_order_size=min_order_size)
    elif balance_inventory_before_buy:
        imb_cap = max_share_imbalance
        under_now = underrepresented_side(eff)
        if (
            under_now is not None
            and side == under_now
            and before_imb > max_share_imbalance + _EPS
        ):
            imb_cap = before_imb
        approved = max_qty_within_imbalance_limit(eff, side, approved, imb_cap)
        approved = normalize_share_qty(approved, min_order_size=min_order_size)

    if not allow_unmatched_accumulation and purpose not in ("cheap_entry", "first_leg", "initial_hedge"):
        py_c, pn_c = eff.yes_confirmed, eff.no_confirmed
        proj_y, proj_n = _project_after_buy(eff, side, approved)
        proj_unmatched = max(abs(proj_y - proj_n), max(0.0, proj_y - proj_n), max(0.0, proj_n - proj_y))
        # unmatched magnitude after confirmed fills (ignore pending for limit)
        conf_y = py_c + (approved if side == "YES" else 0.0)
        conf_n = pn_c + (approved if side == "NO" else 0.0)
        unmatched_after = abs(conf_y - conf_n)
        if unmatched_after > max_unmatched_shares + _EPS and eff.matched_confirmed > _EPS:
            return _finish(
                0.0,
                BuyApproval.REJECTED,
                f"SKIP BUY: unmatched after fill {unmatched_after:.2f} > max {max_unmatched_shares:.2f}",
            )

    approved = normalize_share_qty(approved, min_order_size=min_order_size)
    if approved < min_order_size - _QTY_SNAP:
        return _finish(
            0.0,
            BuyApproval.REJECTED,
            f"SKIP BUY: approved qty {approved:.4f} below min_order_size {min_order_size:.4f}",
        )

    py, pn = _project_after_buy(eff, side, approved)
    after_imb = abs(py - pn)
    if balance_inventory_before_buy and purpose not in ("cheap_entry", "first_leg", "initial_hedge"):
        under = underrepresented_side(eff)
        if after_imb > before_imb + _EPS and (under is None or side != under):
            return _finish(
                0.0,
                BuyApproval.REJECTED,
                f"SKIP BUY: YES={eff.effective_yes:.2f}, NO={eff.effective_no:.2f}, "
                f"proposed {side}={req:.2f}, projected imbalance={after_imb:.2f}, "
                f"maximum allowed={max_share_imbalance:.2f}",
            )

    return _finish(approved, BuyApproval.APPROVED, "approved")


@dataclass(frozen=True)
class PairedBuyPlan:
    approval: BuyApproval
    qty: float
    reason: str
    yes_plan: BuySizePlan
    no_plan: BuySizePlan


def plan_paired_buy(
    *,
    snap: InventorySnapshot,
    eff: EffectiveInventory,
    requested_qty: float,
    min_order_size: float,
    max_share_imbalance: float,
    balance_inventory_before_buy: bool,
) -> PairedBuyPlan:
    """Plan equal YES+NO purchase when inventory is balanced."""
    req = max(0.0, float(requested_qty))
    reject_yes = plan_single_leg_buy(
        snap=snap,
        eff=eff,
        side="YES",
        requested_qty=req,
        min_order_size=min_order_size,
        max_share_imbalance=max_share_imbalance,
        max_unmatched_shares=0.0,
        balance_inventory_before_buy=False,
        allow_unmatched_accumulation=True,
        purpose="paired_dca",
        allow_first_leg_entry=False,
    )
    reject_no = plan_single_leg_buy(
        snap=snap,
        eff=eff,
        side="NO",
        requested_qty=req,
        min_order_size=min_order_size,
        max_share_imbalance=max_share_imbalance,
        max_unmatched_shares=0.0,
        balance_inventory_before_buy=False,
        allow_unmatched_accumulation=True,
        purpose="paired_dca",
        allow_first_leg_entry=False,
    )
    if req <= _EPS:
        plan = reject_yes
        return PairedBuyPlan(BuyApproval.REJECTED, 0.0, "zero_requested_qty", plan, plan)

    if balance_inventory_before_buy and eff.share_imbalance_effective > max_share_imbalance + _EPS:
        return PairedBuyPlan(
            BuyApproval.REJECTED,
            0.0,
            "SKIP BUY: inventory imbalanced — paired purchase not allowed until rebalanced",
            reject_yes,
            reject_no,
        )

    py2, pn2 = eff.effective_yes + req, eff.effective_no + req
    if abs(py2 - pn2) > max_share_imbalance + _EPS:
        return PairedBuyPlan(
            BuyApproval.REJECTED,
            0.0,
            f"SKIP BUY: paired qty would exceed max imbalance {max_share_imbalance:.2f}",
            reject_yes,
            reject_no,
        )

    if req < min_order_size - _EPS:
        return PairedBuyPlan(
            BuyApproval.REJECTED,
            0.0,
            f"SKIP BUY: paired qty below min_order_size {min_order_size:.2f}",
            reject_yes,
            reject_no,
        )

    yes_p = plan_single_leg_buy(
        snap=snap,
        eff=eff,
        side="YES",
        requested_qty=req,
        min_order_size=min_order_size,
        max_share_imbalance=max_share_imbalance,
        max_unmatched_shares=0.0,
        balance_inventory_before_buy=False,
        allow_unmatched_accumulation=True,
        purpose="paired_dca",
    )
    # Effective inventory after both legs (symmetric)
    eff2 = EffectiveInventory(
        yes_confirmed=eff.yes_confirmed,
        no_confirmed=eff.no_confirmed,
        pending_yes=eff.pending_yes + req,
        pending_no=eff.pending_no + req,
    )
    no_p = plan_single_leg_buy(
        snap=snap,
        eff=eff2,
        side="NO",
        requested_qty=req,
        min_order_size=min_order_size,
        max_share_imbalance=max_share_imbalance,
        max_unmatched_shares=0.0,
        balance_inventory_before_buy=False,
        allow_unmatched_accumulation=True,
        purpose="paired_dca",
    )
    if yes_p.approved_qty >= min_order_size - _EPS and no_p.approved_qty >= min_order_size - _EPS:
        return PairedBuyPlan(BuyApproval.APPROVED, req, "paired_approved", yes_p, no_p)
    return PairedBuyPlan(BuyApproval.REJECTED, 0.0, "paired_qty_rejected", yes_p, no_p)


def log_buy_plan(plan: BuySizePlan, *, market: str = "", purpose: str = "") -> None:
    payload = plan.log_payload()
    payload["market"] = market
    payload["purpose"] = purpose
    payload["ts"] = time.time()
    if plan.approval == BuyApproval.APPROVED:
        msg = (
            f"APPROVE BUY {plan.side} qty={plan.approved_qty:.2f} | "
            f"YES={payload['effective_yes']:.2f} NO={payload['effective_no']:.2f} "
            f"pending Y/N={payload['pending_yes']:.2f}/{payload['pending_no']:.2f} | "
            f"proj imb={payload['projected_imbalance']:.2f} | {purpose}"
        )
        print(msg)
    else:
        msg = (
            f"{plan.reason} | YES={payload['yes_confirmed']:.2f} NO={payload['no_confirmed']:.2f} "
            f"proposed {plan.side}={plan.requested_qty:.2f} | {purpose}"
        )
        print(msg)
    _LOG.info(json.dumps({"event": "buy_plan", **payload, "purpose": purpose, "market": market}))
