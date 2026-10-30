"""Exact-99c single-leg entry placement and fill monitoring."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Dict, List, Tuple

from strategies.base import GabagoolDecision
from utils.momentum_risk import is_order_fully_filled

if TYPE_CHECKING:
    from bot import MarketWorker


async def _simulate_dry_leg(
    worker: "MarketWorker",
    side: str,
    price: float,
    size: float,
) -> Tuple[str, float, float] | None:
    """Conservative paper fill: only fill when the observed ask crosses the limit.

    This cannot model queue priority or displayed depth; it deliberately refuses
    to invent fills for resting quotes that merely sit at the bid.
    """
    from bot import MIN_FILL_DELTA

    timeout_sec = max(0.0, worker.worker_config.fill_timeout_ms / 1000.0)
    poll_sec = max(0.05, worker.worker_config.fill_poll_ms / 1000.0)
    deadline = time.monotonic() + timeout_sec
    first_check = True
    while first_check or time.monotonic() <= deadline:
        first_check = False
        ask = float(worker.best_ask(side))
        if 0 < ask <= price + 1e-9:
            # No depth is exposed by best_ask(); report only a crossing-based
            # simulated fill and make the assumption explicit in the event log.
            quantity = max(0.0, float(size))
            if quantity > MIN_FILL_DELTA:
                return side, quantity, ask
        await asyncio.sleep(poll_sec)
    return None


async def _monitor_dry_fills(
    worker: "MarketWorker",
    decision: GabagoolDecision,
    legs: List[Tuple[str, float]],
) -> Dict[str, Tuple[float, float]]:
    """Simulate only observable marketable fills; never auto-fill on a timer."""
    cfg = worker.worker_config
    order_size = float(decision.size)
    leg_tasks = {
        side: asyncio.create_task(_simulate_dry_leg(worker, side, price, order_size))
        for side, price in legs
    }
    fills: Dict[str, Tuple[float, float]] = {}
    results = await asyncio.gather(*leg_tasks.values(), return_exceptions=True)
    for side, result in zip(leg_tasks.keys(), results):
        if isinstance(result, Exception):
            print(f"  🧪 [PAPER] {side} simulation error: {type(result).__name__}")
            continue
        if result is None:
            print(f"  🧪 [PAPER NO FILL] {side}: ask did not cross limit within {cfg.fill_timeout_ms}ms")
            continue
        fill_side, size, price = result
        fills[fill_side] = (size, price)
        print(f"  🧪 [PAPER CROSSING FILL] {fill_side} {size:.2f}@{price:.4f}; depth/queue not modeled")
    return fills


async def _monitor_live_fills(
    worker: "MarketWorker",
    decision: GabagoolDecision,
    legs: List[Tuple[str, float]],
    placed: List[Tuple[str | None, float]],
) -> Dict[str, Tuple[float, float]]:
    from bot import MIN_FILL_DELTA

    cfg = worker.worker_config
    order_size = float(decision.size)
    timeout_sec = cfg.fill_timeout_ms / 1000.0
    poll_sec = cfg.fill_poll_ms / 1000.0

    fills: Dict[str, Tuple[float, float]] = {}
    pending: Dict[str, Tuple[str, float, float]] = {}
    observed: Dict[str, float] = {}

    def add_delta(side: str, delta: float, fill_price: float) -> None:
        if delta <= MIN_FILL_DELTA:
            return
        existing_size, existing_price = fills.get(side, (0.0, 0.0))
        total = existing_size + delta
        fills[side] = (total, (existing_size * existing_price + delta * fill_price) / total)

    for (side, limit_price), (order_id, immediate_fill) in zip(legs, placed):
        if not order_id:
            continue
        if immediate_fill:
            # The placement helper's immediate-fill flag is a quantity in this
            # codebase; clamp it to the requested order size.
            quantity = min(order_size, max(0.0, float(immediate_fill)))
            add_delta(side, quantity, limit_price)
            worker._untrack_order(order_id)
        else:
            pending[side] = (order_id, limit_price, order_size)
            observed[order_id] = 0.0

    started = time.monotonic()
    while pending and (time.monotonic() - started) < timeout_sec:
        for side in list(pending.keys()):
            order_id, limit_price, requested = pending[side]
            cumulative, fill_price = await worker.poll_order_fill(order_id, requested, limit_price)
            cumulative = max(observed.get(order_id, 0.0), min(requested, max(0.0, cumulative)))
            delta = cumulative - observed.get(order_id, 0.0)
            if delta > MIN_FILL_DELTA:
                add_delta(side, delta, fill_price)
                observed[order_id] = cumulative
            if is_order_fully_filled(requested, cumulative, MIN_FILL_DELTA):
                worker._untrack_order(order_id)
                del pending[side]
        if pending:
            await asyncio.sleep(poll_sec)

    for side, (order_id, limit_price, requested) in list(pending.items()):
        try:
            cumulative, fill_price = await worker.cancel_order_confirmed(
                order_id, requested, limit_price,
            )
        except TimeoutError:
            # Cancellation is uncertain: preserve the order in tracking and do
            # not submit a replacement. Already-observed fills are still returned.
            print(f"⚠️ [ORDER SAFETY] cancellation unconfirmed for {order_id}; keeping it tracked")
            continue
        previous = observed.get(order_id, 0.0)
        cumulative = max(previous, min(requested, max(0.0, cumulative)))
        add_delta(side, cumulative - previous, fill_price)

    return fills


def _record_fills(
    worker: "MarketWorker",
    fills: Dict[str, Tuple[float, float]],
) -> None:
    from bot import MIN_FILL_DELTA

    for side, (size, price) in fills.items():
        if size > MIN_FILL_DELTA and price > 0:
            worker.inventory.record_buy(side, size, price)
            print(f"  ✅ [GABAGOOL FILL] {side} {size:.2f}@{round(price*100)}c")


async def execute_momentum_decision(
    worker: "MarketWorker",
    decision: GabagoolDecision,
) -> None:
    """Execute one exact-99c entry. No hedge or averaging orders are generated."""
    from bot import OrderState

    cfg = worker.worker_config
    legs = worker.resolve_execution_legs(decision)
    if not legs:
        print(
            f"❌ [GABAGOOL ABORT] {worker.asset_type.upper()} {worker.window_slug} | "
            f"no executable ask for {decision.side}"
        )
        return

    if not worker.validate_entry_execution(decision, legs):
        return

    for side, _price in legs:
        if not worker.validate_order_size(side, decision.size):
            print(
                f"❌ [GABAGOOL ABORT] {worker.asset_type.upper()} {worker.window_slug} | "
                f"{side} size={decision.size} failed pre-submit sanity check"
            )
            return

    worker.order_state = OrderState.PENDING
    try:
        leg_str = " ".join(f"{s}@{round(p*100)}c" for s, p in legs)
        if worker.is_dry_run():
            print(
                f"\n🧪 [DRY GABAGOOL] {worker.asset_type.upper()} {worker.window_slug} | "
                f"{decision.side} ask={decision.trigger_price:.4f} "
                f"size={decision.size} reason={decision.reason} | {leg_str} | "
                f"pairing=disabled"
            )
            start = time.monotonic()
            fills = await _monitor_dry_fills(worker, decision, legs)
            _record_fills(worker, fills)
            if fills:
                worker.log_entry_trades(fills=fills)
            worker._log_entry(decision, fills=fills or None, dry_run=True)
            print(f"  🧪 [DRY GABAGOOL] cycle done in {time.monotonic() - start:.2f}s")
            return

        print(
            f"\n📊 [GABAGOOL] {worker.asset_type.upper()} {worker.window_slug} | "
            f"{decision.side} ask={decision.trigger_price:.4f} "
            f"reason={decision.reason} | {leg_str} | "
            f"pairing=disabled"
        )

        order_size = float(decision.size)
        placed = await asyncio.gather(
            *[worker.place_entry_gtc(
                side, price, order_size, single_leg_entry_authorized=True,
            ) for side, price in legs]
        )

        fills = await _monitor_live_fills(worker, decision, legs, placed)
        _record_fills(worker, fills)

        if fills:
            worker.log_entry_trades(fills=fills)
        worker._log_entry(decision, fills=fills or None)
    finally:
        worker.order_state = OrderState.IDLE


# Explicit name for new code; old import remains valid.
execute_gabagool_decision = execute_momentum_decision
