"""Human-readable strategy states for dashboard/API."""

from __future__ import annotations

from typing import Any, Mapping


def display_strategy_state(raw: str, *, stopped_out: bool, has_position: bool) -> str:
    key = (raw or "idle").strip().lower()
    if stopped_out and not has_position:
        return "MARKET BLOCKED AFTER STOP LOSS"
    mapping = {
        "idle": "WAITING FOR ENTRY",
        "waiting": "WAITING FOR ENTRY",
        "entry_eligible": "ENTRY PRICE REACHED",
        "entry_pending": "BUY ORDER SUBMITTED",
        "in_position": "POSITION OPEN",
        "position_open": "POSITION OPEN",
        "stop_loss_triggered": "STOP LOSS TRIGGERED",
        "stop_loss_exit": "SELL ORDER SUBMITTED",
        "stop_loss_exit_pending": "EXIT RETRY",
        "position_closed": "POSITION CLOSED",
        "stopped_out": "POSITION CLOSED",
        "market_completed": "MARKET COMPLETED",
        "entry_failed": "WAITING FOR ENTRY",
    }
    return mapping.get(key, key.replace("_", " ").upper())


def enrich_dashboard_payload(d: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(d)
    out["strategy_name"] = "95c+ Min Entry / Stop Loss"
    out["execution_type"] = "FOK"
    stopped = bool(out.get("market_stopped_out"))
    has_pos = float(out.get("position_size") or 0) > 0
    raw = str(out.get("strategy_state") or "idle")
    out["strategy_display_state"] = display_strategy_state(
        raw, stopped_out=stopped, has_position=has_pos,
    )
    return out
