"""Fail-closed filters for late-entry (final-window) strategy."""
from __future__ import annotations

import math
from typing import Iterable, Optional, Tuple


def price_velocity_cents_per_second(
    history: Iterable[Tuple[float, float]],
    now: float,
    *,
    lookback_seconds: float = 1.0,
    minimum_span_seconds: float = 0.05,
) -> Optional[float]:
    """Return absolute token-price velocity in cents/second, or None if unknown.

    Uses the earliest and latest valid observations in the recent lookback.
    Unknown/stale/insufficient history is represented by None so callers can
    fail closed rather than buying without knowing whether the market is moving
    too quickly.
    """
    try:
        now_f = float(now)
        lookback = float(lookback_seconds)
        min_span = float(minimum_span_seconds)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (now_f, lookback, min_span)) or lookback <= 0 or min_span <= 0:
        return None
    samples = []
    try:
        for ts, price in history:
            ts_f, px_f = float(ts), float(price)
            if math.isfinite(ts_f) and math.isfinite(px_f) and px_f > 0 and now_f - lookback <= ts_f <= now_f:
                samples.append((ts_f, px_f))
    except (TypeError, ValueError):
        return None
    samples.sort(key=lambda item: item[0])
    if len(samples) < 2:
        return None
    elapsed = samples[-1][0] - samples[0][0]
    if elapsed < min_span:
        return None
    return abs(samples[-1][1] - samples[0][1]) * 100.0 / elapsed


def exact_99c_entry_allowed(
    *,
    seconds_left: float,
    entry_window_seconds: int,
    live_ask: float,
    order_price: float,
    velocity_cents_per_second: Optional[float],
    max_velocity_cents_per_second: float = 5.0,
    tolerance: float = 1e-6,
) -> tuple[bool, str]:
    """Validate the requested exact-price, endgame, low-velocity BUY rule."""
    try:
        left = float(seconds_left)
        window = int(entry_window_seconds)
        ask = float(live_ask)
        px = float(order_price)
        max_velocity = float(max_velocity_cents_per_second)
    except (TypeError, ValueError):
        return False, "invalid_entry_parameters"
    if not all(math.isfinite(v) for v in (left, ask, px, max_velocity)):
        return False, "non_finite_entry_parameters"
    if window <= 0 or left <= 0 or left > window:
        return False, "outside_entry_window"
    if abs(ask - 0.99) > tolerance:
        return False, "live_ask_not_exactly_99c"
    if abs(px - 0.99) > tolerance:
        return False, "order_price_not_exactly_99c"
    if velocity_cents_per_second is None or not math.isfinite(float(velocity_cents_per_second)):
        return False, "price_velocity_unknown"
    if abs(float(velocity_cents_per_second)) >= max_velocity - 1e-9:
        return False, "price_moving_at_least_5c_per_second"
    return True, "entry_conditions_met"


def late_entry_window_open(seconds_left: float, entry_window_seconds: int) -> bool:
    try:
        left = float(seconds_left)
        window = int(entry_window_seconds)
    except (TypeError, ValueError):
        return False
    if window <= 0 or not math.isfinite(left):
        return False
    return 0 < left <= window


def late_entry_price_allowed(
    *,
    executable_price: float,
    entry_price_threshold: float,
    max_entry_price: float,
    tolerance: float = 1e-6,
) -> tuple[bool, str]:
    """Minimum threshold and maximum cap for a late-entry BUY."""
    try:
        exe = float(executable_price)
        lo = float(entry_price_threshold)
        hi = float(max_entry_price)
    except (TypeError, ValueError):
        return False, "invalid_price_parameters"
    if not all(math.isfinite(v) for v in (exe, lo, hi)):
        return False, "non_finite_price_parameters"
    if lo <= 0 or hi <= 0 or lo > hi + tolerance:
        return False, "invalid_threshold_range"
    if exe + tolerance < lo:
        return False, "executable_below_threshold"
    if exe > hi + tolerance:
        return False, "executable_above_max_entry"
    return True, "entry_price_ok"


def late_entry_allowed(
    *,
    seconds_left: float,
    entry_window_seconds: int,
    executable_price: float,
    entry_price_threshold: float,
    max_entry_price: float,
) -> tuple[bool, str]:
    if not late_entry_window_open(seconds_left, entry_window_seconds):
        return False, "outside_entry_window"
    return late_entry_price_allowed(
        executable_price=executable_price,
        entry_price_threshold=entry_price_threshold,
        max_entry_price=max_entry_price,
    )
