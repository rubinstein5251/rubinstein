"""Minimum entry price and BUY FOK limit price (shared by dashboard + execution)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from utils.clob_helpers import clamp_buy_price

_MAX_BUY = 0.99


@dataclass(frozen=True)
class MinimumEntryResolution:
    """Tick-normalized configured minimum entry price."""

    minimum_price: float
    configured_entry: float
    tick_aligned: bool
    reason: str

    @property
    def limit_price(self) -> float:
        return self.minimum_price


def resolve_minimum_entry_price(configured_entry: float) -> MinimumEntryResolution:
    configured = float(configured_entry)
    minimum = clamp_buy_price(configured)
    aligned = abs(minimum - configured) <= 0.005 + 1e-9
    reason = "ok" if aligned else "minimum_entry_adjusted_to_tick"
    return MinimumEntryResolution(
        minimum_price=minimum,
        configured_entry=configured,
        tick_aligned=aligned,
        reason=reason,
    )


# Backward-compatible alias used by worker helper name.
EntryLimitResolution = MinimumEntryResolution
resolve_entry_fok_limit = resolve_minimum_entry_price


def compute_buy_fok_limit_price(
    executable_avg: float,
    min_entry_price: float,
) -> Tuple[Optional[float], str]:
    """FOK BUY limit = executable walk price, must be >= minimum and <= 0.99."""
    raw = float(executable_avg)
    if raw <= 0:
        return None, "missing_executable_ask"
    if raw > _MAX_BUY + 1e-9:
        return None, "above_max_token_price"
    limit = clamp_buy_price(raw)
    minimum = clamp_buy_price(float(min_entry_price))
    if limit < minimum - 1e-9:
        return None, "below_minimum_entry"
    if limit > _MAX_BUY + 1e-9:
        return None, "above_max_token_price"
    return limit, "ok"
