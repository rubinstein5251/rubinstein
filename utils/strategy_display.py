"""Human-readable strategy states for dashboard/API."""

from __future__ import annotations

from typing import Any, Mapping


def display_strategy_state(raw: str, *, stopped_out: bool, has_position: bool) -> str:
    key = (raw or "idle").strip().lower()
    if stopped_out and not has_position:
        return "MARKET BLOCKED AFTER STOP LOSS"
    mapping = {
        "idle": "WAITING FOR MOMENTUM",
        "waiting": "WAITING FOR MOMENTUM",
        "waiting_for_market_data": "WAITING FOR MARKET DATA",
        "waiting_for_momentum": "WAITING FOR MOMENTUM",
        "momentum_detected": "MOMENTUM DETECTED",
        "calculating_probability": "CALCULATING PROBABILITY",
        "waiting_for_price_adjustment": "WAITING FOR PRICE ADJUSTMENT",
        "edge_detected": "EDGE DETECTED",
        "entry_eligible": "ENTRY ELIGIBLE",
        "buy_fok_submitted": "BUY FOK SUBMITTED",
        "entry_pending": "BUY FOK SUBMITTED",
        "position_open": "POSITION OPEN",
        "take_profit_triggered": "TAKE PROFIT TRIGGERED",
        "take_profit_fok_submitted": "TAKE PROFIT FOK SUBMITTED",
        "position_closed_take_profit": "POSITION CLOSED — TAKE PROFIT",
        "stop_loss_triggered": "STOP LOSS TRIGGERED",
        "stop_loss_fok_submitted": "STOP LOSS FOK SUBMITTED",
        "exit_retry": "EXIT RETRY",
        "position_closed_stop_loss": "POSITION CLOSED — STOP LOSS",
        "position_settled": "POSITION SETTLED",
        "in_position": "POSITION OPEN",
        "market_completed": "MARKET COMPLETED",
        "entry_failed": "WAITING FOR MOMENTUM",
        "market_blocked_after_stop_loss": "MARKET BLOCKED AFTER STOP LOSS",
        "first_leg": "FIRST LEG FILL",
        "ideal_hedge": "HEDGING OPPOSITE LEG",
        "emergency_hedge": "EMERGENCY HEDGE",
        "average_down": "AVERAGING DOWN",
        "rebalance": "REBALANCING PAIRS",
        "state_1_initial_momentum": "MOMENTUM ACCUMULATION",
        "state_2_hedge_pending": "HEDGE PENDING",
        "state_3_cost_optimization": "COST OPTIMIZATION",
        "state_4_inventory_balancing": "INVENTORY BALANCING",
        "target_reached": "TARGET REACHED (5/5)",
        "momentum_entry": "MOMENTUM ENTRY (FAK)",
        "accumulate_primary": "ACCUMULATING MOMENTUM LEG",
        "accumulate_opposite": "ACCUMULATING OPPOSITE LEG",
        "cost_optimize": "COST OPTIMIZATION BUY",
        "balance": "BALANCE INVENTORY",
    }
    return mapping.get(key, key.replace("_", " ").upper())


def momentum_signal_display(d: Mapping[str, Any]) -> str:
    decision = str(d.get("entry_decision") or "").upper()
    if decision == "ENTER":
        return "Confirmed"
    reason = str(d.get("entry_block_reason") or "")
    if decision == "SKIP" and reason:
        return "Rejected"
    state = str(d.get("strategy_state") or "").lower()
    if state in {"momentum_detected", "edge_detected", "entry_eligible", "buy_fok_submitted"}:
        return "Detected"
    if str(d.get("entry_order_status") or "").upper() == "FILLED":
        return "Confirmed"
    return "—"


def enrich_dashboard_payload(d: Mapping[str, Any]) -> dict[str, Any]:
    from utils.stop_loss_status import resolve_stop_loss_status

    out = dict(d)
    out["strategy_name"] = out.get("strategy_name") or "BTC/ETH Momentum"
    out["strategy_label"] = out.get("strategy_label") or "BTC/ETH Momentum"
    out["execution_type"] = str(out.get("execution_type") or "FOK").upper()
    stopped = bool(out.get("market_stopped_out"))
    has_pos = (
        float(out.get("position_size") or 0) > 0
        or float(out.get("yes_shares") or 0) > 0
        or float(out.get("no_shares") or 0) > 0
    )
    if out.get("inventory_accumulation_enabled"):
        out["strategy_name"] = out.get("strategy_name") or "Momentum inventory 5/5"
        out["strategy_label"] = out.get("strategy_label") or "Momentum-first inventory"
        if not out.get("execution_type") or out.get("execution_type") == "FOK":
            out["execution_type"] = str(out.get("inventory_execution_type") or "FAK").upper()
    raw = str(out.get("strategy_state") or "idle")
    out["strategy_display_state"] = display_strategy_state(
        raw, stopped_out=stopped, has_position=has_pos,
    )
    out["momentum_signal_display"] = momentum_signal_display(out)
    event, stop_label = resolve_stop_loss_status(out)
    if event:
        out["stop_loss_event"] = event
    out["stop_loss_status"] = stop_label
    init_side = out.get("bought_side") or out.get("position_side")
    if init_side:
        out["initial_entry_side"] = str(init_side).upper()
    if out.get("entry_fill_price_c"):
        out["initial_entry_fill_c"] = out.get("entry_fill_price_c")
    if out.get("filled_size"):
        out["initial_entry_quantity"] = out.get("filled_size")
    score = out.get("signal_quality_score")
    if score is not None:
        out["signal_quality_display"] = f"Score {score}/{out.get('signal_quality_threshold', 75)}"
    decision = out.get("entry_decision")
    if decision:
        out["entry_decision_display"] = str(decision)
    reason = out.get("entry_block_reason")
    if reason:
        out["entry_rejection_display"] = str(reason)
    tp_pct = out.get("take_profit_pct")
    if tp_pct is not None:
        out["take_profit_pct_display"] = round(float(tp_pct) * 100, 1)
    sl_pct = out.get("stop_loss_pct")
    if sl_pct is not None:
        out["stop_loss_pct_display"] = round(float(sl_pct) * 100, 1)
    return out
