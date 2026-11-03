"""Paper-trading helpers: FOK feasibility against visible book depth."""

from __future__ import annotations

from typing import Optional, Tuple


def simulate_fok_buy_against_book(
    executable_avg: float,
    executable_size: float,
    limit_price: float,
    order_size: float,
    min_fill_delta: float,
) -> Tuple[float, float]:
    if executable_avg <= 0 or executable_size <= 0:
        return 0.0, 0.0
    if executable_avg > limit_price + 1e-9:
        return 0.0, 0.0
    if executable_size + 1e-9 < order_size - min_fill_delta:
        return 0.0, 0.0
    return order_size, executable_avg
