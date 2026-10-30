"""FOK limit buy/sell helpers for the directional strategy."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Optional, Tuple

from utils.clob_helpers import clamp_buy_price, clamp_sell_price

if TYPE_CHECKING:
    from bot import MarketWorker


async def _simulate_fok_buy(worker: "MarketWorker", side: str, limit: float, size: float):
    from bot import MIN_FILL_DELTA

    cfg = worker.worker_config
    timeout_sec = max(0.0, cfg.fill_timeout_ms / 1000.0)
    poll_sec = max(0.05, cfg.fill_poll_ms / 1000.0)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() <= deadline:
        exe = worker.executable_buy(side, size)
        if exe and exe[0] <= limit + 1e-9 and exe[1] >= size - MIN_FILL_DELTA:
            return exe[1], exe[0]
        await asyncio.sleep(poll_sec)
    return 0.0, 0.0


async def fok_buy_limit(
    worker: "MarketWorker",
    side: str,
    limit_price: float,
    size: float,
    *,
    purpose: str,
) -> Tuple[float, float, Optional[str]]:
    from bot import MIN_FILL_DELTA

    limit = clamp_buy_price(limit_price)
    if purpose == "directional_entry":
        minimum = worker.resolved_entry_fok_limit().minimum_price
        if limit < minimum - 1e-9:
            raise ValueError(
                f"buy limit {limit:.4f} below minimum entry {minimum:.4f}"
            )
    if worker.is_dry_run():
        qty, px = await _simulate_fok_buy(worker, side, limit, size)
        return qty, px, "dry-run" if qty > MIN_FILL_DELTA else None

    placed = await worker.place_strategy_fok_buy(side, limit, size, purpose=purpose)
    order_id, immediate = placed[0], placed[1]
    if not order_id:
        return 0.0, 0.0, None
    if immediate and immediate > MIN_FILL_DELTA:
        worker._untrack_order(order_id)
        fill_px = await worker.resolve_fill_price(order_id, size, limit)
        return float(immediate), fill_px, order_id

    cfg = worker.worker_config
    timeout_sec = cfg.fill_timeout_ms / 1000.0
    poll_sec = cfg.fill_poll_ms / 1000.0
    started = time.monotonic()
    cumulative = 0.0
    fill_price = limit
    while time.monotonic() - started < timeout_sec:
        cumulative, fill_price = await worker.poll_order_fill(order_id, size, limit)
        from utils.momentum_risk import is_order_fully_filled

        if is_order_fully_filled(size, cumulative, MIN_FILL_DELTA):
            worker._untrack_order(order_id)
            return cumulative, fill_price, order_id
        await asyncio.sleep(poll_sec)
    try:
        cumulative, fill_price = await worker.cancel_order_confirmed(order_id, size, limit)
    except TimeoutError:
        return cumulative, fill_price, order_id
    worker._untrack_order(order_id)
    from utils.momentum_risk import is_order_fully_filled

    if is_order_fully_filled(size, cumulative, MIN_FILL_DELTA):
        return cumulative, fill_price, order_id
    return 0.0, 0.0, order_id


async def _simulate_fok_sell(worker: "MarketWorker", side: str, limit: float, size: float):
    from bot import MIN_FILL_DELTA

    cfg = worker.worker_config
    timeout_sec = max(0.0, cfg.fill_timeout_ms / 1000.0)
    poll_sec = max(0.05, cfg.fill_poll_ms / 1000.0)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() <= deadline:
        bid = float(worker.best_bid(side))
        if bid >= limit - 1e-9:
            qty = min(size, worker.inventory.shares(side))
            if qty >= size - MIN_FILL_DELTA:
                return qty, bid
        await asyncio.sleep(poll_sec)
    return 0.0, 0.0


async def fok_sell_limit(
    worker: "MarketWorker",
    side: str,
    limit_price: float,
    size: float,
    *,
    purpose: str,
) -> Tuple[float, float, Optional[str]]:
    from bot import MIN_FILL_DELTA

    limit = clamp_sell_price(limit_price)
    if worker.is_dry_run():
        qty, px = await _simulate_fok_sell(worker, side, limit, size)
        return qty, px, "dry-run" if qty > MIN_FILL_DELTA else None

    fill_size, fill_px = await worker.place_exit_sell(
        side, limit, size, stop_loss_only=True,
    )
    if fill_size > MIN_FILL_DELTA:
        return fill_size, fill_px, "stop-loss"
    return 0.0, 0.0, None


async def fok_sell_with_retries(
    worker: "MarketWorker",
    side: str,
    size: float,
    *,
    purpose: str,
    limit_price: Optional[float] = None,
    max_retries: int,
    retry_delay_ms: int,
) -> Tuple[float, float, Optional[str]]:
    from bot import MIN_FILL_DELTA

    attempts = 0
    remaining = float(size)
    total_qty = 0.0
    vwap_num = 0.0
    last_oid: Optional[str] = None
    while attempts <= max_retries and remaining > MIN_FILL_DELTA:
        worker.refresh_order_book(side)
        bid = float(worker.best_bid(side))
        if bid <= 0:
            break
        limit = clamp_sell_price(limit_price if limit_price is not None else bid)
        qty, px, oid = await fok_sell_limit(worker, side, limit, remaining, purpose=purpose)
        last_oid = oid or last_oid
        if qty > MIN_FILL_DELTA and px > 0:
            total_qty += qty
            vwap_num += qty * px
            remaining = max(0.0, worker.inventory.shares(side))
            if remaining <= MIN_FILL_DELTA:
                avg_px = vwap_num / total_qty if total_qty > 0 else px
                return total_qty, avg_px, last_oid
        attempts += 1
        if attempts > max_retries:
            break
        await asyncio.sleep(max(0.05, retry_delay_ms / 1000.0))
    if total_qty > MIN_FILL_DELTA:
        return total_qty, vwap_num / total_qty, last_oid
    return 0.0, 0.0, last_oid
