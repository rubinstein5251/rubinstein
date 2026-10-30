"""Single-position directional strategy: configurable entry + stop-loss FOK."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Optional

from strategies.fok_execution import fok_buy_limit, fok_sell_with_retries
from utils.directional_entry import EntryDecision, pick_entry_side
from utils.entry_limit import compute_buy_fok_limit_price, resolve_minimum_entry_price
from utils.graceful_shutdown import shutdown_requested
from utils.momentum_risk import stop_loss_price, stop_loss_triggered
from utils.profitability_gate import check_executable_depth
from utils.stopped_out_store import stopped_out_store

if TYPE_CHECKING:
    from bot import MarketWorker

_LOG = logging.getLogger("directional_fok")


class DirectionalFokStrategy:
    def _key(self, worker: "MarketWorker") -> str:
        return worker.worker_config.key

    def _position_side(self, worker: "MarketWorker") -> Optional[str]:
        inv = worker.inventory
        ys, ns = inv.yes_shares, inv.no_shares
        from bot import MIN_SHARES

        if ys >= MIN_SHARES - 1e-9 and ns >= MIN_SHARES - 1e-9:
            return worker.dashboard.get("position_side") or worker.position_side
        if ys >= MIN_SHARES - 1e-9:
            return "YES"
        if ns >= MIN_SHARES - 1e-9:
            return "NO"
        return None

    def _publish_dashboard(self, worker: "MarketWorker") -> None:
        cfg = worker.worker_config
        cid = worker.canonical_market_id() or ""
        wkey = self._key(worker)
        stopped = stopped_out_store.is_stopped_out(wkey, cid) if cid else False
        side = self._position_side(worker)
        inv = worker.inventory
        min_res = worker.resolved_entry_fok_limit()
        worker.dashboard["minimum_entry_price"] = min_res.minimum_price
        worker.dashboard["minimum_entry_price_c"] = round(min_res.minimum_price * 100, 2)
        worker.dashboard["configured_entry_price"] = min_res.configured_entry
        worker.dashboard["configured_entry_price_c"] = round(min_res.configured_entry * 100, 2)
        worker.dashboard["stop_loss_pct"] = cfg.stop_loss_pct
        worker.dashboard["order_size"] = cfg.order_size_max
        worker.dashboard["execution_type"] = "FOK"
        worker.dashboard["market_id"] = cid
        worker.dashboard["market_stopped_out"] = stopped
        worker.dashboard["position_side"] = side or ""
        worker.dashboard["yes_ask"] = round(float(worker.best_ask("YES")), 4)
        worker.dashboard["no_ask"] = round(float(worker.best_ask("NO")), 4)
        worker.dashboard["yes_ask_c"] = round(worker.dashboard["yes_ask"] * 100, 2)
        worker.dashboard["no_ask_c"] = round(worker.dashboard["no_ask"] * 100, 2)
        if side:
            entry = inv.avg_cost(side)
            worker.dashboard["entry_fill_price"] = round(entry, 4)
            worker.dashboard["entry_fill_price_c"] = round(entry * 100, 2)
            worker.dashboard["position_size"] = round(inv.shares(side), 4)
            bid = worker.best_bid(side)
            worker.dashboard["executable_bid"] = round(bid, 4)
            worker.dashboard["executable_bid_c"] = round(bid * 100, 2) if bid > 0 else 0
            stop_px = stop_loss_price(entry, cfg.stop_loss_pct)
            worker.dashboard["stop_loss_trigger_price"] = round(stop_px, 4)
            worker.dashboard["stop_loss_trigger_c"] = round(stop_px * 100, 2)
            mark = bid * inv.shares(side) if bid > 0 else 0.0
            worker.dashboard["unrealized_pnl"] = round(mark - entry * inv.shares(side), 4)
        elif worker.dashboard.get("entry_order_status") != "FILLED":
            worker.dashboard["entry_fill_price"] = 0.0
            worker.dashboard["entry_fill_price_c"] = 0.0
            worker.dashboard["position_size"] = 0.0
            worker.dashboard["executable_bid"] = 0.0
            worker.dashboard["executable_bid_c"] = 0.0
            worker.dashboard["stop_loss_trigger_price"] = 0.0
            worker.dashboard["stop_loss_trigger_c"] = 0.0
            worker.dashboard["unrealized_pnl"] = 0.0
        worker.dashboard["realized_pnl"] = round(inv.realized_pnl, 4)

    def _market_ready(self, worker: "MarketWorker") -> Optional[str]:
        from bot import is_locked_price

        if getattr(worker, "_ws_market_data_stale", False):
            return "REJECT_STALE_ORDERBOOK"
        if not worker.active_market:
            return "no_active_market"
        if not worker.canonical_market_id():
            return "missing_condition_id"
        if worker.market_seconds_left() <= 0:
            return "market_closed"
        cfg = worker.worker_config
        age = worker.max_quote_age_seconds()
        if age is not None and age > cfg.max_quote_age_seconds:
            return "REJECT_STALE_ORDERBOOK"
        y = float(worker.best_ask("YES"))
        n = float(worker.best_ask("NO"))
        if y <= 0 and n <= 0:
            return "missing_quotes"
        if y > 0 and is_locked_price(y):
            return "locked_outcome_price"
        if n > 0 and is_locked_price(n):
            return "locked_outcome_price"
        return None

    async def _record_buy(
        self,
        worker: "MarketWorker",
        side: str,
        qty: float,
        fill_px: float,
        limit_px: float,
        oid: Optional[str],
    ) -> None:
        if oid and not worker.record_fill_idempotent(oid, side, qty, fill_px):
            return
        worker.inventory.record_buy(side, qty, fill_px)
        worker.entry_count = 1
        worker.position_side = side
        worker.position_size = qty
        worker.entry_price = fill_px
        from bot import TradeState

        worker.trade_state = TradeState.FILLED
        worker.log_trade(side, fill_px, "BUY", size=qty, order_id=oid)
        worker.dashboard["buy_limit_price"] = limit_px
        worker.dashboard["buy_limit_price_c"] = round(limit_px * 100, 2)
        worker.dashboard["entry_fill_price"] = round(fill_px, 4)
        worker.dashboard["entry_fill_price_c"] = round(fill_px * 100, 2)
        worker.dashboard["filled_size"] = round(qty, 4)
        worker.dashboard["entry_order_status"] = "FILLED"
        worker.dashboard["strategy_state"] = "in_position"
        worker.dashboard["bought_side"] = side
        label = f"{worker.asset_type.upper()} {worker.window_slug}"
        print(
            f"✅ [BUY FILLED] {label} | {side} | limit_price={limit_px:.4f} | "
            f"avg_fill_price={fill_px:.4f} | filled_size={qty:.2f}"
        )

    async def _record_stop_sell(
        self, worker: "MarketWorker", side: str, qty: float, px: float, oid: Optional[str],
    ) -> None:
        if oid and oid != "stop-loss" and not worker.record_fill_idempotent(oid, side, qty, px):
            return
        worker.inventory.record_sell(side, qty, px)
        worker.log_trade(side, px, "SELL", size=qty, order_id=oid)
        worker.dashboard["exit_order_status"] = "FILLED"
        worker.dashboard["strategy_state"] = "position_closed"
        worker.position_side = None
        worker.position_size = 0.0
        worker.entry_price = 0.0
        worker.dashboard["buy_limit_price"] = 0.0
        worker.dashboard["buy_limit_price_c"] = 0.0

    async def _handle_stop_loss(self, worker: "MarketWorker", cid: str) -> None:
        from bot import MIN_SHARES, OrderState

        cfg = worker.worker_config
        side = self._position_side(worker)
        if not side:
            return
        shares = worker.inventory.shares(side)
        if shares < MIN_SHARES - 1e-9:
            return
        entry = worker.inventory.avg_cost(side)
        bid = float(worker.best_bid(side))
        if bid <= 0 or not stop_loss_triggered(bid, entry, cfg.stop_loss_pct):
            return

        wkey = self._key(worker)
        stopped_out_store.mark_stopped_out(wkey, cid)
        worker.dashboard["market_stopped_out"] = True
        worker.dashboard["strategy_state"] = "stop_loss_triggered"

        if worker._stop_loss_exit_in_flight:
            return
        async with worker._stop_loss_lock:
            if worker._stop_loss_exit_in_flight:
                return
            worker._stop_loss_exit_in_flight = True
            worker.dashboard["exit_order_status"] = "SUBMITTED"
            worker.dashboard["strategy_state"] = "stop_loss_exit"
            try:
                if worker.order_state == OrderState.PENDING or worker._order_lock.locked():
                    return
                remaining = worker.inventory.shares(side)
                if remaining < MIN_SHARES - 1e-9:
                    return
                async with worker._order_lock:
                    worker.order_state = OrderState.PENDING
                    stop_limit = stop_loss_price(entry, cfg.stop_loss_pct)
                    qty, px, oid = await fok_sell_with_retries(
                        worker,
                        side,
                        remaining,
                        purpose="stop_loss",
                        limit_price=stop_limit,
                        max_retries=cfg.stop_loss_exit_max_retries,
                        retry_delay_ms=cfg.stop_loss_exit_retry_ms,
                    )
                    worker.order_state = OrderState.IDLE
                    remaining_after = worker.inventory.shares(side)
                    if qty > MIN_SHARES - 1e-9 and px > 0:
                        await self._record_stop_sell(worker, side, qty, px, oid)
                    if remaining_after >= MIN_SHARES - 1e-9:
                        worker.dashboard["strategy_state"] = "stop_loss_exit_pending"
                        worker.dashboard["exit_order_status"] = "RETRY"
                        worker.dashboard["entry_block_reason"] = "stop_loss_exit_incomplete"
                    else:
                        worker.dashboard["entry_block_reason"] = "stopped_out_market"
            finally:
                worker._stop_loss_exit_in_flight = False

    async def _try_entry(self, worker: "MarketWorker", cid: str) -> None:
        from bot import MIN_SHARES, OrderState, is_trading_allowed

        if shutdown_requested():
            worker.dashboard["entry_block_reason"] = "shutdown_in_progress"
            return

        cfg = worker.worker_config
        wkey = self._key(worker)
        label = f"{worker.asset_type.upper()} {worker.window_slug}"
        if stopped_out_store.is_stopped_out(wkey, cid):
            worker.dashboard["strategy_state"] = "idle"
            worker.dashboard["entry_block_reason"] = "stopped_out_market"
            worker.dashboard["market_stopped_out"] = True
            return
        if worker.entry_count >= 1 or self._position_side(worker):
            worker.dashboard["entry_block_reason"] = "position_already_open"
            return
        if cid in worker._market_entry_guard:
            worker.dashboard["entry_block_reason"] = "entry_in_progress"
            return

        block = self._market_ready(worker)
        if block:
            worker.dashboard["entry_block_reason"] = block
            worker.dashboard["strategy_state"] = "idle"
            return
        if not worker.can_enter():
            worker.dashboard["entry_block_reason"] = "outside_entry_window"
            return
        if not is_trading_allowed():
            worker.dashboard["entry_block_reason"] = "trading_schedule_blocked"
            return

        min_res = worker.resolved_entry_fok_limit()
        if not min_res.tick_aligned:
            print(
                f"⚠️ [ENTRY] {label} | minimum_entry={min_res.configured_entry:.4f} "
                f"adjusted to tick={min_res.minimum_price:.4f}"
            )

        size = worker.entry_order_size(["YES", "NO"])
        if not size or size < cfg.min_order_size:
            worker.dashboard["entry_block_reason"] = "invalid_order_size"
            return

        yes_ask = float(worker.best_ask("YES"))
        no_ask = float(worker.best_ask("NO"))
        choice = pick_entry_side(yes_ask, no_ask, min_entry_price=min_res.minimum_price)
        worker.dashboard["entry_candidate_side"] = choice.side or ""
        side_for_log = choice.side or "—"
        ask_log = choice.best_ask if choice.side else max(yes_ask, no_ask)
        eligible = choice.decision == EntryDecision.BUY and choice.side is not None
        print(
            f"ℹ️ [ENTRY CHECK] {label} | {side_for_log} ask={ask_log:.4f} | "
            f"minimum_entry={min_res.minimum_price:.4f} | eligible={str(eligible).lower()}"
        )

        if choice.decision != EntryDecision.BUY or not choice.side:
            worker.dashboard["entry_block_reason"] = choice.reason
            worker.dashboard["strategy_state"] = "idle"
            return

        side = choice.side
        exe = worker.executable_buy(side, float(size))
        if not exe:
            worker.dashboard["entry_block_reason"] = "no_liquidity"
            return
        limit_px, limit_reason = compute_buy_fok_limit_price(exe[0], min_res.minimum_price)
        if limit_px is None:
            worker.dashboard["entry_block_reason"] = limit_reason
            return
        depth_err = check_executable_depth(exe, float(size), min_orderbook_depth=cfg.min_orderbook_depth)
        if depth_err:
            worker.dashboard["entry_block_reason"] = depth_err
            return
        worker.dashboard["strategy_state"] = "entry_eligible"
        worker.dashboard["buy_limit_price"] = limit_px
        worker.dashboard["buy_limit_price_c"] = round(limit_px * 100, 2)
        worker.dashboard["entry_fill_price"] = 0.0
        worker.dashboard["entry_fill_price_c"] = 0.0

        worker._market_entry_guard.add(cid)
        try:
            async with worker._order_lock:
                if stopped_out_store.is_stopped_out(wkey, cid):
                    return
                if worker.order_state != OrderState.IDLE:
                    worker.dashboard["entry_block_reason"] = "REJECT_PENDING_ORDER_CONFLICT"
                    return
                if self._position_side(worker) or worker.entry_count >= 1:
                    return
                worker.order_state = OrderState.PENDING
                worker.dashboard["strategy_state"] = "entry_pending"
                worker.dashboard["entry_order_status"] = "SUBMITTED"
                print(
                    f"📤 [BUY FOK] {label} | {side} | limit_price={limit_px:.4f} | size={size:.2f}"
                )
                qty, fill_px, oid = await fok_buy_limit(
                    worker, side, limit_px, float(size), purpose="directional_entry",
                )
                worker.order_state = OrderState.IDLE
                if qty <= MIN_SHARES - 1e-9:
                    worker.dashboard["entry_block_reason"] = "entry_fok_failed"
                    worker.dashboard["entry_order_status"] = "FAILED"
                    worker.dashboard["strategy_state"] = "entry_failed"
                    worker.dashboard["entry_fill_price"] = 0.0
                    worker.dashboard["entry_fill_price_c"] = 0.0
                    print(
                        f"❌ [BUY FOK FAILED] {label} | {side} | limit_price={limit_px:.4f} | "
                        f"reason=insufficient_liquidity_or_no_fill"
                    )
                    return
                await self._record_buy(worker, side, qty, fill_px, limit_px, oid)
                _LOG.info(
                    json.dumps(
                        {
                            "event": "directional_entry_filled",
                            "market_id": cid,
                            "side": side,
                            "limit_price": limit_px,
                            "avg_fill_price": fill_px,
                            "qty": qty,
                            "ts": time.time(),
                        }
                    )
                )
        finally:
            worker._market_entry_guard.discard(cid)

    async def on_market_tick(self, worker: "MarketWorker") -> None:
        if shutdown_requested():
            return
        self._publish_dashboard(worker)
        cid = worker.canonical_market_id()
        if not cid:
            return

        await self._handle_stop_loss(worker, cid)

        has_position = self._position_side(worker) is not None
        if has_position or worker._stop_loss_exit_in_flight:
            self._publish_dashboard(worker)
            return

        wkey = self._key(worker)
        if stopped_out_store.is_stopped_out(wkey, cid):
            worker.dashboard["entry_block_reason"] = "stopped_out_market"
            worker.dashboard["strategy_state"] = "idle"
            return

        if worker.order_state.name == "PENDING" or worker._order_lock.locked():
            return

        await self._try_entry(worker, cid)
        self._publish_dashboard(worker)

    async def on_binance_tick(self, worker: "MarketWorker") -> None:
        await self.on_market_tick(worker)

    async def evaluate(self, worker: "MarketWorker"):
        return None

    async def execute(self, worker: "MarketWorker", decision) -> None:
        return None


MomentumStrategy = DirectionalFokStrategy
GabagoolStrategy = DirectionalFokStrategy
SpreadCaptureStrategy = DirectionalFokStrategy
LateEntryStrategy = DirectionalFokStrategy
BinanceTriggeredFokStrategy = DirectionalFokStrategy
BinanceFokStrategy = DirectionalFokStrategy
InventoryAccumulationStrategy = DirectionalFokStrategy
