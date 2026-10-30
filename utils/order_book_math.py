"""Executable price from order-book levels (walk the book)."""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple


def _levels(raw: Iterable) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for row in raw or []:
        try:
            if hasattr(row, "price") and hasattr(row, "size"):
                px, sz = float(row.price), float(row.size)
            elif isinstance(row, dict):
                px, sz = float(row["price"]), float(row["size"])
            else:
                px, sz = float(row[0]), float(row[1])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if not (math.isfinite(px) and math.isfinite(sz)) or px <= 0 or sz <= 0:
            continue
        if px > 1.0:
            px /= 100.0
        out.append((px, sz))
    return out


def executable_buy_from_asks(
    asks: Iterable,
    size: float,
    *,
    require_full_size: bool = True,
) -> Optional[Tuple[float, float]]:
    """Return (volume-weighted avg price, filled qty) for a BUY walking asks."""
    try:
        target = float(size)
    except (TypeError, ValueError):
        return None
    if target <= 0 or not math.isfinite(target):
        return None
    levels = sorted(_levels(asks), key=lambda x: x[0])
    if not levels:
        return None
    cost = 0.0
    filled = 0.0
    for px, avail in levels:
        take = min(avail, target - filled)
        if take <= 0:
            break
        cost += take * px
        filled += take
        if filled >= target - 1e-9:
            break
    if filled <= 0:
        return None
    if require_full_size and filled < target - 1e-6:
        return None
    return cost / filled, filled


def executable_sell_from_bids(
    bids: Iterable,
    size: float,
    *,
    require_full_size: bool = True,
) -> Optional[Tuple[float, float]]:
    """Return (volume-weighted avg bid, filled qty) for a SELL walking bids."""
    try:
        target = float(size)
    except (TypeError, ValueError):
        return None
    if target <= 0 or not math.isfinite(target):
        return None
    levels = sorted(_levels(bids), key=lambda x: x[0], reverse=True)
    if not levels:
        return None
    proceeds = 0.0
    filled = 0.0
    for px, avail in levels:
        take = min(avail, target - filled)
        if take <= 0:
            break
        proceeds += take * px
        filled += take
        if filled >= target - 1e-9:
            break
    if filled <= 0:
        return None
    if require_full_size and filled < target - 1e-6:
        return None
    return proceeds / filled, filled


def best_ask_from_levels(asks: Iterable) -> float:
    """Lowest ask price in the book (0 if empty)."""
    levels = sorted(_levels(asks), key=lambda x: x[0])
    return levels[0][0] if levels else 0.0


def best_bid_from_levels(bids: Iterable) -> float:
    """Highest bid price in the book (0 if empty)."""
    levels = sorted(_levels(bids), key=lambda x: x[0], reverse=True)
    return levels[0][0] if levels else 0.0


def best_ask_size_from_levels(asks: Iterable) -> float:
    levels = sorted(_levels(asks), key=lambda x: x[0])
    return levels[0][1] if levels else 0.0


def best_level_fallback(side: str, price: float, size: float, book_size: float) -> Optional[Tuple[float, float]]:
    """Use top-of-book when a full depth snapshot is unavailable."""
    if price <= 0 or size <= 0:
        return None
    if book_size > 0 and book_size < size - 1e-6:
        return None
    return float(price), float(size)
