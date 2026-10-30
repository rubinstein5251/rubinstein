"""Falling-knife detection and stabilization (testable, worker-agnostic)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class KnifeSideState:
    block_until: float = 0.0
    latched: bool = False
    session_low: float = 0.0
    consecutive_buys: int = 0

    def to_metadata(self) -> dict:
        return {
            "block_until": self.block_until,
            "latched": self.latched,
            "session_low": self.session_low,
            "consecutive_buys": self.consecutive_buys,
        }

    @classmethod
    def from_metadata(cls, raw: dict) -> "KnifeSideState":
        return cls(
            block_until=float(raw.get("block_until") or 0.0),
            latched=bool(raw.get("latched")),
            session_low=float(raw.get("session_low") or 0.0),
            consecutive_buys=int(raw.get("consecutive_buys") or 0),
        )


@dataclass
class FallingKnifeState:
    sides: Dict[str, KnifeSideState] = field(
        default_factory=lambda: {"YES": KnifeSideState(), "NO": KnifeSideState()}
    )

    def metadata_blob(self) -> dict:
        return {side: st.to_metadata() for side, st in self.sides.items()}

    @classmethod
    def from_metadata(cls, raw: Optional[dict]) -> "FallingKnifeState":
        out = cls()
        if not raw:
            return out
        for side in ("YES", "NO"):
            if side in raw and isinstance(raw[side], dict):
                out.sides[side] = KnifeSideState.from_metadata(raw[side])
        return out


def _price_at_or_before(
    history: Iterable[Tuple[float, float]], now: float, seconds: float,
) -> Optional[float]:
    target = now - seconds
    candidates = [px for ts, px in history if ts <= target]
    return candidates[-1] if candidates else None


def percent_drop(from_px: float, to_px: float) -> float:
    if from_px <= 0 or to_px <= 0:
        return 0.0
    return max(0.0, (from_px - to_px) / from_px)


def check_stabilized(
    history: List[Tuple[float, float]],
    ask: float,
    *,
    stabilization_window_seconds: float,
    stabilization_tolerance_percent: float,
) -> bool:
    """True when price has not set a new low and movement is within tolerance."""
    if ask <= 0 or stabilization_window_seconds <= 0:
        return False
    if len(history) < 2:
        return False
    now = history[-1][0] if history else time.time()
    window = [px for ts, px in history if ts >= now - stabilization_window_seconds]
    if not window:
        return False
    window_low = min(window)
    if ask < window_low - 1e-9:
        return False
    ref = window[0]
    if ref <= 0:
        return False
    move = abs(ask - ref) / ref
    return move <= max(0.0, stabilization_tolerance_percent) + 1e-9


def evaluate_falling_knife(
    *,
    side: str,
    ask: float,
    history: List[Tuple[float, float]],
    state: FallingKnifeState,
    enabled: bool,
    window_seconds: float,
    fast_window_seconds: float,
    max_price_drop_fraction: float,
    fast_drop_fraction: float,
    cooldown_seconds: float,
    recovery_cents: float,
    stabilization_window_seconds: float,
    stabilization_tolerance_percent: float,
    max_consecutive_buys: int,
    now: Optional[float] = None,
) -> Tuple[bool, str, FallingKnifeState]:
    """Return (block_buy, reason_code, updated_state)."""
    side = side.upper()
    st = state.sides.setdefault(side, KnifeSideState())
    if not enabled or ask <= 0:
        return False, "ok", state
    now_f = float(now if now is not None else time.time())

    if max_consecutive_buys > 0 and st.consecutive_buys >= max_consecutive_buys:
        return True, "REJECT_MAX_CONSECUTIVE_BUYS", state

    if now_f < st.block_until:
        if not check_stabilized(
            history,
            ask,
            stabilization_window_seconds=stabilization_window_seconds,
            stabilization_tolerance_percent=stabilization_tolerance_percent,
        ):
            return True, "REJECT_NOT_STABILIZED", state
        if st.latched:
            return True, "REJECT_NOT_STABILIZED", state

    low = st.session_low if st.session_low > 0 else ask
    st.session_low = min(low, ask)
    recovered = ask >= st.session_low + (recovery_cents / 100.0)
    if st.latched and not recovered:
        return True, "REJECT_FALLING_KNIFE_COOLDOWN", state
    if recovered:
        st.block_until = 0.0
        st.latched = False
        st.session_low = ask

    if len(history) < 3:
        return False, "ok", state

    old = _price_at_or_before(history, now_f, window_seconds)
    fast_old = _price_at_or_before(history, now_f, fast_window_seconds)
    window_drop = percent_drop(old, ask) if old is not None else 0.0
    fast_drop = percent_drop(fast_old, ask) if fast_old is not None else 0.0

    recent = [px for ts, px in history if ts >= now_f - min(window_seconds, 2.0)] + [ask]
    down_moves = sum(1 for a, b in zip(recent, recent[1:]) if b < a - 0.002)
    monotone = len(recent) >= 4 and down_moves >= max(2, len(recent) - 2)

    local_fast = fast_drop >= fast_drop_fraction
    local_sustained = window_drop >= max_price_drop_fraction and monotone
    if not (local_fast or local_sustained):
        return False, "ok", state

    st.block_until = now_f + max(0.0, cooldown_seconds)
    st.latched = True
    st.session_low = min(st.session_low, ask)
    return True, "REJECT_FALLING_KNIFE_COOLDOWN", state


def record_buy_on_side(state: FallingKnifeState, side: str, *, paired: bool = False) -> None:
    side = side.upper()
    if paired:
        for s in state.sides.values():
            s.consecutive_buys = 0
        return
    for name, st in state.sides.items():
        if name == side:
            st.consecutive_buys += 1
        else:
            st.consecutive_buys = 0
