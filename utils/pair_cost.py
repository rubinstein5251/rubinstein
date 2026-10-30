"""Pair-cost helpers for FOK hedge decisions."""

from __future__ import annotations


def combined_pair_cost(
    entry_avg: float,
    hedge_ask: float,
    *,
    fee_rate: float,
    slippage: float,
) -> float:
    hedge_px = hedge_ask + max(0.0, slippage) + hedge_ask * max(0.0, fee_rate)
    return entry_avg + hedge_px


def net_edge(pair_cost: float) -> float:
    return 1.0 - pair_cost
