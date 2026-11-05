"""Order-book ask-price momentum (UP=YES, DOWN=NO)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple


@dataclass(frozen=True)
class SideMomentum:
    side: str
    delta: Optional[float]


@dataclass(frozen=True)
class OrderBookMomentumEvaluation:
    yes: SideMomentum
    no: SideMomentum
    momentum_side: Optional[str]
    reason: str

    @property
    def confirmed_side(self) -> Optional[str]:
        """Alias for dashboard / strategy compatibility."""
        return self.momentum_side


def _price_at_or_before(
    history: Sequence[Tuple[float, float]],
    now: float,
    lookback_sec: float,
) -> Optional[float]:
    target = now - lookback_sec
    candidates = [px for ts, px in history if ts <= target]
    if candidates:
        return float(candidates[-1])
    if history:
        return float(history[0][1])
    return None


def ask_delta(
    history: Sequence[Tuple[float, float]],
    now: float,
    lookback_sec: float,
) -> Optional[float]:
    if not history:
        return None
    current = float(history[-1][1])
    old = _price_at_or_before(history, now, lookback_sec)
    if old is None:
        return None
    return current - old


def evaluate_order_book_momentum(
    yes_history: Sequence[Tuple[float, float]],
    no_history: Sequence[Tuple[float, float]],
    now: float,
    *,
    lookback_sec: float = 5.0,
) -> OrderBookMomentumEvaluation:
    """Pick the side with upward ask movement over ``lookback_sec`` (strictly positive delta)."""
    y_d = ask_delta(yes_history, now, lookback_sec)
    n_d = ask_delta(no_history, now, lookback_sec)
    yes_m = SideMomentum("YES", y_d)
    no_m = SideMomentum("NO", n_d)

    yes_up = y_d is not None and y_d > 0
    no_up = n_d is not None and n_d > 0

    if yes_up and no_up:
        if y_d is not None and n_d is not None and y_d >= n_d:
            return OrderBookMomentumEvaluation(yes_m, no_m, "YES", "momentum_yes_stronger")
        return OrderBookMomentumEvaluation(yes_m, no_m, "NO", "momentum_no_stronger")
    if yes_up:
        return OrderBookMomentumEvaluation(yes_m, no_m, "YES", "momentum_yes_up")
    if no_up:
        return OrderBookMomentumEvaluation(yes_m, no_m, "NO", "momentum_no_up")

    return OrderBookMomentumEvaluation(yes_m, no_m, None, "no_upward_momentum")
