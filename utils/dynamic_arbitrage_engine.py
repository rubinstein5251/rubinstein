"""Pure decision logic for paired inventory accumulation (98¢ target, hedge, DCA, rebalance)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Optional, Tuple

from utils.inventory_cost import (
    InventorySnapshot,
    PurchaseDecision,
    evaluate_initial_hedge,
    evaluate_paired_dca_purchase,
    evaluate_rebalance_purchase,
    matched_combined_average,
    pick_cheap_side,
)
_EPS = 1e-9


class ArbitrageAction(str, Enum):
    NONE = "none"
    FIRST_LEG = "first_leg"
    IDEAL_HEDGE = "ideal_hedge"
    EMERGENCY_HEDGE = "emergency_hedge"
    AVERAGE_DOWN = "average_down"
    REBALANCE = "rebalance"


@dataclass(frozen=True)
class ArbitrageDecision:
    action: ArbitrageAction
    side: str
    qty: float
    limit_price: float
    reason: str
    projected_combined: Optional[float] = None
    paired: bool = False
    yes_limit_price: float = 0.0
    no_limit_price: float = 0.0


@dataclass(frozen=True)
class ArbitrageConfig:
    target_combined_cost: float = 0.98
    max_combined_cost: float = 0.98
    emergency_max_combined_cost: float = 1.0
    rebalance_max_combined_cost: float = 1.0
    min_net_profit_per_pair: float = 0.02
    slippage_reserve: float = 0.0
    max_slippage: float = 0.005
    cheap_entry_price: float = 0.92
    max_first_leg_price: float = 0.92
    min_order_size: float = 5.0
    fee_rate: float = 0.0
    hedge_timeout_seconds: float = 4.0
    endgame_rebalance_seconds: float = 90.0
    endgame_rebalance_max_combined_cost: float = 1.03


def _exact_max_opposite_price(primary_avg: float, target_combined: float) -> float:
    diff = Decimal(str(target_combined)) - Decimal(str(primary_avg))
    return float(diff.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _exe_tuple(exe: Optional[Tuple[float, float]]) -> Tuple[float, float]:
    if not exe:
        return 0.0, 0.0
    return float(exe[0]), float(exe[1])


def evaluate_inventory_tick(
    snap: InventorySnapshot,
    *,
    now: float,
    hedge_deadline_at: float,
    first_leg_side: str,
    yes_exe: Optional[Tuple[float, float]],
    no_exe: Optional[Tuple[float, float]],
    cfg: ArbitrageConfig,
    seconds_to_close: Optional[float] = None,
    target_achieved: bool = False,
) -> ArbitrageDecision:
    """Return the highest-priority buy action for this tick (no execution)."""
    y_px, y_liq = _exe_tuple(yes_exe)
    n_px, n_liq = _exe_tuple(no_exe)
    order_size = cfg.min_order_size
    combined = matched_combined_average(snap)

    unmatched_yes = snap.unpaired_yes
    unmatched_no = snap.unpaired_no
    has_yes_only = snap.yes_shares > _EPS and snap.no_shares <= _EPS
    has_no_only = snap.no_shares > _EPS and snap.yes_shares <= _EPS

    # ── 1) Unhedged first leg: ideal hedge, then emergency after deadline ──
    if has_yes_only or has_no_only:
        primary = "YES" if has_yes_only else "NO"
        opposite = "NO" if primary == "YES" else "YES"
        opp_px, opp_liq = (n_px, n_liq) if opposite == "NO" else (y_px, y_liq)
        hedge_qty = min(order_size, snap.yes_shares if primary == "YES" else snap.no_shares, opp_liq)
        if hedge_qty < cfg.min_order_size - _EPS:
            return ArbitrageDecision(ArbitrageAction.NONE, "", 0.0, 0.0, "insufficient_hedge_depth")

        primary_avg = snap.yes_avg() if primary == "YES" else snap.no_avg()
        max_ideal = _exact_max_opposite_price(primary_avg, cfg.target_combined_cost)
        limit_ideal = max_ideal + cfg.max_slippage

        hedge_ev = evaluate_initial_hedge(
            snap,
            opposite,
            hedge_qty,
            opp_px,
            max_slippage=cfg.max_slippage,
            limit_price=limit_ideal,
            max_combined_cost=cfg.target_combined_cost,
            min_net_profit_per_pair=cfg.min_net_profit_per_pair,
            slippage_reserve=cfg.slippage_reserve,
        )
        if hedge_ev.decision == PurchaseDecision.BUY and primary_avg + opp_px <= cfg.target_combined_cost + _EPS:
            return ArbitrageDecision(
                ArbitrageAction.IDEAL_HEDGE,
                opposite,
                hedge_qty,
                min(opp_px + cfg.max_slippage, limit_ideal),
                hedge_ev.reason,
                hedge_ev.resulting_combined,
            )

        past_deadline = hedge_deadline_at > 0 and now >= hedge_deadline_at - _EPS
        if past_deadline and opp_px > 0:
            # Close single-leg exposure after timeout — economics cap waived (may exceed $1.00).
            after = snap.with_buy(opposite, hedge_qty, opp_px)
            resulting = matched_combined_average(after)
            return ArbitrageDecision(
                ArbitrageAction.EMERGENCY_HEDGE,
                opposite,
                hedge_qty,
                min(1.0, opp_px + cfg.max_slippage),
                "force_hedge_after_timeout",
                resulting,
            )

        if not past_deadline:
            return ArbitrageDecision(
                ArbitrageAction.NONE, "", 0.0, 0.0,
                f"waiting_for_hedge max_opposite={max_ideal:.2f} ask={opp_px:.2f}",
            )
        return ArbitrageDecision(ArbitrageAction.NONE, "", 0.0, 0.0, "emergency_hedge_blocked")

    in_endgame = (
        seconds_to_close is not None
        and seconds_to_close <= cfg.endgame_rebalance_seconds + _EPS
    )
    rebalance_cap = (
        cfg.endgame_rebalance_max_combined_cost
        if in_endgame
        else cfg.rebalance_max_combined_cost
    )

    # ── 2) Share imbalance: rebalance lagging leg (combined must stay <= $1) ──
    if unmatched_yes > _EPS or unmatched_no > _EPS:
        if unmatched_yes > unmatched_no:
            lagging, gap = "NO", unmatched_yes
        else:
            lagging, gap = "YES", unmatched_no
        lag_px, lag_liq = (n_px, n_liq) if lagging == "NO" else (y_px, y_liq)
        qty = min(order_size, gap, lag_liq)
        if qty >= cfg.min_order_size - _EPS and lag_px > 0:
            rebalance_min_profit = -1.0 if in_endgame else 0.0
            ev = evaluate_rebalance_purchase(
                snap,
                lagging,
                qty,
                lag_px,
                max_combined_cost=rebalance_cap,
                min_net_profit_per_pair=rebalance_min_profit,
                slippage_reserve=cfg.slippage_reserve,
            )
            if ev.decision == PurchaseDecision.BUY:
                return ArbitrageDecision(
                    ArbitrageAction.REBALANCE,
                    lagging,
                    qty,
                    lag_px + cfg.max_slippage,
                    ev.reason,
                    ev.resulting_combined,
                )
        return ArbitrageDecision(ArbitrageAction.NONE, "", 0.0, 0.0, "rebalance_not_eligible")

    # ── 3) Balanced inventory: paired average-down (both legs, no skew) ──
    if snap.matched_pairs >= cfg.min_order_size - _EPS:
        at_target = target_achieved or (
            combined is not None and combined <= cfg.target_combined_cost + _EPS
        )
        if not at_target and y_px > 0 and n_px > 0:
            qty = min(order_size, y_liq, n_liq)
            if qty >= cfg.min_order_size - _EPS:
                ev = evaluate_paired_dca_purchase(
                    snap,
                    qty,
                    y_px,
                    n_px,
                    combined_cost_target=cfg.target_combined_cost,
                    max_combined_cost=cfg.max_combined_cost,
                    require_improvement=True,
                    min_net_profit_per_pair=0.0,
                    slippage_reserve=cfg.slippage_reserve,
                )
                if ev.decision == PurchaseDecision.BUY:
                    return ArbitrageDecision(
                        ArbitrageAction.AVERAGE_DOWN,
                        "",
                        qty,
                        0.0,
                        ev.reason,
                        ev.resulting_combined,
                        paired=True,
                        yes_limit_price=min(1.0, y_px + cfg.max_slippage),
                        no_limit_price=min(1.0, n_px + cfg.max_slippage),
                    )

    # ── 4) Flat book: first leg ──
    if snap.yes_shares <= _EPS and snap.no_shares <= _EPS:
        side, ev = pick_cheap_side(
            yes_exe if y_px <= cfg.max_first_leg_price + _EPS else None,
            no_exe if n_px <= cfg.max_first_leg_price + _EPS else None,
            cheap_entry_price=cfg.cheap_entry_price,
            max_entry_price=cfg.max_first_leg_price,
            yes_shares=0.0,
            no_shares=0.0,
        )
        if side and ev.decision == PurchaseDecision.BUY:
            px = y_px if side == "YES" else n_px
            liq = y_liq if side == "YES" else n_liq
            qty = min(order_size, liq)
            if qty >= cfg.min_order_size - _EPS:
                return ArbitrageDecision(
                    ArbitrageAction.FIRST_LEG,
                    side,
                    qty,
                    px + cfg.max_slippage,
                    ev.reason,
                    combined,
                )
        if y_px <= cfg.max_first_leg_price + _EPS or n_px <= cfg.max_first_leg_price + _EPS:
            side = "YES" if (y_px > 0 and (n_px <= 0 or y_px <= n_px)) else "NO"
            px, liq = (y_px, y_liq) if side == "YES" else (n_px, n_liq)
            if px > 0 and liq >= cfg.min_order_size - _EPS and px <= cfg.max_first_leg_price + _EPS:
                return ArbitrageDecision(
                    ArbitrageAction.FIRST_LEG,
                    side,
                    min(order_size, liq),
                    px + cfg.max_slippage,
                    "first_leg_by_best_ask",
                    None,
                )

    return ArbitrageDecision(ArbitrageAction.NONE, "", 0.0, 0.0, "no_action")


def hedge_deadline_from_fill(
    first_fill_at: float,
    *,
    hedge_delay_seconds: float,
    hedge_timeout_seconds: float,
) -> float:
    return first_fill_at + max(0.0, hedge_delay_seconds) + max(0.0, hedge_timeout_seconds)
