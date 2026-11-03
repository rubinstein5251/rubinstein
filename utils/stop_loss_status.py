"""Stop-loss lifecycle labels for dashboard/API."""

from __future__ import annotations

from typing import Mapping

STOP_LOSS_TRIGGERED = "STOP_LOSS_TRIGGERED"
STOP_LOSS_ORDER_SUBMITTED = "STOP_LOSS_ORDER_SUBMITTED"
STOP_LOSS_ORDER_FAILED = "STOP_LOSS_ORDER_FAILED"
STOP_LOSS_PARTIAL_FILL = "STOP_LOSS_PARTIAL_FILL"
STOP_LOSS_FILLED = "STOP_LOSS_FILLED"
POSITION_CLOSED = "POSITION_CLOSED"

_DISPLAY = {
    STOP_LOSS_TRIGGERED: "Triggered",
    STOP_LOSS_ORDER_SUBMITTED: "Exit Submitted",
    STOP_LOSS_ORDER_FAILED: "Order Failed",
    STOP_LOSS_PARTIAL_FILL: "Partially Executed",
    STOP_LOSS_FILLED: "Completed",
    POSITION_CLOSED: "Completed",
    "": "Monitoring",
}


def stop_loss_status_label(raw_event: str) -> str:
    key = (raw_event or "").strip().upper()
    return _DISPLAY.get(key, "Monitoring")


def resolve_stop_loss_status(d: Mapping[str, object]) -> tuple[str, str]:
    """Return (raw_event_code, human label)."""
    raw = str(d.get("stop_loss_event") or d.get("exit_reason") or "").upper()
    state = str(d.get("strategy_state") or "").lower()
    exit_ord = str(d.get("exit_order_status") or "").upper()

    if raw in _DISPLAY:
        return raw, stop_loss_status_label(raw)
    if state == "stop_loss_triggered":
        return STOP_LOSS_TRIGGERED, stop_loss_status_label(STOP_LOSS_TRIGGERED)
    if state == "stop_loss_fok_submitted" or exit_ord == "SUBMITTED":
        return STOP_LOSS_ORDER_SUBMITTED, stop_loss_status_label(STOP_LOSS_ORDER_SUBMITTED)
    if state == "exit_partial_fill" or exit_ord == "RETRY":
        return STOP_LOSS_PARTIAL_FILL, stop_loss_status_label(STOP_LOSS_PARTIAL_FILL)
    if state == "position_closed_stop_loss":
        return POSITION_CLOSED, stop_loss_status_label(POSITION_CLOSED)
    if exit_ord == "FAILED":
        return STOP_LOSS_ORDER_FAILED, stop_loss_status_label(STOP_LOSS_ORDER_FAILED)
    if float(d.get("stop_loss_matched_quantity") or 0) > 0 and not d.get(
        "stop_loss_directional_eligible", True
    ):
        return "", "Monitoring (matched protected)"
    return "", "Monitoring"
