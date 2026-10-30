"""Execution-window and order-risk helpers.

These functions intentionally stay small and deterministic so they can be
used by both the momentum and three-engine execution paths.
"""

from __future__ import annotations

import math


def entry_window_ok(
    seconds_left: float,
    *,
    entry_seconds_left: float,
    min_entry_seconds_left: float = 0.0,
) -> bool:
    """Return True when a new entry is inside the configured market window.

    ``entry_seconds_left`` is the maximum number of seconds remaining at which
    entries are allowed.  A non-positive value disables that upper bound.
    ``min_entry_seconds_left`` protects against entering too close to expiry.
    """
    try:
        left = float(seconds_left)
        upper = float(entry_seconds_left)
        lower = float(min_entry_seconds_left)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(left):
        return False
    if left <= 0 or left < lower:
        return False
    if upper > 0 and left > upper:
        return False
    return True


def is_order_fully_filled(
    requested: float,
    filled: float,
    min_fill_delta: float = 0.0,
) -> bool:
    try:
        req = float(requested)
        got = float(filled)
        tol = max(0.0, float(min_fill_delta))
    except (TypeError, ValueError):
        return False
    if req <= 0:
        return False
    return got >= max(0.0, req - tol)


def stop_loss_price(entry_price: float, stop_loss_pct: float) -> float:
    entry = float(entry_price)
    pct = max(0.0, float(stop_loss_pct))
    return max(0.0, min(1.0, entry * (1.0 - pct)))


def stop_loss_triggered(current_price: float, entry_price: float, stop_loss_pct: float) -> bool:
    try:
        current = float(current_price)
        stop = stop_loss_price(entry_price, stop_loss_pct)
    except (TypeError, ValueError):
        return False
    return current <= stop


def is_market_locked(
    yes_price: float,
    no_price: float,
    *,
    is_locked=None,
    low: float = 0.01,
    high: float = 1.00,
) -> bool:
    """Return True when either market leg is at a terminal/locked price.

    ``strategies.momentum`` can pass the bot's existing ``is_locked_price``
    callback so the utility follows the exact price-lock convention used by
    the worker.  When no callback is supplied, the default is 1c/100c.
    """
    try:
        yes = float(yes_price)
        no = float(no_price)
    except (TypeError, ValueError):
        return False

    if not (math.isfinite(yes) and math.isfinite(no)):
        return False

    if is_locked is not None:
        try:
            return bool(is_locked(yes)) or bool(is_locked(no))
        except (TypeError, ValueError):
            return False

    return yes <= float(low) or yes >= float(high) or no <= float(low) or no >= float(high)
