"""Order-book ask-price momentum (UP=YES, DOWN=NO)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Deque, Optional, Sequence, Tuple


@dataclass(frozen=True)
class SideMomentum:
    side: str
    delta_5s: Optional[float]
    delta_15s: Optional[float]
    delta_30s: Optional[float]
    confirmations: int

    @property
    def confirmed_up(self) -> bool:
        return self.confirmations >= 2 and all(
            d is not None and d > 0 for d in (self.delta_5s, self.delta_15s, self.delta_30s)
            if d is not None
        )


@dataclass(frozen=True)
class OrderBookMomentumEvaluation:
    yes: SideMomentum
    no: SideMomentum
    confirmed_side: Optional[str]
    reason: str
    conflict: bool


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


def _count_confirmations(
    d5: Optional[float],
    d15: Optional[float],
    d30: Optional[float],
    *,
    t5: float,
    t15: float,
    t30: float,
) -> int:
    n = 0
    if d5 is not None and d5 >= t5:
        n += 1
    if d15 is not None and d15 >= t15:
        n += 1
    if d30 is not None and d30 >= t30:
        n += 1
    return n


def evaluate_order_book_momentum(
    yes_history: Sequence[Tuple[float, float]],
    no_history: Sequence[Tuple[float, float]],
    now: float,
    *,
    lookback_5s: float = 5.0,
    lookback_15s: float = 15.0,
    lookback_30s: float = 30.0,
    threshold_5s: float = 0.01,
    threshold_15s: float = 0.02,
    threshold_30s: float = 0.03,
    confirmation_required: int = 2,
) -> OrderBookMomentumEvaluation:
    y5 = ask_delta(yes_history, now, lookback_5s)
    y15 = ask_delta(yes_history, now, lookback_15s)
    y30 = ask_delta(yes_history, now, lookback_30s)
    n5 = ask_delta(no_history, now, lookback_5s)
    n15 = ask_delta(no_history, now, lookback_15s)
    n30 = ask_delta(no_history, now, lookback_30s)

    yes_conf = _count_confirmations(y5, y15, y30, t5=threshold_5s, t15=threshold_15s, t30=threshold_30s)
    no_conf = _count_confirmations(n5, n15, n30, t5=threshold_5s, t15=threshold_15s, t30=threshold_30s)

    yes_m = SideMomentum("YES", y5, y15, y30, yes_conf)
    no_m = SideMomentum("NO", n5, n15, n30, no_conf)

    yes_ok = yes_conf >= confirmation_required
    no_ok = no_conf >= confirmation_required

    if yes_ok and no_ok:
        return OrderBookMomentumEvaluation(
            yes_m, no_m, None, "momentum_conflict_both_sides", True,
        )
    if yes_ok and not no_ok:
        if n5 is not None and n5 > threshold_5s:
            return OrderBookMomentumEvaluation(
                yes_m, no_m, None, "momentum_conflict_opposing_rising", True,
            )
        return OrderBookMomentumEvaluation(yes_m, no_m, "YES", "momentum_yes_confirmed", False)
    if no_ok and not yes_ok:
        if y5 is not None and y5 > threshold_5s:
            return OrderBookMomentumEvaluation(
                yes_m, no_m, None, "momentum_conflict_opposing_rising", True,
            )
        return OrderBookMomentumEvaluation(yes_m, no_m, "NO", "momentum_no_confirmed", False)

    return OrderBookMomentumEvaluation(yes_m, no_m, None, "momentum_unclear", False)


def momentum_still_valid(
    evaluation: OrderBookMomentumEvaluation,
    side: str,
    *,
    confirmation_required: int = 2,
) -> bool:
    side = side.upper()
    if evaluation.confirmed_side != side:
        return False
    sm = evaluation.yes if side == "YES" else evaluation.no
    return sm.confirmations >= confirmation_required


def momentum_reversed(
    history: Sequence[Tuple[float, float]],
    now: float,
    *,
    since_ts: float,
    side: str,
    reversal_threshold: float = 0.01,
) -> bool:
    """True if ask fell sharply since signal time (adverse reversal)."""
    if not history:
        return True
    recent = [(ts, px) for ts, px in history if ts >= since_ts - 0.05]
    if len(recent) < 2:
        return False
    peak = max(px for _, px in recent)
    current = float(history[-1][1])
    return (peak - current) >= reversal_threshold
