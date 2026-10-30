"""Polymarket CLOB price/order helpers."""

from __future__ import annotations

from typing import Any


def _tick(price: float, *, minimum: float = 0.01, maximum: float = 0.99) -> float:
    p = float(price)
    if p != p or p in (float("inf"), float("-inf")):
        raise ValueError(f"invalid price: {price!r}")
    # Polymarket binary-token prices are represented in one-cent increments in
    # this bot. Round to the nearest cent, then clamp to executable bounds.
    p = round(p + 1e-12, 2)
    return max(minimum, min(maximum, p))


def clamp_buy_price(price: float) -> float:
    """Return a safe binary-token BUY price in [0.01, 0.99]."""
    return _tick(price)


def clamp_sell_price(price: float) -> float:
    """Return a safe binary-token SELL price in [0.01, 0.99]."""
    return _tick(price)


def parse_order_type(order_type: Any):
    """Map a human-readable order type to py-clob-client's OrderType.

    The helper accepts the enum itself as well as strings such as ``GTC``,
    ``FOK`` and ``GTD``.  Importing is lazy so utility tests do not require the
    CLOB SDK until an actual live order is submitted.
    """
    if not isinstance(order_type, str):
        return order_type

    key = order_type.strip().upper()
    try:
        from py_clob_client_v2.clob_types import OrderType
    except ImportError:
        # Useful for static tests; the real bot has the SDK installed.
        return key

    candidates = {
        "GTC": "GTC",
        "FOK": "FOK",
        "GTD": "GTD",
    }
    name = candidates.get(key, key)
    if hasattr(OrderType, name):
        return getattr(OrderType, name)
    try:
        return OrderType(name)
    except Exception:
        raise ValueError(f"unsupported order type: {order_type!r}")
