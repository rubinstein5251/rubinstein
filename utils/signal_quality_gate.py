"""Explainable signal-quality gate for momentum entries (0–100 score + hard rejects)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Mapping, Optional, Sequence, Tuple

from utils.market_reference import MarketReferenceContext
from utils.momentum_engine import MomentumDirection, MomentumSnapshot
from utils.probability_model import ProbabilityEstimate
from utils.take_profit import take_profit_price


class SignalDecision(Enum):
    ENTER = auto()
    REJECT = auto()


@dataclass(frozen=True)
class SignalQualityWeights:
    momentum_alignment: float = 25.0
    persistence_acceleration: float = 20.0
    net_edge: float = 25.0
    liquidity_spread: float = 15.0
    reference_time: float = 15.0

    def normalized(self) -> "SignalQualityWeights":
        total = (
            self.momentum_alignment
            + self.persistence_acceleration
            + self.net_edge
            + self.liquidity_spread
            + self.reference_time
        )
        if total <= 0:
            return SignalQualityWeights()
        return SignalQualityWeights(
            momentum_alignment=100.0 * self.momentum_alignment / total,
            persistence_acceleration=100.0 * self.persistence_acceleration / total,
            net_edge=100.0 * self.net_edge / total,
            liquidity_spread=100.0 * self.liquidity_spread / total,
            reference_time=100.0 * self.reference_time / total,
        )


@dataclass(frozen=True)
class SignalQualityConfig:
    min_score: float = 75.0
    weights: SignalQualityWeights = field(default_factory=SignalQualityWeights)
    gate_windows_seconds: Tuple[int, ...] = (5, 15, 30)
    min_short_long_agreement: bool = True
    max_spread: float = 0.06
    max_estimated_slippage: float = 0.03
    max_token_move_since_open: float = 0.12
    min_persistence_ratio: float = 0.55
    reversal_acceleration_threshold: float = -0.0005
    min_net_edge: float = 0.05
    max_entry_price: float = 0.92
    take_profit_pct: float = 0.10
    fee_rate: float = 0.0
    slippage_buffer: float = 0.02


@dataclass(frozen=True)
class SignalQualityComponents:
    momentum_alignment: float = 0.0
    persistence_acceleration: float = 0.0
    net_edge: float = 0.0
    liquidity_spread: float = 0.0
    reference_time: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "momentum_alignment": round(self.momentum_alignment, 2),
            "persistence_acceleration": round(self.persistence_acceleration, 2),
            "net_edge": round(self.net_edge, 2),
            "liquidity_spread": round(self.liquidity_spread, 2),
            "reference_time": round(self.reference_time, 2),
        }


@dataclass(frozen=True)
class SignalQualityResult:
    decision: SignalDecision
    total_score: float
    components: SignalQualityComponents
    reason: str
    side: Optional[str] = None
    net_edge: float = 0.0
    gross_edge: float = 0.0
    executable_ask: float = 0.0
    spread: float = 0.0
    estimated_slippage: float = 0.0
    mandatory_reject: bool = False

    def log_payload(self) -> dict:
        w = SignalQualityConfig().weights  # placeholder; caller adds thresholds
        return {
            "decision": self.decision.name,
            "total_score": round(self.total_score, 2),
            "components": self.components.as_dict(),
            "reason": self.reason,
            "side": self.side,
            "net_edge": round(self.net_edge, 4),
            "gross_edge": round(self.gross_edge, 4),
            "executable_ask": round(self.executable_ask, 4),
            "spread": round(self.spread, 4),
            "estimated_slippage": round(self.estimated_slippage, 4),
            "mandatory_reject": self.mandatory_reject,
        }


def _clamp_score(x: float) -> float:
    return max(0.0, min(100.0, x))


def _tiered_trigger_confirm_conflict(
    returns: Mapping[int, float],
    gate_windows: Sequence[int],
    direction: MomentumDirection,
) -> bool:
    """Only hard-reject when trigger (first) and confirm (second) windows oppose."""
    if direction not in (MomentumDirection.UP, MomentumDirection.DOWN):
        return True
    if len(gate_windows) < 2:
        return False
    trigger_w, confirm_w = int(gate_windows[0]), int(gate_windows[1])
    r0 = returns.get(trigger_w)
    r1 = returns.get(confirm_w)
    if r0 is None or r1 is None:
        return False
    if direction == MomentumDirection.UP:
        return r0 < 0 or r1 < -1e-9
    return r0 > 0 or r1 > 1e-9


def _momentum_faded(
    momentum: MomentumSnapshot,
    min_persistence: float,
    gate_windows: Sequence[int],
) -> bool:
    if momentum.direction not in (MomentumDirection.UP, MomentumDirection.DOWN):
        return True
    returns = momentum.returns
    if not returns:
        return True
    keys = [w for w in gate_windows if w in returns] or list(returns.keys())
    aligned = 0
    for w in keys:
        r = returns[w]
        if momentum.direction == MomentumDirection.UP and r > 0:
            aligned += 1
        elif momentum.direction == MomentumDirection.DOWN and r < 0:
            aligned += 1
    if not keys:
        return True
    return (aligned / len(keys)) < min_persistence


def _score_momentum_alignment(momentum: MomentumSnapshot, conflict: bool) -> float:
    if momentum.stale or conflict:
        return 0.0
    if momentum.direction not in (MomentumDirection.UP, MomentumDirection.DOWN):
        return 15.0
    votes = len(momentum.returns)
    aligned = sum(
        1
        for r in momentum.returns.values()
        if (momentum.direction == MomentumDirection.UP and r > 0)
        or (momentum.direction == MomentumDirection.DOWN and r < 0)
    )
    vote_ratio = aligned / max(1, votes)
    return _clamp_score(40.0 + 60.0 * vote_ratio * momentum.confidence)


def _score_persistence(momentum: MomentumSnapshot, cfg: SignalQualityConfig) -> float:
    if momentum.direction not in (MomentumDirection.UP, MomentumDirection.DOWN):
        return 0.0
    accel = momentum.acceleration
    if momentum.direction == MomentumDirection.UP and accel < cfg.reversal_acceleration_threshold:
        return 20.0
    if momentum.direction == MomentumDirection.DOWN and accel > -cfg.reversal_acceleration_threshold:
        return 20.0
    strength_part = min(1.0, momentum.strength / max(cfg.min_net_edge * 10, 0.5))
    accel_part = min(1.0, abs(accel) / max(momentum.realized_volatility, 1e-6))
    return _clamp_score(30.0 + 35.0 * strength_part + 35.0 * accel_part)


def _score_net_edge(net_edge: float, min_edge: float) -> float:
    if net_edge <= 0:
        return 0.0
    if net_edge < min_edge:
        return _clamp_score(50.0 * net_edge / max(min_edge, 1e-9))
    excess = min(0.15, net_edge - min_edge)
    return _clamp_score(70.0 + excess / 0.15 * 30.0)


def _score_liquidity(spread: float, slippage: float, depth_ok: bool, cfg: SignalQualityConfig) -> float:
    if not depth_ok:
        return 0.0
    spread_penalty = min(1.0, spread / max(cfg.max_spread, 1e-9))
    slip_penalty = min(1.0, slippage / max(cfg.max_estimated_slippage, 1e-9))
    return _clamp_score(100.0 * (1.0 - 0.6 * spread_penalty - 0.4 * slip_penalty))


def _score_reference_time(
    reference: MarketReferenceContext,
    seconds_remaining: float,
    interval_seconds: int,
    min_time_remaining: float,
) -> float:
    if not reference.synchronized:
        return 0.0
    if seconds_remaining < min_time_remaining:
        return 10.0
    frac = seconds_remaining / max(interval_seconds, 1)
    time_part = min(1.0, frac / 0.5)
    sync_part = 1.0 if reference.synchronized else 0.0
    return _clamp_score(40.0 + 60.0 * 0.5 * (time_part + sync_part))


def evaluate_signal_quality(
    *,
    momentum: MomentumSnapshot,
    reference: MarketReferenceContext,
    probability: Optional[ProbabilityEstimate],
    side: Optional[str],
    yes_ask: float,
    no_ask: float,
    yes_bid: float,
    no_bid: float,
    executable_vwap: float,
    executable_filled: float,
    order_size: float,
    seconds_remaining: float,
    interval_seconds: int,
    min_time_remaining: float,
    token_ask_at_open: Optional[float],
    cfg: SignalQualityConfig,
    quote_stale: bool = False,
) -> SignalQualityResult:
    """Hard rejects first; score is informational only when not mandatorily rejected."""

    empty_components = SignalQualityComponents()

    def reject(reason: str, *, mandatory: bool = True, partial: Optional[SignalQualityComponents] = None) -> SignalQualityResult:
        comp = partial or empty_components
        return SignalQualityResult(
            decision=SignalDecision.REJECT,
            total_score=0.0,
            components=comp,
            reason=reason,
            mandatory_reject=mandatory,
        )

    if quote_stale:
        return reject("REJECT_STALE_ORDERBOOK")
    if momentum.stale:
        return reject(momentum.reason or "stale_external_data")
    if not reference.synchronized:
        return reject(reference.reason or "reference_not_synchronized")
    if seconds_remaining < min_time_remaining:
        return reject("insufficient_time_remaining")
    if momentum.direction == MomentumDirection.CONFLICT:
        return reject("conflicting_timeframes")
    if momentum.direction == MomentumDirection.NEUTRAL:
        return reject(momentum.reason or "weak_momentum")
    if momentum.reason == "excessive_volatility":
        return reject("excessive_volatility")

    conflict = _tiered_trigger_confirm_conflict(
        momentum.returns, cfg.gate_windows_seconds, momentum.direction,
    )
    if cfg.min_short_long_agreement and conflict:
        return reject("trigger_confirm_conflict")

    if _momentum_faded(momentum, cfg.min_persistence_ratio, cfg.gate_windows_seconds):
        return reject("momentum_faded")

    if probability is None or side is None:
        return reject("probability_unavailable")

    ask = yes_ask if side == "YES" else no_ask
    bid = yes_bid if side == "YES" else no_bid
    if ask <= 0 or executable_vwap <= 0:
        return reject("missing_executable_ask")

    spread = max(0.0, ask - bid) if bid > 0 else cfg.max_spread
    if spread > cfg.max_spread + 1e-9:
        return reject("spread_too_wide")

    estimated_slippage = max(0.0, executable_vwap - ask)
    if estimated_slippage > cfg.max_estimated_slippage + 1e-9:
        return reject("estimated_slippage_too_high")

    if executable_filled + 1e-9 < order_size:
        return reject("REJECT_INSUFFICIENT_DEPTH")

    p_side = probability.p_yes if side == "YES" else probability.p_no
    gross_edge = p_side - executable_vwap
    total_cost = cfg.fee_rate + cfg.slippage_buffer + estimated_slippage
    net_edge = gross_edge - total_cost

    if net_edge + 1e-9 < cfg.min_net_edge:
        return reject("edge_below_minimum")

    if executable_vwap > cfg.max_entry_price + 1e-9:
        return reject("above_max_entry_price")

    tp = take_profit_price(executable_vwap, cfg.take_profit_pct)
    if not tp.reachable:
        return reject("take_profit_unreachable")

    if token_ask_at_open is not None and token_ask_at_open > 0:
        move = abs(ask - token_ask_at_open)
        if move > cfg.max_token_move_since_open + 1e-9 and net_edge < cfg.min_net_edge * 1.5:
            return reject("polymarket_already_adjusted")

    comp_raw = SignalQualityComponents(
        momentum_alignment=_score_momentum_alignment(momentum, conflict),
        persistence_acceleration=_score_persistence(momentum, cfg),
        net_edge=_score_net_edge(net_edge, cfg.min_net_edge),
        liquidity_spread=_score_liquidity(
            spread, estimated_slippage, depth_ok=True, cfg=cfg,
        ),
        reference_time=_score_reference_time(
            reference, seconds_remaining, interval_seconds, min_time_remaining,
        ),
    )
    w = cfg.weights.normalized()
    total = (
        comp_raw.momentum_alignment * w.momentum_alignment / 100.0
        + comp_raw.persistence_acceleration * w.persistence_acceleration / 100.0
        + comp_raw.net_edge * w.net_edge / 100.0
        + comp_raw.liquidity_spread * w.liquidity_spread / 100.0
        + comp_raw.reference_time * w.reference_time / 100.0
    )
    total = round(total, 2)

    if total + 1e-9 < cfg.min_score:
        return SignalQualityResult(
            decision=SignalDecision.REJECT,
            total_score=total,
            components=comp_raw,
            reason="signal_score_below_minimum",
            side=side,
            net_edge=net_edge,
            gross_edge=gross_edge,
            executable_ask=executable_vwap,
            spread=spread,
            estimated_slippage=estimated_slippage,
            mandatory_reject=False,
        )

    return SignalQualityResult(
        decision=SignalDecision.ENTER,
        total_score=total,
        components=comp_raw,
        reason="signal_quality_pass",
        side=side,
        net_edge=net_edge,
        gross_edge=gross_edge,
        executable_ask=executable_vwap,
        spread=spread,
        estimated_slippage=estimated_slippage,
        mandatory_reject=False,
    )


def config_from_worker(worker_cfg) -> SignalQualityConfig:
    """Build gate config from WorkerConfig (or compatible namespace)."""
    weights_raw = getattr(worker_cfg, "signal_quality_weights", None) or {}
    weights = SignalQualityWeights(
        momentum_alignment=float(weights_raw.get("momentum_alignment", 25.0)),
        persistence_acceleration=float(weights_raw.get("persistence_acceleration", 20.0)),
        net_edge=float(weights_raw.get("net_edge", 25.0)),
        liquidity_spread=float(weights_raw.get("liquidity_spread", 15.0)),
        reference_time=float(weights_raw.get("reference_time", 15.0)),
    )
    gate_windows = getattr(worker_cfg, "signal_gate_windows_seconds", (5, 15, 30))
    return SignalQualityConfig(
        min_score=float(getattr(worker_cfg, "min_signal_quality_score", 75.0)),
        weights=weights,
        gate_windows_seconds=tuple(int(w) for w in gate_windows),
        min_short_long_agreement=bool(getattr(worker_cfg, "signal_require_timeframe_agreement", True)),
        max_spread=float(getattr(worker_cfg, "signal_max_spread", 0.06)),
        max_estimated_slippage=float(getattr(worker_cfg, "signal_max_slippage", 0.03)),
        max_token_move_since_open=float(getattr(worker_cfg, "signal_max_token_move_since_open", 0.12)),
        min_persistence_ratio=float(getattr(worker_cfg, "signal_min_persistence_ratio", 0.55)),
        reversal_acceleration_threshold=float(
            getattr(worker_cfg, "signal_reversal_acceleration_threshold", -0.0005)
        ),
        min_net_edge=float(worker_cfg.min_net_edge),
        max_entry_price=float(worker_cfg.momentum_max_entry_price),
        take_profit_pct=float(getattr(worker_cfg, "take_profit_pct", 0.10)),
        fee_rate=float(worker_cfg.estimated_fee_rate),
        slippage_buffer=float(worker_cfg.execution_slippage_buffer),
    )
