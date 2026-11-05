"""Momentum-first 10/10 inventory accumulation (pure decisions, no I/O)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from utils.inventory_cost import InventorySnapshot, matched_combined_average
from utils.order_book_momentum import (
    OrderBookMomentumEvaluation,
    evaluate_order_book_momentum,
    momentum_still_valid,
)

_EPS = 1e-9


class InventoryPhase(str, Enum):
    IDLE = "idle"
    STATE_1_INITIAL_MOMENTUM = "state_1_initial_momentum"
    STATE_2_HEDGE_PENDING = "state_2_hedge_pending"
    STATE_3_COST_OPTIMIZATION = "state_3_cost_optimization"
    STATE_4_INVENTORY_BALANCING = "state_4_inventory_balancing"
    TARGET_REACHED = "target_reached"


class InventoryBuyAction(str, Enum):
    NONE = "none"
    MOMENTUM_ENTRY = "momentum_entry"
    ACCUMULATE_PRIMARY = "accumulate_primary"
    ACCUMULATE_OPPOSITE = "accumulate_opposite"
    COST_OPTIMIZE = "cost_optimize"
    BALANCE = "balance"


@dataclass(frozen=True)
class MomentumInventoryConfig:
    target_shares_per_side: float = 10.0
    lookback_5s: float = 5.0
    lookback_15s: float = 15.0
    lookback_30s: float = 30.0
    threshold_5s: float = 0.01
    threshold_15s: float = 0.02
    threshold_30s: float = 0.03
    confirmation_required: int = 2
    stabilization_period_seconds: float = 2.0
    hedge_pending_timeout_seconds: float = 60.0
    max_combined_entry_cost: float = 0.97
    max_combined_cost: float = 0.97
    max_slippage: float = 0.005
    min_order_size: float = 5.0
    max_purchase_size: float = 10.0
    min_cost_improvement: float = 0.005
    entry_max_chase_cents: float = 0.04
    fee_rate: float = 0.0


@dataclass
class EntryFilterStatus:
    momentum: bool = False
    stabilization: bool = False
    volatility: bool = False
    combined_cost: bool = False
    liquidity: bool = False

    def as_dict(self) -> Dict[str, bool]:
        return {
            "filter_a_momentum": self.momentum,
            "filter_b_stabilization": self.stabilization,
            "filter_c_volatility": self.volatility,
            "filter_d_combined_cost": self.combined_cost,
            "filter_e_liquidity": self.liquidity,
        }


@dataclass
class StabilizationPending:
    side: str
    signal_at: float
    evaluation: OrderBookMomentumEvaluation


@dataclass(frozen=True)
class InventoryTickDecision:
    action: InventoryBuyAction
    phase: InventoryPhase
    side: str
    qty: float
    limit_price: float
    reason: str
    projected_combined: Optional[float] = None
    momentum: Optional[OrderBookMomentumEvaluation] = None
    entry_filters: EntryFilterStatus = field(default_factory=EntryFilterStatus)
    hedge_pending_elapsed: float = 0.0
    hedge_timeout_reassess: bool = False


def _exe(exe: Optional[Tuple[float, float]]) -> Tuple[float, float]:
    if not exe:
        return 0.0, 0.0
    return float(exe[0]), float(exe[1])


def _remaining(snap: InventorySnapshot, side: str, target: float) -> float:
    sh = snap.yes_shares if side.upper() == "YES" else snap.no_shares
    return max(0.0, target - sh)


def _phase_for(snap: InventorySnapshot, target: float, first_fill_at: float) -> InventoryPhase:
    if snap.yes_shares >= target - _EPS and snap.no_shares >= target - _EPS:
        return InventoryPhase.TARGET_REACHED
    if snap.yes_shares <= _EPS and snap.no_shares <= _EPS:
        return InventoryPhase.IDLE
    if first_fill_at <= 0:
        return InventoryPhase.STATE_1_INITIAL_MOMENTUM
    only_yes = snap.yes_shares > _EPS and snap.no_shares <= _EPS
    only_no = snap.no_shares > _EPS and snap.yes_shares <= _EPS
    if only_yes or only_no:
        return InventoryPhase.STATE_2_HEDGE_PENDING
    if abs(snap.yes_shares - snap.no_shares) > _EPS:
        return InventoryPhase.STATE_4_INVENTORY_BALANCING
    if snap.yes_shares < target - _EPS or snap.no_shares < target - _EPS:
        return InventoryPhase.STATE_3_COST_OPTIMIZATION
    return InventoryPhase.TARGET_REACHED


def estimate_combined_acquisition(
    snap: InventorySnapshot,
    yes_px: float,
    no_px: float,
    *,
    target: float,
) -> Optional[float]:
    """Estimated pair cost if remaining shares fill at executable asks."""
    if yes_px <= 0 or no_px <= 0:
        return None
    rem_y = _remaining(snap, "YES", target)
    rem_n = _remaining(snap, "NO", target)
    proj = snap
    if rem_y > _EPS:
        proj = proj.with_buy("YES", rem_y, yes_px)
    if rem_n > _EPS:
        proj = proj.with_buy("NO", rem_n, no_px)
    return matched_combined_average(proj)


def estimate_flat_entry_combined(yes_px: float, no_px: float) -> Optional[float]:
    if yes_px <= 0 or no_px <= 0:
        return None
    return yes_px + no_px


def _volatility_ok(momentum: OrderBookMomentumEvaluation, side: str, cfg: MomentumInventoryConfig) -> bool:
    sm = momentum.yes if side.upper() == "YES" else momentum.no
    for d in (sm.delta_5s, sm.delta_15s, sm.delta_30s):
        if d is not None and d > cfg.entry_max_chase_cents + _EPS:
            return False
    return True


def _pick_qty(
    remaining: float,
    liq: float,
    cfg: MomentumInventoryConfig,
) -> float:
    cap = min(cfg.max_purchase_size, remaining, liq)
    if cap < cfg.min_order_size - _EPS:
        return 0.0
    if cap >= cfg.min_order_size - _EPS:
        return cap
    return 0.0


def _projected_combined_after_buy(
    snap: InventorySnapshot,
    side: str,
    qty: float,
    px: float,
) -> Optional[float]:
    after = snap.with_buy(side, qty, px)
    return matched_combined_average(after)


def _buy_improves_cost(
    snap: InventorySnapshot,
    side: str,
    qty: float,
    px: float,
    cfg: MomentumInventoryConfig,
) -> bool:
    current = matched_combined_average(snap)
    after = _projected_combined_after_buy(snap, side, qty, px)
    if after is None:
        return current is None
    if current is None:
        return after <= cfg.max_combined_cost + _EPS
    return after <= current - cfg.min_cost_improvement + _EPS or after <= cfg.max_combined_cost + _EPS


def evaluate_momentum_inventory_tick(
    snap: InventorySnapshot,
    *,
    now: float,
    yes_history,
    no_history,
    yes_exe: Optional[Tuple[float, float]],
    no_exe: Optional[Tuple[float, float]],
    cfg: MomentumInventoryConfig,
    primary_side: str = "",
    first_fill_at: float = 0.0,
    stabilization: Optional[StabilizationPending] = None,
) -> InventoryTickDecision:
    target = cfg.target_shares_per_side
    y_px, y_liq = _exe(yes_exe)
    n_px, n_liq = _exe(no_exe)
    phase = _phase_for(snap, target, first_fill_at)
    empty_filters = EntryFilterStatus()

    if phase == InventoryPhase.TARGET_REACHED:
        return InventoryTickDecision(
            InventoryBuyAction.NONE, phase, "", 0.0, 0.0, "target_reached",
            matched_combined_average(snap),
        )

    momentum = evaluate_order_book_momentum(
        yes_history,
        no_history,
        now,
        lookback_5s=cfg.lookback_5s,
        lookback_15s=cfg.lookback_15s,
        lookback_30s=cfg.lookback_30s,
        threshold_5s=cfg.threshold_5s,
        threshold_15s=cfg.threshold_15s,
        threshold_30s=cfg.threshold_30s,
        confirmation_required=cfg.confirmation_required,
    )

    hedge_elapsed = max(0.0, now - first_fill_at) if first_fill_at > 0 else 0.0
    hedge_reassess = (
        first_fill_at > 0
        and hedge_elapsed >= cfg.hedge_pending_timeout_seconds - _EPS
    )

    # ── Balanced partial inventory: cost optimize + top-up toward target ──
    if phase in (
        InventoryPhase.STATE_3_COST_OPTIMIZATION,
        InventoryPhase.STATE_4_INVENTORY_BALANCING,
    ):
        candidates = []
        for side, px, liq in (("YES", y_px, y_liq), ("NO", n_px, n_liq)):
            rem = _remaining(snap, side, target)
            if rem <= _EPS:
                continue
            qty = _pick_qty(rem, liq, cfg)
            if qty <= 0 or px <= 0:
                continue
            proj = _projected_combined_after_buy(snap, side, qty, px)
            if proj is not None and proj > cfg.max_combined_cost + _EPS:
                continue
            if not _buy_improves_cost(snap, side, qty, px, cfg):
                continue
            action = (
                InventoryBuyAction.BALANCE
                if phase == InventoryPhase.STATE_4_INVENTORY_BALANCING
                else InventoryBuyAction.COST_OPTIMIZE
            )
            candidates.append((proj or 999.0, side, qty, px, action))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            _proj, side, qty, px, action = candidates[0]
            return InventoryTickDecision(
                action,
                phase,
                side,
                qty,
                px + cfg.max_slippage,
                "favorable_fill",
                _proj,
                momentum,
                EntryFilterStatus(
                    momentum=True,
                    stabilization=True,
                    volatility=True,
                    combined_cost=True,
                    liquidity=True,
                ),
                hedge_elapsed,
                hedge_reassess,
            )
        reason = "hedge_timeout_reassess_no_acceptable_price" if hedge_reassess else "waiting_favorable_price"
        return InventoryTickDecision(
            InventoryBuyAction.NONE, phase, "", 0.0, 0.0, reason,
            matched_combined_average(snap), momentum,
            hedge_pending_elapsed=hedge_elapsed,
            hedge_timeout_reassess=hedge_reassess,
        )

    # ── Single-leg exposure: complete primary or hedge opposite ──
    if phase == InventoryPhase.STATE_2_HEDGE_PENDING:
        primary = "YES" if snap.yes_shares > snap.no_shares else "NO"
        opposite = "NO" if primary == "YES" else "YES"
        opp_px, opp_liq = (n_px, n_liq) if opposite == "NO" else (y_px, y_liq)
        pri_px, pri_liq = (y_px, y_liq) if primary == "YES" else (n_px, n_liq)
        pri_rem = _remaining(snap, primary, target)

        # Still building momentum leg toward target when price acceptable
        if pri_rem > _EPS and pri_px > 0:
            qty = _pick_qty(pri_rem, pri_liq, cfg)
            proj = _projected_combined_after_buy(snap, primary, qty, pri_px)
            est = estimate_combined_acquisition(snap, y_px, n_px, target=target)
            if (
                qty >= cfg.min_order_size - _EPS
                and (est is None or est <= cfg.max_combined_entry_cost + _EPS)
                and (proj is None or proj <= cfg.max_combined_cost + _EPS)
            ):
                return InventoryTickDecision(
                    InventoryBuyAction.ACCUMULATE_PRIMARY,
                    phase,
                    primary,
                    qty,
                    pri_px + cfg.max_slippage,
                    "continue_primary_leg",
                    proj,
                    momentum,
                    EntryFilterStatus(True, True, True, True, True),
                    hedge_elapsed,
                    hedge_reassess,
                )

        opp_rem = _remaining(snap, opposite, target)
        qty = _pick_qty(min(opp_rem, snap.yes_shares if primary == "YES" else snap.no_shares), opp_liq, cfg)
        if qty >= cfg.min_order_size - _EPS and opp_px > 0:
            proj = _projected_combined_after_buy(snap, opposite, qty, opp_px)
            if proj is not None and proj <= cfg.max_combined_cost + _EPS:
                return InventoryTickDecision(
                    InventoryBuyAction.ACCUMULATE_OPPOSITE,
                    phase,
                    opposite,
                    qty,
                    opp_px + cfg.max_slippage,
                    "opposite_at_acceptable_cost",
                    proj,
                    momentum,
                    EntryFilterStatus(True, True, True, True, True),
                    hedge_elapsed,
                    hedge_reassess,
                )
        reason = "hedge_timeout_reassess_no_acceptable_price" if hedge_reassess else "waiting_opposite_price"
        return InventoryTickDecision(
            InventoryBuyAction.NONE, phase, "", 0.0, 0.0, reason,
            matched_combined_average(snap), momentum,
            hedge_pending_elapsed=hedge_elapsed,
            hedge_timeout_reassess=hedge_reassess,
        )

    # ── Flat book: momentum-first entry ──
    filters = EntryFilterStatus()
    if not momentum.confirmed_side:
        filters.momentum = False
        return InventoryTickDecision(
            InventoryBuyAction.NONE, InventoryPhase.IDLE, "", 0.0, 0.0,
            momentum.reason, None, momentum, filters,
        )
    side = momentum.confirmed_side
    filters.momentum = True

    if not _volatility_ok(momentum, side, cfg):
        filters.volatility = False
        return InventoryTickDecision(
            InventoryBuyAction.NONE, InventoryPhase.IDLE, "", 0.0, 0.0,
            "excessive_volatility", None, momentum, filters,
        )
    filters.volatility = True

    pending = stabilization
    if pending is None or pending.side != side:
        return InventoryTickDecision(
            InventoryBuyAction.NONE, InventoryPhase.IDLE, "", 0.0, 0.0,
            "awaiting_stabilization",
            None, momentum, EntryFilterStatus(True, False, True, False, False),
        )

    elapsed = now - pending.signal_at
    if elapsed < cfg.stabilization_period_seconds - _EPS:
        return InventoryTickDecision(
            InventoryBuyAction.NONE, InventoryPhase.IDLE, "", 0.0, 0.0,
            f"stabilizing_{elapsed:.1f}s",
            None, momentum,
            EntryFilterStatus(True, False, True, False, False),
        )

    if not momentum_still_valid(momentum, side, confirmation_required=cfg.confirmation_required):
        return InventoryTickDecision(
            InventoryBuyAction.NONE, InventoryPhase.IDLE, "", 0.0, 0.0,
            "stabilization_signal_lost",
            None, momentum,
            EntryFilterStatus(True, False, True, False, False),
        )
    filters.stabilization = True

    px, liq = (y_px, y_liq) if side == "YES" else (n_px, n_liq)
    flat_combined = estimate_flat_entry_combined(y_px, n_px)
    if flat_combined is not None and flat_combined > cfg.max_combined_entry_cost + _EPS:
        filters.combined_cost = False
        return InventoryTickDecision(
            InventoryBuyAction.NONE, InventoryPhase.IDLE, "", 0.0, 0.0,
            "combined_entry_cost_too_high",
            flat_combined, momentum, filters,
        )
    filters.combined_cost = True

    rem = _remaining(snap, side, target)
    qty = _pick_qty(rem, liq, cfg)
    if qty < cfg.min_order_size - _EPS or px <= 0:
        filters.liquidity = False
        return InventoryTickDecision(
            InventoryBuyAction.NONE, InventoryPhase.IDLE, "", 0.0, 0.0,
            "insufficient_liquidity",
            flat_combined, momentum, filters,
        )
    filters.liquidity = True

    return InventoryTickDecision(
        InventoryBuyAction.MOMENTUM_ENTRY,
        InventoryPhase.STATE_1_INITIAL_MOMENTUM,
        side,
        qty,
        px + cfg.max_slippage,
        "momentum_entry_ready",
        flat_combined,
        momentum,
        filters,
    )


def config_from_worker(wc) -> MomentumInventoryConfig:
    target = float(getattr(wc, "target_shares_per_side", 0) or wc.max_shares or wc.max_position_size)
    max_entry = float(
        getattr(wc, "max_combined_entry_cost", 0)
        or wc.max_combined_pair_cost
        or wc.max_combined_cost
    )
    return MomentumInventoryConfig(
        target_shares_per_side=target,
        lookback_5s=float(getattr(wc, "momentum_lookback_5s", 5)),
        lookback_15s=float(getattr(wc, "momentum_lookback_15s", 15)),
        lookback_30s=float(getattr(wc, "momentum_lookback_30s", 30)),
        threshold_5s=float(getattr(wc, "momentum_threshold_5s", 0.01)),
        threshold_15s=float(getattr(wc, "momentum_threshold_15s", 0.02)),
        threshold_30s=float(getattr(wc, "momentum_threshold_30s", 0.03)),
        confirmation_required=int(getattr(wc, "momentum_confirmation_required", 2)),
        stabilization_period_seconds=float(getattr(wc, "stabilization_period_seconds", 2.0)),
        hedge_pending_timeout_seconds=float(
            getattr(wc, "hedge_pending_timeout_seconds", 0)
            or wc.initial_hedge_timeout_seconds
        ),
        max_combined_entry_cost=max_entry,
        max_combined_cost=float(wc.max_combined_pair_cost or wc.max_combined_cost),
        max_slippage=float(wc.max_slippage),
        min_order_size=float(wc.min_order_size),
        max_purchase_size=float(getattr(wc, "max_purchase_size", 0) or wc.max_order_size or target),
        min_cost_improvement=float(getattr(wc, "min_cost_improvement", 0.005)),
        entry_max_chase_cents=float(getattr(wc, "entry_max_chase_cents", 0.04)),
        fee_rate=float(wc.spread_capture_fee_rate),
    )
