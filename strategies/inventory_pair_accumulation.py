"""Dynamic arbitrage: paired entry, timed hedge, average-down, rebalance."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Optional

from strategies.fok_execution import fok_buy_limit
from utils.clob_helpers import clamp_buy_price
from utils.dynamic_arbitrage_engine import (
    ArbitrageAction,
    ArbitrageConfig,
    evaluate_inventory_tick,
    hedge_deadline_from_fill,
)
from utils.graceful_shutdown import shutdown_requested
from utils.inventory_balance import BuyApproval, log_buy_plan, plan_single_leg_buy
from utils.inventory_cost import InventorySnapshot, matched_combined_average
from utils.inventory_strategy_store import (
    AccumulationPhase,
    inventory_strategy_store,
)

if TYPE_CHECKING:
    from bot import MarketWorker

_LOG = logging.getLogger("inventory_pair_accumulation")


class InventoryPairAccumulationStrategy:
    def _key(self, worker: "MarketWorker") -> str:
        return worker.worker_config.key

    def _arb_cfg(self, worker: "MarketWorker") -> ArbitrageConfig:
        wc = worker.worker_config
        target = float(wc.target_combined_pair_cost or wc.combined_cost_target)
        return ArbitrageConfig(
            target_combined_cost=target,
            max_combined_cost=float(wc.max_combined_pair_cost or wc.max_combined_cost),
            emergency_max_combined_cost=1.0,
            rebalance_max_combined_cost=1.0,
            min_net_profit_per_pair=float(wc.min_net_profit_per_pair or wc.gabagool_min_profit_margin),
            slippage_reserve=float(wc.spread_capture_slippage_buffer),
            max_slippage=float(wc.max_slippage),
            cheap_entry_price=float(wc.cheap_entry_price),
            max_first_leg_price=float(wc.gabagool_initial_entry_threshold),
            min_order_size=float(wc.min_order_size),
            fee_rate=float(wc.spread_capture_fee_rate),
            hedge_timeout_seconds=float(wc.initial_hedge_timeout_seconds),
        )

    def _max_unmatched(self, worker: "MarketWorker") -> float:
        wc = worker.worker_config
        return max(
            float(wc.max_unmatched_shares),
            float(wc.gabagool_max_unpaired_shares),
            float(wc.max_unmatched_yes),
            float(wc.max_unmatched_no),
        )

    def _max_shares_per_side(self, worker: "MarketWorker") -> float:
        wc = worker.worker_config
        return max(float(wc.max_shares), float(wc.max_position_size))

    def _purpose_for_action(self, action: ArbitrageAction) -> str:
        if action == ArbitrageAction.FIRST_LEG:
            return "cheap_entry"
        if action == ArbitrageAction.IDEAL_HEDGE:
            return "initial_hedge"
        if action == ArbitrageAction.EMERGENCY_HEDGE:
            return "emergency_hedge"
        if action == ArbitrageAction.AVERAGE_DOWN:
            return "dca_average_down"
        return "dca_rebalance"

    async def _record_buy(
        self,
        worker: "MarketWorker",
        side: str,
        qty: float,
        fill_px: float,
        limit_px: float,
        oid: Optional[str],
        *,
        action: ArbitrageAction,
    ) -> None:
        if oid and not worker.record_fill_idempotent(oid, side, qty, fill_px):
            return
        worker.inventory.record_buy(side, qty, fill_px)
        worker.log_trade(side, fill_px, "BUY", size=qty, order_id=oid)
        notify = getattr(worker, "notify_confirmed_fill", None)
        if callable(notify):
            notify(side=side, action="BUY", quantity=qty, price=fill_px, order_id=oid)
        cid = worker.canonical_market_id() or ""
        wkey = self._key(worker)
        if action == ArbitrageAction.FIRST_LEG:
            wc = worker.worker_config
            inventory_strategy_store.start_cycle(
                wkey,
                cid,
                first_leg_side=side,
                hedge_delay_seconds=float(wc.initial_hedge_delay_seconds),
                hedge_timeout_seconds=float(wc.initial_hedge_timeout_seconds),
            )
        elif action in (ArbitrageAction.IDEAL_HEDGE, ArbitrageAction.EMERGENCY_HEDGE):
            rec = inventory_strategy_store.cycle_for_market(wkey, cid)
            if rec:
                inventory_strategy_store.record_cycle_leg_fill(
                    rec,
                    leg="hedge",
                    side=side,
                    qty=qty,
                    price=fill_px,
                    kind=action.value,
                )
                rec.initial_hedge_complete = True
                rec.phase = AccumulationPhase.PAIR_COMPLETED
                combined = matched_combined_average(
                    InventorySnapshot.from_ledger(worker.inventory, fee_rate=0.0),
                )
                if combined is not None and combined <= self._arb_cfg(worker).target_combined_cost + 1e-9:
                    rec.target_achieved = True
                inventory_strategy_store.upsert(rec)
        elif action in (ArbitrageAction.AVERAGE_DOWN, ArbitrageAction.REBALANCE):
            rec = inventory_strategy_store.cycle_for_market(wkey, cid)
            if rec:
                inventory_strategy_store.record_cycle_leg_fill(
                    rec,
                    leg="dca",
                    side=side,
                    qty=qty,
                    price=fill_px,
                    kind=action.value,
                )
        worker.dashboard["entry_fill_price"] = round(fill_px, 4)
        worker.dashboard["strategy_state"] = action.value
        if hasattr(worker, "sync_accounting_dashboard"):
            worker.sync_accounting_dashboard()

    async def _execute_decision(
        self,
        worker: "MarketWorker",
        decision,
        *,
        cid: str,
    ) -> None:
        from bot import OrderState

        if decision.action == ArbitrageAction.NONE or decision.qty <= 0:
            return
        purpose = self._purpose_for_action(decision.action)
        allow_first = decision.action == ArbitrageAction.FIRST_LEG
        wc = worker.worker_config
        snap = InventorySnapshot.from_ledger(worker.inventory, fee_rate=wc.spread_capture_fee_rate)
        market = worker.active_market.get("question", "") if worker.active_market else ""
        plan = plan_single_leg_buy(
            snap=snap,
            eff=worker.effective_inventory(),
            side=decision.side,
            requested_qty=decision.qty,
            min_order_size=float(wc.min_order_size),
            max_share_imbalance=float(wc.max_share_imbalance),
            max_unmatched_shares=self._max_unmatched(worker),
            balance_inventory_before_buy=bool(wc.balance_inventory_before_buy),
            allow_unmatched_accumulation=True,
            purpose=purpose,
            allow_first_leg_entry=allow_first,
            max_shares_per_side=self._max_shares_per_side(worker),
        )
        log_buy_plan(plan, market=market, purpose=purpose)
        if plan.approval != BuyApproval.APPROVED or plan.approved_qty <= 0:
            worker.dashboard["entry_block_reason"] = plan.reason
            return
        qty = plan.approved_qty
        limit_px = clamp_buy_price(decision.limit_price)
        async with worker._order_lock:
            if worker.order_state != OrderState.IDLE:
                return
            worker.order_state = OrderState.PENDING
            fok_purpose = (
                "emergency_hedge"
                if decision.action == ArbitrageAction.EMERGENCY_HEDGE
                else "pair_accumulation"
            )
            qty_f, fill_px, oid = await fok_buy_limit(
                worker, decision.side, limit_px, qty, purpose=fok_purpose,
            )
            worker.order_state = OrderState.IDLE
            if qty_f <= 0:
                worker.dashboard["entry_block_reason"] = f"{decision.action.value}_fok_failed"
                return
            await self._record_buy(
                worker, decision.side, qty_f, fill_px, limit_px, oid, action=decision.action,
            )
            _LOG.info(
                json.dumps(
                    {
                        "event": "inventory_buy_filled",
                        "market_id": cid,
                        "action": decision.action.value,
                        "side": decision.side,
                        "qty": qty_f,
                        "fill_px": fill_px,
                        "projected_combined": decision.projected_combined,
                        "ts": time.time(),
                    }
                )
            )

    def _publish_dashboard(self, worker: "MarketWorker") -> None:
        snap = InventorySnapshot.from_ledger(
            worker.inventory,
            fee_rate=worker.worker_config.spread_capture_fee_rate,
        )
        combined = matched_combined_average(snap)
        worker.dashboard["strategy_mode"] = "inventory_pair_accumulation"
        worker.dashboard["pair_avg_price_c"] = round((combined or 0) * 100, 2) if combined else 0.0
        worker.dashboard["yes_shares"] = round(snap.yes_shares, 2)
        worker.dashboard["no_shares"] = round(snap.no_shares, 2)
        cid = worker.canonical_market_id() or ""
        rec = inventory_strategy_store.cycle_for_market(self._key(worker), cid)
        hedge_fill, dca_fill = inventory_strategy_store.dashboard_fill_buckets(rec)
        worker.dashboard["first_leg_fill"] = hedge_fill
        worker.dashboard["second_leg_fill"] = dca_fill
        worker.dashboard["hedge_fills"] = hedge_fill
        worker.dashboard["dca_fills"] = dca_fill
        if rec:
            worker.dashboard["hedge_deadline_at"] = rec.hedge_deadline_at
            worker.dashboard["accumulation_phase"] = rec.phase.value

    async def on_market_tick(self, worker: "MarketWorker") -> None:
        if shutdown_requested():
            return
        if not worker.worker_config.inventory_accumulation_enabled:
            worker.dashboard["entry_block_reason"] = "inventory_accumulation_disabled"
            return
        cid = worker.canonical_market_id()
        if not cid or not worker.active_market:
            return
        self._publish_dashboard(worker)
        if worker.order_state.name == "PENDING" or worker._order_lock.locked():
            return
        if worker.market_seconds_left() <= worker.worker_config.min_time_before_close_seconds:
            worker.dashboard["entry_block_reason"] = "too_close_to_resolution"
            return

        wc = worker.worker_config
        size = float(wc.min_order_size)
        yes_exe = worker.executable_buy("YES", size)
        no_exe = worker.executable_buy("NO", size)
        snap = InventorySnapshot.from_ledger(worker.inventory, fee_rate=wc.spread_capture_fee_rate)
        wkey = self._key(worker)
        rec = inventory_strategy_store.cycle_for_market(wkey, cid)
        deadline = 0.0
        if rec and rec.first_fill_at > 0:
            deadline = rec.hedge_deadline_at or hedge_deadline_from_fill(
                rec.first_fill_at,
                hedge_delay_seconds=float(wc.initial_hedge_delay_seconds),
                hedge_timeout_seconds=float(wc.initial_hedge_timeout_seconds),
            )
        decision = evaluate_inventory_tick(
            snap,
            now=time.time(),
            hedge_deadline_at=deadline,
            first_leg_side=rec.first_leg_side if rec else "",
            yes_exe=yes_exe,
            no_exe=no_exe,
            cfg=self._arb_cfg(worker),
        )
        worker.dashboard["inventory_next_action"] = decision.action.value
        worker.dashboard["inventory_action_reason"] = decision.reason
        if decision.action != ArbitrageAction.NONE:
            await self._execute_decision(worker, decision, cid=cid)
        self._publish_dashboard(worker)

    async def on_binance_tick(self, worker: "MarketWorker") -> None:
        await self.on_market_tick(worker)

    async def evaluate(self, worker: "MarketWorker"):
        return None

    async def execute(self, worker: "MarketWorker", decision) -> None:
        return None
