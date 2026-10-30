"""Safety-only market helpers for the exact-99c, single-leg strategy.

Legacy momentum, spread-capture, and market-making order engines are removed.
This module retains position accounting and preemptive cancellation only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bot import MarketWorker

_EPS = 1e-9


@dataclass(frozen=True)
class EconomicPosition:
    yes_shares: float
    no_shares: float
    yes_avg: float
    no_avg: float
    invested: float
    position_delta: float
    pnl_if_yes: float
    pnl_if_no: float
    mark_pnl: float
    matched_pairs: float
    locked_profit: float

    @property
    def unpaired(self) -> float:
        return abs(self.yes_shares - self.no_shares)

    @property
    def combined_avg(self) -> float:
        if self.yes_shares <= _EPS or self.no_shares <= _EPS:
            return 0.0
        return self.yes_avg + self.no_avg


def economic_position(worker: "MarketWorker") -> EconomicPosition:
    inv = worker.inventory
    ys = float(inv.shares("YES"))
    ns = float(inv.shares("NO"))
    ya = float(inv.avg_cost("YES")) if ys > _EPS else 0.0
    na = float(inv.avg_cost("NO")) if ns > _EPS else 0.0
    invested = ys * ya + ns * na
    # Resolution economics: YES wins => YES shares pay $1, NO pays $0.
    # DOWN/NO wins => inverse.  This is the core risk/accounting view.
    pnl_yes = ys - invested
    pnl_no = ns - invested
    y_bid = worker.best_bid("YES")
    n_bid = worker.best_bid("NO")
    mark_value = ys * max(y_bid, 0.0) + ns * max(n_bid, 0.0)
    mark_pnl = mark_value - invested
    matched = min(ys, ns)
    locked = max(0.0, matched * (1.0 - ya - na)) if matched > _EPS else 0.0
    return EconomicPosition(
        yes_shares=ys, no_shares=ns, yes_avg=ya, no_avg=na,
        invested=invested, position_delta=ys - ns,
        pnl_if_yes=pnl_yes, pnl_if_no=pnl_no, mark_pnl=mark_pnl,
        matched_pairs=matched, locked_profit=locked,
    )



class BinancePreemptiveCancel:
    """Shared adverse-selection gate for every worker.

    A sharp Binance move cancels the resting token most likely to be filled
    toxically before new quotes are considered.  The signal is deliberately
    conservative: stale Binance data never triggers a cancellation.
    """

    @staticmethod
    def exposed_side(worker: "MarketWorker") -> Optional[str]:
        signal = worker.binance_signal()
        if signal is None or not signal.is_fresh:
            return None
        # Positive Binance move makes resting NO/Down bids vulnerable;
        # negative move makes resting YES/Up bids vulnerable.
        if signal.price_delta <= -worker.worker_config.preemptive_cancel_price_delta:
            return "YES"
        if signal.price_delta >= worker.worker_config.preemptive_cancel_price_delta:
            return "NO"
        if signal.momentum <= -worker.worker_config.preemptive_cancel_momentum:
            return "YES"
        if signal.momentum >= worker.worker_config.preemptive_cancel_momentum:
            return "NO"
        if signal.imbalance <= -worker.worker_config.preemptive_cancel_imbalance:
            return "YES"
        if signal.imbalance >= worker.worker_config.preemptive_cancel_imbalance:
            return "NO"
        return None

    async def run(self, worker: "MarketWorker") -> bool:
        if not worker.worker_config.preemptive_cancel_enabled:
            return False
        side = self.exposed_side(worker)
        if not side:
            worker.dashboard["preemptive_cancel_active"] = False
            return False
        worker.dashboard["preemptive_cancel_active"] = True
        worker.dashboard["preemptive_cancel_side"] = side
        cancelled = await worker.cancel_engine_orders(side=side)
        if cancelled:
            print(
                f"🚨 [PREEMPTIVE CANCEL] {worker.asset_type.upper()} {worker.window_slug} | "
                f"Binance adverse move -> cancelled {side} resting exposure ({cancelled})"
            )
        return cancelled > 0


class ArbigabEngineRouter:
    """Safety-only router. Legacy momentum, spread and market-making engines are removed."""

    def __init__(self) -> None:
        self.cancel = BinancePreemptiveCancel()

    async def on_tick(self, worker: "MarketWorker") -> bool:
        worker.dashboard["engine"] = "risk"
        await self.cancel.run(worker)
        return False
