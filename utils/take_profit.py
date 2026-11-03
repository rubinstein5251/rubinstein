"""Percentage take-profit price helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from utils.clob_helpers import clamp_sell_price

_MAX_TOKEN_PRICE = 1.0


@dataclass(frozen=True)
class TakeProfitResolution:
    trigger_price: float
    raw_target: float
    reachable: bool
    reason: str


def take_profit_price(entry_price: float, take_profit_pct: float) -> TakeProfitResolution:
    """Compute tick-normalized take-profit trigger from average entry fill."""
    entry = float(entry_price)
    pct = float(take_profit_pct)
    if entry <= 0 or entry != entry:
        return TakeProfitResolution(0.0, 0.0, False, "invalid_entry_price")
    if pct <= 0 or pct != pct:
        return TakeProfitResolution(0.0, 0.0, False, "invalid_take_profit_pct")

    raw = entry * (1.0 + pct)
    if raw > _MAX_TOKEN_PRICE + 1e-9:
        return TakeProfitResolution(
            raw, raw, False, "take_profit_above_max_token_price",
        )

    ticked = clamp_sell_price(raw)
    if abs(ticked - raw) > 0.005 + 1e-9:
        return TakeProfitResolution(ticked, raw, False, "take_profit_unreachable_after_tick")

    return TakeProfitResolution(ticked, raw, True, "ok")


def take_profit_triggered(best_bid: float, trigger_price: float) -> bool:
    try:
        bid = float(best_bid)
        trigger = float(trigger_price)
    except (TypeError, ValueError):
        return False
    if trigger <= 0 or bid <= 0:
        return False
    return bid >= trigger - 1e-9


def validate_take_profit_for_entry(
    expected_entry_price: float,
    take_profit_pct: float,
) -> Tuple[bool, str]:
    res = take_profit_price(expected_entry_price, take_profit_pct)
    return res.reachable, res.reason


def approaching_take_profit(best_bid: float, trigger_price: float, *, within: float = 0.02) -> bool:
    try:
        bid = float(best_bid)
        trigger = float(trigger_price)
        band = max(0.0, float(within))
    except (TypeError, ValueError):
        return False
    if trigger <= 0 or bid <= 0:
        return False
    return trigger - band <= bid < trigger - 1e-9
