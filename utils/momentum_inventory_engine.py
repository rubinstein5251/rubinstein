"""Momentum-first 5/5 inventory accumulation (pure decisions, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from utils.inventory_cost import InventorySnapshot, matched_combined_average
from utils.order_book_momentum import (
    OrderBookMomentumEvaluation,
    evaluate_order_book_momentum,
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


class EntryStatus(str, Enum):
    WAITING = "WAITING"
    ELIGIBLE = "ELIGIBLE"
    EXECUTING = "EXECUTING"
    FILLED = "FILLED"


@dataclass(frozen=True)
class MomentumInventoryConfig:
    target_shares_per_side: float = 5.0
    momentum_lookback_seconds: float = 5.0
    momentum_entry_min_price: float = 0.65
    hedge_pending_timeout_seconds: float = 60.0
    target_combined_pair_cost: float = 0.93
    max_slippage: float = 0.005
    slippage_reserve: float = 0.0
    min_order_size: float = 5.0
    max_purchase_size: float = 5.0
    min_cost_improvement: float = 0.005
    fee_rate: float = 0.0


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
    entry_status: str = EntryStatus.WAITING.value
    momentum_side: Optional[str] = None
    momentum_side_ask: float = 0.0
    max_opposite_price: Optional[float] = None
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


def max_acceptable_opposite_price(
    snap: InventorySnapshot,
    primary_side: str,
    target_combined: float,
) -> Optional[float]:
    """Required opposite ask ceiling: target combined minus primary average fill price."""
    primary = primary_side.upper()
    if primary == "YES":
        if snap.yes_shares <= _EPS:
            return None
        avg = snap.yes_avg()
    else:
        if snap.no_shares <= _EPS:
            return None
        avg = snap.no_avg()
    return float(target_combined) - avg


def _projected_combined_after_buy(
    snap: InventorySnapshot,
    side: str,
    qty: float,
    px: float,
) -> Optional[float]:
    after = snap.with_buy(side, qty, px)
    return matched_combined_average(after)


def _meets_target_combined(
    snap: InventorySnapshot,
    side: str,
    qty: float,
    px: float,
    cfg: MomentumInventoryConfig,
) -> bool:
    after = snap.with_buy(side, qty, px)
    combined = matched_combined_average(after)
    if combined is None:
        return False
    cap = cfg.target_combined_pair_cost + cfg.slippage_reserve + _EPS
    return combined <= cap


def _pick_qty(
    remaining: float,
    liq: float,
    cfg: MomentumInventoryConfig,
) -> float:
    cap = min(cfg.max_purchase_size, remaining, liq)
    if cap < cfg.min_order_size - _EPS:
        return 0.0
    return cap


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
        return after <= cfg.target_combined_pair_cost + _EPS
    return (
        after <= current - cfg.min_cost_improvement + _EPS
        or after <= cfg.target_combined_pair_cost + _EPS
    )


def _primary_side(snap: InventorySnapshot) -> str:
    if snap.yes_shares > snap.no_shares + _EPS:
        return "YES"
    if snap.no_shares > snap.yes_shares + _EPS:
        return "NO"
    return "YES" if snap.yes_shares >= snap.no_shares else "NO"


def _unpaired_on(snap: InventorySnapshot, side: str) -> float:
    if side.upper() == "YES":
        return snap.unpaired_yes
    return snap.unpaired_no


def evaluate_momentum_inventory_tick(
    snap: InventorySnapshot,
    *,
    now: float,
    yes_history,
    no_history,
    yes_exe: Optional[Tuple[float, float]],
    no_exe: Optional[Tuple[float, float]],
    cfg: MomentumInventoryConfig,
    first_fill_at: float = 0.0,
) -> InventoryTickDecision:
    target = cfg.target_shares_per_side
    y_px, y_liq = _exe(yes_exe)
    n_px, n_liq = _exe(no_exe)
    phase = _phase_for(snap, target, first_fill_at)

    momentum = evaluate_order_book_momentum(
        yes_history,
        no_history,
        now,
        lookback_sec=cfg.momentum_lookback_seconds,
    )

    hedge_elapsed = max(0.0, now - first_fill_at) if first_fill_at > 0 else 0.0
    hedge_reassess = (
        first_fill_at > 0
        and hedge_elapsed >= cfg.hedge_pending_timeout_seconds - _EPS
    )

    if phase == InventoryPhase.TARGET_REACHED:
        return InventoryTickDecision(
            InventoryBuyAction.NONE, phase, "", 0.0, 0.0, "target_reached",
            matched_combined_average(snap), momentum,
            entry_status=EntryStatus.FILLED.value,
        )

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
            if not _meets_target_combined(snap, side, qty, px, cfg):
                continue
            if not _buy_improves_cost(snap, side, qty, px, cfg):
                continue
            proj = _projected_combined_after_buy(snap, side, qty, px)
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
                entry_status=EntryStatus.WAITING.value,
                hedge_pending_elapsed=hedge_elapsed,
                hedge_timeout_reassess=hedge_reassess,
            )
        reason = "hedge_timeout_reassess_no_acceptable_price" if hedge_reassess else "waiting_favorable_price"
        return InventoryTickDecision(
            InventoryBuyAction.NONE, phase, "", 0.0, 0.0, reason,
            matched_combined_average(snap), momentum,
            hedge_pending_elapsed=hedge_elapsed,
            hedge_timeout_reassess=hedge_reassess,
        )

    if phase == InventoryPhase.STATE_2_HEDGE_PENDING:
        primary = _primary_side(snap)
        opposite = "NO" if primary == "YES" else "YES"
        opp_px, opp_liq = (n_px, n_liq) if opposite == "NO" else (y_px, y_liq)
        pri_px, pri_liq = (y_px, y_liq) if primary == "YES" else (n_px, n_liq)
        pri_rem = _remaining(snap, primary, target)
        max_opp = max_acceptable_opposite_price(snap, primary, cfg.target_combined_pair_cost)

        if pri_rem > _EPS and pri_px > 0 and pri_px >= cfg.momentum_entry_min_price - _EPS:
            qty = _pick_qty(pri_rem, pri_liq, cfg)
            if qty >= cfg.min_order_size - _EPS:
                proj = _projected_combined_after_buy(snap, primary, qty, pri_px)
                return InventoryTickDecision(
                    InventoryBuyAction.ACCUMULATE_PRIMARY,
                    phase,
                    primary,
                    qty,
                    pri_px + cfg.max_slippage,
                    "continue_primary_leg",
                    proj,
                    momentum,
                    hedge_pending_elapsed=hedge_elapsed,
                    hedge_timeout_reassess=hedge_reassess,
                )

        opp_rem = _remaining(snap, opposite, target)
        pair_cap = _unpaired_on(snap, primary)
        qty = _pick_qty(min(opp_rem, pair_cap), opp_liq, cfg)
        if (
            qty >= cfg.min_order_size - _EPS
            and opp_px > 0
            and max_opp is not None
            and opp_px <= max_opp + cfg.max_slippage + _EPS
            and _meets_target_combined(snap, opposite, qty, opp_px, cfg)
        ):
            proj = _projected_combined_after_buy(snap, opposite, qty, opp_px)
            return InventoryTickDecision(
                InventoryBuyAction.ACCUMULATE_OPPOSITE,
                phase,
                opposite,
                qty,
                min(opp_px + cfg.max_slippage, max_opp + cfg.max_slippage),
                "opposite_at_target_combined",
                proj,
                momentum,
                max_opposite_price=max_opp,
                hedge_pending_elapsed=hedge_elapsed,
                hedge_timeout_reassess=hedge_reassess,
            )
        reason = "hedge_timeout_reassess_no_acceptable_price" if hedge_reassess else "waiting_opposite_target_price"
        if max_opp is not None and opp_px > max_opp + cfg.max_slippage + _EPS:
            reason = f"opposite_above_max_{max_opp:.2f}_ask_{opp_px:.2f}"
        return InventoryTickDecision(
            InventoryBuyAction.NONE, phase, "", 0.0, 0.0, reason,
            matched_combined_average(snap), momentum,
            max_opposite_price=max_opp,
            hedge_pending_elapsed=hedge_elapsed,
            hedge_timeout_reassess=hedge_reassess,
        )

    # ── Flat book: UP or DOWN momentum side, min 65¢ executable ask ──
    mom_side = momentum.momentum_side
    mom_ask = y_px if mom_side == "YES" else n_px if mom_side == "NO" else 0.0
    if not mom_side:
        return InventoryTickDecision(
            InventoryBuyAction.NONE, InventoryPhase.IDLE, "", 0.0, 0.0,
            momentum.reason, None, momentum,
            entry_status=EntryStatus.WAITING.value,
            momentum_side=None,
            momentum_side_ask=mom_ask,
        )

    px, liq = (y_px, y_liq) if mom_side == "YES" else (n_px, n_liq)
    if px < cfg.momentum_entry_min_price - _EPS:
        return InventoryTickDecision(
            InventoryBuyAction.NONE, InventoryPhase.IDLE, "", 0.0, 0.0,
            "momentum_side_below_min_entry",
            None, momentum,
            entry_status=EntryStatus.WAITING.value,
            momentum_side=mom_side,
            momentum_side_ask=px,
        )

    rem = _remaining(snap, mom_side, target)
    qty = _pick_qty(rem, liq, cfg)
    if qty < cfg.min_order_size - _EPS or px <= 0:
        return InventoryTickDecision(
            InventoryBuyAction.NONE, InventoryPhase.IDLE, "", 0.0, 0.0,
            "insufficient_liquidity",
            None, momentum,
            entry_status=EntryStatus.WAITING.value,
            momentum_side=mom_side,
            momentum_side_ask=px,
        )

    return InventoryTickDecision(
        InventoryBuyAction.MOMENTUM_ENTRY,
        InventoryPhase.STATE_1_INITIAL_MOMENTUM,
        mom_side,
        qty,
        px + cfg.max_slippage,
        "momentum_entry_eligible",
        None,
        momentum,
        entry_status=EntryStatus.ELIGIBLE.value,
        momentum_side=mom_side,
        momentum_side_ask=px,
    )


def config_from_worker(wc) -> MomentumInventoryConfig:
    target = float(
        getattr(wc, "target_shares_per_side", 0)
        or getattr(wc, "max_shares_per_side", 0)
        or wc.max_shares
        or wc.max_position_size
    )
    target_combined = float(
        wc.target_combined_pair_cost
        or wc.combined_cost_target
        or wc.max_combined_pair_cost
        or wc.max_combined_cost
    )
    return MomentumInventoryConfig(
        target_shares_per_side=target,
        momentum_lookback_seconds=float(getattr(wc, "momentum_lookback_5s", 5.0)),
        momentum_entry_min_price=float(getattr(wc, "momentum_entry_min_price", 0.65)),
        hedge_pending_timeout_seconds=float(
            getattr(wc, "hedge_pending_timeout_seconds", 0)
            or wc.initial_hedge_timeout_seconds
        ),
        target_combined_pair_cost=target_combined,
        max_slippage=float(wc.max_slippage),
        slippage_reserve=float(wc.spread_capture_slippage_buffer),
        min_order_size=float(wc.min_order_size),
        max_purchase_size=float(
            getattr(wc, "max_purchase_size", 0) or wc.max_order_size or target
        ),
        min_cost_improvement=float(getattr(wc, "min_cost_improvement", 0.005)),
        fee_rate=float(wc.spread_capture_fee_rate),
    )
