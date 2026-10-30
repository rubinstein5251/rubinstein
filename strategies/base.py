"""Strategy contracts for Gabagool-style paired UP/DOWN trading."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol

if TYPE_CHECKING:
    from bot import MarketWorker


@dataclass(frozen=True)
class GabagoolDecision:
    """Legacy alias — spread capture uses SpreadCaptureDecision."""

    side: str
    price: float
    size: float
    trigger_price: float
    reason: str
    projected_pair_cost: float
    first_leg: bool


@dataclass(frozen=True)
class SpreadCaptureDecision:
    """Matched YES+NO spread capture at executable asks."""

    size: float
    yes_limit: float
    no_limit: float
    yes_ask: float
    no_ask: float
    combined_cost: float
    expected_net_profit_per_pair: float
    reason: str
    attempt_id: str
    condition_id: str


class GabagoolStrategyProtocol(Protocol):
    async def evaluate(self, worker: "MarketWorker") -> Optional[GabagoolDecision]:
        ...

    async def execute(self, worker: "MarketWorker", decision: GabagoolDecision) -> None:
        ...


# Compatibility aliases.  The bot imports these names today; keeping them
# avoids forcing unrelated code to change at the same time as the strategy.
MomentumDecision = GabagoolDecision
MomentumStrategyProtocol = GabagoolStrategyProtocol
