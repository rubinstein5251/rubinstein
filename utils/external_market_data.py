"""External exchange market data adapters (Binance spot; futures optional)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence, Tuple


class SpotFeed(Protocol):
    symbol: str
    last_trade_price: float
    last_trade_time: float
    imbalance: float
    price_delta: float

    @property
    def is_fresh(self) -> bool: ...

    def price_history(self) -> Sequence[Tuple[float, float]]: ...


@dataclass(frozen=True)
class ExternalPriceSnapshot:
    symbol: str
    price: float
    timestamp: float
    age_seconds: float
    stale: bool
    order_book_imbalance: float
    source: str = "binance_spot"


def snapshot_from_binance_signal(
    signal: Any,
    *,
    max_age_seconds: float,
    now: Optional[float] = None,
) -> Optional[ExternalPriceSnapshot]:
    if signal is None:
        return None
    now = now if now is not None else time.time()
    try:
        price = float(getattr(signal, "last_trade_price", 0) or 0)
        last_ts = float(getattr(signal, "last_trade_time", 0) or 0)
        last_update = float(getattr(signal, "last_update", 0) or 0)
        imbalance = float(getattr(signal, "imbalance", 0) or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    effective_ts = max(last_ts, last_update)
    age = max(0.0, now - effective_ts) if effective_ts > 0 else 9999.0
    fresh_attr = getattr(signal, "is_fresh", True)
    try:
        is_fresh = fresh_attr() if callable(fresh_attr) else bool(fresh_attr)
    except Exception:
        is_fresh = False
    stale = age > max_age_seconds or not is_fresh
    sym = str(getattr(signal, "symbol", "") or "").upper()
    return ExternalPriceSnapshot(
        symbol=sym,
        price=price,
        timestamp=last_ts or now,
        age_seconds=age,
        stale=stale,
        order_book_imbalance=imbalance,
    )


def price_at_or_before(
    history: Sequence[Tuple[float, float]],
    target_ts: float,
) -> Optional[float]:
    """Last price at or before target_ts (no look-ahead)."""
    best: Optional[float] = None
    for ts, px in history:
        if ts <= target_ts and px > 0:
            best = px
        elif ts > target_ts:
            break
    return best


def dedupe_history(history: Sequence[Tuple[float, float]]) -> list[Tuple[float, float]]:
    out: list[Tuple[float, float]] = []
    last_ts = -1.0
    for ts, px in sorted(history, key=lambda row: row[0]):
        if ts <= last_ts:
            continue
        if px <= 0:
            continue
        out.append((ts, px))
        last_ts = ts
    return out


_kline_open_cache: dict[tuple[str, int], float] = {}


def fetch_binance_spot_open_at_ts(binance_symbol: str, start_ts: int) -> Optional[float]:
    """
    One-minute kline open at market start (REST backfill when WS history is short).
    binance_symbol e.g. BTCUSDT.
    """
    if start_ts <= 0:
        return None
    sym = binance_symbol.upper()
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    key = (sym, int(start_ts))
    if key in _kline_open_cache:
        return _kline_open_cache[key]
    try:
        import requests

        start_ms = int(start_ts) * 1000
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={
                "symbol": sym,
                "interval": "1m",
                "startTime": start_ms,
                "limit": 1,
            },
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data or not isinstance(data, list):
            return None
        open_px = float(data[0][1])
        if open_px <= 0:
            return None
        _kline_open_cache[key] = open_px
        return open_px
    except Exception:
        return None
