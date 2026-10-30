"""Time-window checks for the Polymarket market-data WebSocket."""
from __future__ import annotations

import math


def listener_window_is_open(seconds_left: float, entry_window_seconds: int) -> bool:
    """Return True only for 0 < seconds_left <= entry_window_seconds.

    Float comparisons are intentional: converting to int could open the
    socket up to almost one second before the configured window begins.
    Invalid values fail closed.
    """
    try:
        left = float(seconds_left)
        window = int(entry_window_seconds)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(left) and window > 0 and 0.0 < left <= float(window)
