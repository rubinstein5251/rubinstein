"""Polymarket up/down market reference price and resolution oracle hints."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from utils.external_market_data import fetch_binance_spot_open_at_ts, price_at_or_before


@dataclass(frozen=True)
class MarketReferenceContext:
    oracle_label: str
    reference_price: float
    current_price: float
    price_source: str
    synchronized: bool
    reason: str
    basis_vs_binance: float = 0.0


_CHAINLINK_HINT = re.compile(r"chainlink|data\.chain", re.I)


def infer_oracle_from_gamma(market: Mapping[str, Any]) -> str:
    """Best-effort oracle label from Gamma metadata."""
    blob = " ".join(
        str(market.get(k) or "")
        for k in ("question", "description", "resolutionSource", "resolution_source")
    )
    if _CHAINLINK_HINT.search(blob):
        return "chainlink"
    return "polymarket_crypto_oracle"


def resolve_market_reference(
    *,
    market_start_ts: int,
    interval_seconds: int,
    price_history: Sequence[Tuple[float, float]],
    current_binance_price: float,
    oracle_label: str,
    now_ts: float,
    binance_symbol: str = "",
) -> MarketReferenceContext:
    """
    Reference open price = external spot at interval start (Binance proxy).

    Polymarket BTC/ETH 5m markets typically resolve on Chainlink; until a
    dedicated Chainlink feed is wired, we require Binance spot and block entry
    when opening reference cannot be anchored from pre-start history.
    """
    open_px = price_at_or_before(price_history, float(market_start_ts))
    price_source = "binance_spot_ws"
    if (open_px is None or open_px <= 0) and binance_symbol:
        rest_open = fetch_binance_spot_open_at_ts(binance_symbol, market_start_ts)
        if rest_open and rest_open > 0:
            open_px = rest_open
            price_source = "binance_spot_kline_open"
    if open_px is None or open_px <= 0:
        return MarketReferenceContext(
            oracle_label=oracle_label,
            reference_price=0.0,
            current_price=current_binance_price,
            price_source="binance_spot_proxy",
            synchronized=False,
            reason="missing_open_reference_price",
        )
    if current_binance_price <= 0:
        return MarketReferenceContext(
            oracle_label=oracle_label,
            reference_price=open_px,
            current_price=0.0,
            price_source=price_source,
            synchronized=False,
            reason="stale_external_price",
        )
    age_from_start = now_ts - market_start_ts
    if age_from_start < 0:
        return MarketReferenceContext(
            oracle_label=oracle_label,
            reference_price=open_px,
            current_price=current_binance_price,
            price_source="binance_spot_proxy",
            synchronized=False,
            reason="market_not_started",
        )
    basis = (current_binance_price - open_px) / open_px if open_px > 0 else 0.0
    return MarketReferenceContext(
        oracle_label=oracle_label,
        reference_price=open_px,
        current_price=current_binance_price,
        price_source=price_source,
        synchronized=True,
        reason="ok",
        basis_vs_binance=basis,
    )
