"""Backward-compatible re-exports — use strategies.fok_execution."""

from strategies.fok_execution import fok_buy_limit as fok_buy
from strategies.fok_execution import fok_buy_limit

__all__ = ["fok_buy", "fok_buy_limit"]
