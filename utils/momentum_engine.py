"""Rolling momentum metrics from timestamped external price history."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Mapping, Optional, Sequence, Tuple


class MomentumDirection(Enum):
    UP = auto()
    DOWN = auto()
    NEUTRAL = auto()
    CONFLICT = auto()


@dataclass(frozen=True)
class MomentumSnapshot:
    returns: Mapping[int, float]
    realized_volatility: float
    acceleration: float
    volume_confirmed: bool
    direction: MomentumDirection
    strength: float
    confidence: float
    stale: bool
    reason: str


def _pct_return(history: Sequence[Tuple[float, float]], window_sec: float, now: float) -> Optional[float]:
    if window_sec <= 0 or not history:
        return None
    cutoff = now - window_sec
    samples = [(ts, px) for ts, px in history if ts >= cutoff and px > 0]
    if len(samples) >= 2:
        base = samples[0][1]
        last = samples[-1][1]
        if base <= 0:
            return None
        return (last - base) / base

    # Sparse ticks: bridge from last price at/before cutoff to latest print.
    before = [(ts, px) for ts, px in history if ts <= cutoff and px > 0]
    positive = [(ts, px) for ts, px in history if px > 0]
    if not positive:
        return None
    base_px = before[-1][1] if before else positive[0][1]
    last_px = positive[-1][1]
    if base_px <= 0:
        return None
    return (last_px - base_px) / base_px


def _realized_vol(history: Sequence[Tuple[float, float]], window_sec: float, now: float) -> float:
    cutoff = now - window_sec
    samples = [px for ts, px in history if ts >= cutoff and px > 0]
    if len(samples) < 3:
        return 0.0
    rets: list[float] = []
    for i in range(1, len(samples)):
        a, b = samples[i - 1], samples[i]
        if a > 0:
            rets.append((b - a) / a)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(max(0.0, var))


def _sign(v: float, eps: float) -> int:
    if v > eps:
        return 1
    if v < -eps:
        return -1
    return 0


def _evaluate_tiered_direction(
    returns: Mapping[int, float],
    *,
    trigger_w: int,
    confirm_w: int,
    support_w: int,
    context_w: int,
    trigger_min_abs_return: float,
    confirm_tolerance: float,
    support_opposing_tolerance: float,
    context_reversal_min_abs_return: float,
) -> tuple[MomentumDirection, str]:
    """5s trigger, 15s confirm, 30s support, 60s context (reversal guard only)."""
    r_trig = returns.get(trigger_w)
    r_conf = returns.get(confirm_w)
    r_sup = returns.get(support_w)
    r_ctx = returns.get(context_w)

    if r_trig is None:
        return MomentumDirection.NEUTRAL, "insufficient_trigger_window"

    eps = max(1e-12, trigger_min_abs_return * 0.25)
    trig_sign = _sign(r_trig, eps)
    if trig_sign == 0 or abs(r_trig) < trigger_min_abs_return:
        return MomentumDirection.NEUTRAL, "weak_trigger_momentum"

    if r_conf is None:
        return MomentumDirection.NEUTRAL, "insufficient_confirm_window"

    conf_sign = _sign(r_conf, confirm_tolerance)
    if conf_sign != 0 and conf_sign != trig_sign:
        return MomentumDirection.NEUTRAL, "confirm_timeframe_mismatch"
    if conf_sign == 0 and abs(r_conf) > confirm_tolerance:
        return MomentumDirection.NEUTRAL, "confirm_timeframe_mismatch"

    if r_sup is not None:
        if trig_sign > 0 and r_sup < -support_opposing_tolerance:
            return MomentumDirection.NEUTRAL, "support_timeframe_opposes"
        if trig_sign < 0 and r_sup > support_opposing_tolerance:
            return MomentumDirection.NEUTRAL, "support_timeframe_opposes"

    if r_ctx is not None:
        ctx_sign = _sign(r_ctx, eps)
        if (
            trig_sign > 0
            and ctx_sign < 0
            and abs(r_ctx) >= context_reversal_min_abs_return
        ):
            return MomentumDirection.CONFLICT, "context_reversal_against_trigger"
        if (
            trig_sign < 0
            and ctx_sign > 0
            and abs(r_ctx) >= context_reversal_min_abs_return
        ):
            return MomentumDirection.CONFLICT, "context_reversal_against_trigger"

    if trig_sign > 0:
        return MomentumDirection.UP, "momentum_up_tiered"
    return MomentumDirection.DOWN, "momentum_down_tiered"


def compute_momentum(
    history: Sequence[Tuple[float, float]],
    *,
    windows_sec: Sequence[int],
    min_strength: float,
    max_data_age_sec: float,
    last_update_ts: float,
    order_book_imbalance: float = 0.0,
    volume_confirmation_threshold: float = 0.05,
    volatility_cap: float = 0.05,
    now: Optional[float] = None,
    trigger_window_seconds: int = 5,
    confirm_window_seconds: int = 15,
    support_window_seconds: int = 30,
    context_window_seconds: int = 60,
    trigger_min_abs_return: float = 0.00005,
    confirm_tolerance: float = 0.0001,
    support_opposing_tolerance: float = 0.002,
    context_reversal_min_abs_return: float = 0.004,
    require_volume_confirmation: bool = False,
) -> MomentumSnapshot:
    now = now if now is not None else time.time()
    age = max(0.0, now - last_update_ts) if last_update_ts > 0 else 9999.0
    if age > max_data_age_sec:
        return MomentumSnapshot(
            returns={},
            realized_volatility=0.0,
            acceleration=0.0,
            volume_confirmed=False,
            direction=MomentumDirection.NEUTRAL,
            strength=0.0,
            confidence=0.0,
            stale=True,
            reason="stale_external_data",
        )

    returns: dict[int, float] = {}
    for w in windows_sec:
        r = _pct_return(history, float(w), now)
        if r is not None:
            returns[int(w)] = r

    if not returns:
        return MomentumSnapshot(
            returns={},
            realized_volatility=0.0,
            acceleration=0.0,
            volume_confirmed=False,
            direction=MomentumDirection.NEUTRAL,
            strength=0.0,
            confidence=0.0,
            stale=False,
            reason="insufficient_history",
        )

    vol_window = context_window_seconds if context_window_seconds in returns else max(windows_sec)
    vol = _realized_vol(history, float(vol_window), now)
    if vol > volatility_cap + 1e-12:
        return MomentumSnapshot(
            returns=returns,
            realized_volatility=vol,
            acceleration=0.0,
            volume_confirmed=False,
            direction=MomentumDirection.NEUTRAL,
            strength=0.0,
            confidence=0.0,
            stale=False,
            reason="excessive_volatility",
        )

    r_trig = returns.get(trigger_window_seconds, 0.0)
    r_conf = returns.get(confirm_window_seconds, r_trig)
    acceleration = r_trig - r_conf

    vol_denom = max(vol, 1e-6)
    strength = max(abs(r_trig / vol_denom), abs(r_conf / vol_denom))

    direction, reason = _evaluate_tiered_direction(
        returns,
        trigger_w=trigger_window_seconds,
        confirm_w=confirm_window_seconds,
        support_w=support_window_seconds,
        context_w=context_window_seconds,
        trigger_min_abs_return=trigger_min_abs_return,
        confirm_tolerance=confirm_tolerance,
        support_opposing_tolerance=support_opposing_tolerance,
        context_reversal_min_abs_return=context_reversal_min_abs_return,
    )

    imb_ok_up = order_book_imbalance >= volume_confirmation_threshold
    imb_ok_down = order_book_imbalance <= -volume_confirmation_threshold
    volume_check_disabled = (
        volume_confirmation_threshold <= 1e-12 or not require_volume_confirmation
    )

    if direction == MomentumDirection.UP and not volume_check_disabled and not imb_ok_up:
        direction = MomentumDirection.NEUTRAL
        reason = "volume_not_confirmed_up"
    elif direction == MomentumDirection.DOWN and not volume_check_disabled and not imb_ok_down:
        direction = MomentumDirection.NEUTRAL
        reason = "volume_not_confirmed_down"
    elif direction in (MomentumDirection.UP, MomentumDirection.DOWN) and strength < min_strength:
        direction = MomentumDirection.NEUTRAL
        reason = "weak_momentum"

    volume_confirmed = volume_check_disabled or (
        (direction == MomentumDirection.UP and imb_ok_up)
        or (direction == MomentumDirection.DOWN and imb_ok_down)
        or direction not in (MomentumDirection.UP, MomentumDirection.DOWN)
    )

    confidence = min(1.0, strength / max(min_strength, 1e-9)) * (
        0.5 + 0.5 * min(1.0, len(returns) / max(1, len(windows_sec)))
    )
    if direction == MomentumDirection.CONFLICT:
        confidence *= 0.3

    return MomentumSnapshot(
        returns=returns,
        realized_volatility=vol,
        acceleration=acceleration,
        volume_confirmed=volume_confirmed,
        direction=direction,
        strength=strength,
        confidence=confidence,
        stale=False,
        reason=reason,
    )


def momentum_reversal_detected(
    entry_direction: MomentumDirection,
    current: MomentumSnapshot,
    *,
    min_confirm_strength: float,
) -> bool:
    if entry_direction == MomentumDirection.UP:
        return (
            current.direction in (MomentumDirection.DOWN, MomentumDirection.CONFLICT)
            and current.strength >= min_confirm_strength
        )
    if entry_direction == MomentumDirection.DOWN:
        return (
            current.direction in (MomentumDirection.UP, MomentumDirection.CONFLICT)
            and current.strength >= min_confirm_strength
        )
    return False
