"""Binance BTC short-window move detection for signal-triggered entries."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class BtcSignalEvent:
    direction: str  # UP -> buy YES, DOWN -> buy NO
    pct_move: float
    window_ms: float
    price: float
    detected_at: float
    exchange_event_ms: Optional[float] = None


def _pct_change(history: Sequence[Tuple[float, float]], window_sec: float, now: float) -> Optional[float]:
    if window_sec <= 0 or not history:
        return None
    cutoff = now - window_sec
    samples = [(ts, px) for ts, px in history if ts >= cutoff and px > 0]
    if len(samples) < 2:
        return None
    base_px = samples[0][1]
    last_px = samples[-1][1]
    if base_px <= 0:
        return None
    return (last_px - base_px) / base_px


def detect_btc_signal(
    signal: Any,
    *,
    windows_ms: Iterable[float],
    up_threshold_pct: float,
    down_threshold_pct: float,
    max_data_age_ms: float,
    now: Optional[float] = None,
) -> Optional[BtcSignalEvent]:
    """Return a directional signal when any window exceeds configured move thresholds."""
    if signal is None:
        return None
    now = now if now is not None else time.time()
    try:
        last_update = float(getattr(signal, "last_trade_time", 0) or getattr(signal, "last_update", 0))
        price = float(getattr(signal, "last_trade_price", 0) or 0)
        history = list(getattr(signal, "_price_history", []) or [])
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    age_ms = (now - last_update) * 1000.0 if last_update > 0 else 999_999.0
    if age_ms > max_data_age_ms:
        return None
    if not getattr(signal, "is_fresh", True):
        return None

    best: Optional[BtcSignalEvent] = None
    for w_ms in windows_ms:
        w_sec = max(0.001, float(w_ms) / 1000.0)
        pct = _pct_change(history, w_sec, now)
        if pct is None:
            continue
        if pct >= up_threshold_pct:
            cand = BtcSignalEvent(
                direction="UP",
                pct_move=pct,
                window_ms=float(w_ms),
                price=price,
                detected_at=now,
            )
            if best is None or abs(cand.pct_move) > abs(best.pct_move):
                best = cand
        elif pct <= -down_threshold_pct:
            cand = BtcSignalEvent(
                direction="DOWN",
                pct_move=pct,
                window_ms=float(w_ms),
                price=price,
                detected_at=now,
            )
            if best is None or abs(cand.pct_move) > abs(best.pct_move):
                best = cand
    return best


def parse_signal_windows_ms(raw: Any, default: Optional[List[float]] = None) -> List[float]:
    default = default or [1000.0, 3000.0, 5000.0]
    if raw is None:
        return list(default)
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw if float(x) > 0]
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return [float(p) for p in parts] if parts else list(default)
    try:
        return [float(raw)]
    except (TypeError, ValueError):
        return list(default)
