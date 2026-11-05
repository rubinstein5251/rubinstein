"""Momentum-first dynamic 10/10 inventory accumulation (FAK only)."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Optional

from strategies.fok_execution import fak_buy_limit
from utils.clob_helpers import clamp_buy_price
from utils.graceful_shutdown import shutdown_requested
from utils.inventory_balance import BuyApproval, log_buy_plan, plan_single_leg_buy
from utils.inventory_cost import InventorySnapshot, matched_combined_average
from utils.inventory_strategy_store import (
    AccumulationPhase,
    inventory_strategy_store,
)
from utils.momentum_inventory_engine import (
    EntryStatus,
    InventoryBuyAction,
    InventoryPhase,
    config_from_worker,
    evaluate_momentum_inventory_tick,
)

if TYPE_CHECKING:
    from bot import MarketWorker

_LOG = logging.getLogger("inventory_pair_accumulation")


class InventoryPairAccumulationStrategy:
    def _key(self, worker: "MarketWorker") -> str:
        return worker.worker_config.key

    def _max_shares_per_side(self, worker: "MarketWorker") -> float:
        wc = worker.worker_config
        return float(getattr(wc, "target_shares_per_side", 0) or wc.max_shares or wc.max_position_size)

    def _purpose_for_action(self, action: InventoryBuyAction) -> str:
        if action == InventoryBuyAction.MOMENTUM_ENTRY:
            return "momentum_entry"
        if action == InventoryBuyAction.ACCUMULATE_OPPOSITE:
            return "initial_hedge"
        if action == InventoryBuyAction.COST_OPTIMIZE:
            return "inventory_dca"
        if action in (InventoryBuyAction.BALANCE, InventoryBuyAction.ACCUMULATE_PRIMARY):
            return "inventory_rebalance"
        return "pair_accumulation"

    async def _record_buy(
        self,
        worker: "MarketWorker",
        side: str,
        qty: float,
        fill_px: float,
        limit_px: float,
        oid: Optional[str],
        *,
        action: InventoryBuyAction,
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
        rec = inventory_strategy_store.cycle_for_market(wkey, cid)
        if action == InventoryBuyAction.MOMENTUM_ENTRY:
            wc = worker.worker_config
            inventory_strategy_store.start_cycle(
                wkey,
                cid,
                first_leg_side=side,
                hedge_delay_seconds=float(wc.initial_hedge_delay_seconds),
                hedge_timeout_seconds=float(
                    getattr(wc, "hedge_pending_timeout_seconds", 0)
                    or wc.initial_hedge_timeout_seconds
                ),
            )
        elif rec and action in (
            InventoryBuyAction.ACCUMULATE_OPPOSITE,
            InventoryBuyAction.ACCUMULATE_PRIMARY,
        ):
            inventory_strategy_store.record_cycle_leg_fill(
                rec,
                leg="hedge",
                side=side,
                qty=qty,
                price=fill_px,
                kind=action.value,
            )
            if worker.inventory.yes_shares > 0 and worker.inventory.no_shares > 0:
                rec.initial_hedge_complete = True
                rec.phase = AccumulationPhase.PAIR_COMPLETED
            inventory_strategy_store.upsert(rec)
        elif rec and action in (InventoryBuyAction.COST_OPTIMIZE, InventoryBuyAction.BALANCE):
            inventory_strategy_store.record_cycle_leg_fill(
                rec,
                leg="dca",
                side=side,
                qty=qty,
                price=fill_px,
                kind=action.value,
            )
            inventory_strategy_store.upsert(rec)

        cfg = config_from_worker(worker.worker_config)
        target = cfg.target_shares_per_side
        if (
            worker.inventory.yes_shares >= target - 1e-9
            and worker.inventory.no_shares >= target - 1e-9
            and rec
        ):
            rec.target_achieved = True
            rec.phase = AccumulationPhase.PAIR_COMPLETED
            inventory_strategy_store.upsert(rec)

        worker.dashboard["entry_fill_price"] = round(fill_px, 4)
        worker.dashboard["strategy_state"] = action.value
        worker.dashboard["last_fak_fill_qty"] = round(qty, 4)
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

        if decision.action == InventoryBuyAction.NONE or decision.qty <= 0:
            return
        purpose = self._purpose_for_action(decision.action)
        allow_first = decision.action == InventoryBuyAction.MOMENTUM_ENTRY
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
            max_unmatched_shares=max(
                float(wc.max_unmatched_shares),
                float(wc.gabagool_max_unpaired_shares),
                self._max_shares_per_side(worker),
            ),
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
            worker.dashboard["fak_order_status"] = "submitted"
            worker.dashboard["inventory_entry_status"] = EntryStatus.EXECUTING.value
            qty_f, fill_px, oid = await fak_buy_limit(
                worker, decision.side, limit_px, qty, purpose="momentum_inventory",
            )
            worker.order_state = OrderState.IDLE
            worker.dashboard["fak_order_status"] = "filled" if qty_f > 0 else "no_fill"
            if qty_f <= 0:
                worker.dashboard["entry_block_reason"] = f"{decision.action.value}_fak_no_fill"
                worker.dashboard["inventory_entry_status"] = EntryStatus.WAITING.value
                return
            worker.dashboard["inventory_entry_status"] = EntryStatus.FILLED.value
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
                        "requested_qty": qty,
                        "fill_px": fill_px,
                        "projected_combined": decision.projected_combined,
                        "ts": time.time(),
                    }
                )
            )

    def _publish_dashboard(self, worker: "MarketWorker", decision) -> None:
        wc = worker.worker_config
        cfg = config_from_worker(wc)
        snap = InventorySnapshot.from_ledger(
            worker.inventory,
            fee_rate=wc.spread_capture_fee_rate,
        )
        combined = matched_combined_average(snap)
        target = cfg.target_shares_per_side
        worker.dashboard["strategy_mode"] = "momentum_inventory_accumulation"
        worker.dashboard["strategy_name"] = "Momentum inventory 10/10"
        worker.dashboard["strategy_label"] = "Momentum-first inventory"
        worker.dashboard["execution_type"] = str(
            getattr(wc, "inventory_execution_type", "FAK") or "FAK"
        ).upper()
        worker.dashboard["pair_avg_price_c"] = round((combined or 0) * 100, 2) if combined else 0.0
        worker.dashboard["matched_combined_cost_c"] = worker.dashboard["pair_avg_price_c"]
        worker.dashboard["yes_shares"] = round(snap.yes_shares, 2)
        worker.dashboard["no_shares"] = round(snap.no_shares, 2)
        worker.dashboard["up_shares"] = worker.dashboard["yes_shares"]
        worker.dashboard["down_shares"] = worker.dashboard["no_shares"]
        worker.dashboard["target_shares_per_side"] = target
        worker.dashboard["remaining_up_shares"] = round(max(0.0, target - snap.yes_shares), 2)
        worker.dashboard["remaining_down_shares"] = round(max(0.0, target - snap.no_shares), 2)
        worker.dashboard["yes_avg_price_c"] = round(snap.yes_avg() * 100, 2) if snap.yes_shares > 0 else 0.0
        worker.dashboard["no_avg_price_c"] = round(snap.no_avg() * 100, 2) if snap.no_shares > 0 else 0.0
        worker.dashboard["yes_spent_usd"] = round(snap.yes_cost, 4)
        worker.dashboard["no_spent_usd"] = round(snap.no_cost, 4)
        worker.dashboard["combined_acquisition_cost_usd"] = round(snap.total_exposure_usd(), 4)
        worker.dashboard["target_reached"] = (
            snap.yes_shares >= target - 1e-9 and snap.no_shares >= target - 1e-9
        )
        worker.dashboard["accumulation_phase"] = decision.phase.value
        worker.dashboard["inventory_next_action"] = decision.action.value
        worker.dashboard["inventory_action_reason"] = decision.reason
        worker.dashboard["hedge_pending_elapsed_s"] = round(decision.hedge_pending_elapsed, 1)
        worker.dashboard["hedge_timeout_reassess"] = decision.hedge_timeout_reassess
        worker.dashboard["max_combined_entry_cost_c"] = round(cfg.max_combined_entry_cost * 100, 2)

        min_entry_c = round(cfg.momentum_entry_min_price * 100, 1)
        worker.dashboard["momentum_entry_min_price_c"] = min_entry_c
        mom_side = decision.momentum_side or (
            decision.momentum.momentum_side if decision.momentum else None
        )
        worker.dashboard["momentum_side"] = mom_side or "—"
        worker.dashboard["momentum_confirmed_side"] = worker.dashboard["momentum_side"]
        ask = decision.momentum_side_ask
        if ask <= 0 and mom_side:
            ask = float(worker.best_ask(mom_side))
        worker.dashboard["momentum_side_ask_c"] = round(ask * 100, 2) if ask > 0 else 0.0
        if worker.order_state.name == "PENDING":
            entry_status = EntryStatus.EXECUTING.value
        elif worker.dashboard.get("inventory_entry_status") == EntryStatus.FILLED.value:
            entry_status = EntryStatus.FILLED.value
        else:
            entry_status = decision.entry_status
        worker.dashboard["inventory_entry_status"] = entry_status

        cid = worker.canonical_market_id() or ""
        rec = inventory_strategy_store.cycle_for_market(self._key(worker), cid)
        hedge_fill, dca_fill = inventory_strategy_store.dashboard_fill_buckets(rec)
        worker.dashboard["first_leg_fill"] = hedge_fill
        worker.dashboard["second_leg_fill"] = dca_fill
        worker.dashboard["hedge_fills"] = hedge_fill
        worker.dashboard["dca_fills"] = dca_fill
        if rec:
            worker.dashboard["hedge_deadline_at"] = rec.hedge_deadline_at
            worker.dashboard["first_fill_at"] = rec.first_fill_at

        if combined is not None and snap.matched_pairs > 0:
            gross = snap.matched_pairs - snap.matched_pairs * combined
            worker.dashboard["expected_gross_settlement_surplus"] = round(gross, 4)
            worker.dashboard["expected_net_settlement_surplus"] = round(gross, 4)
        worker.dashboard["unmatched_exposure_shares"] = round(
            max(snap.unpaired_yes, snap.unpaired_no), 2,
        )

    async def on_market_tick(self, worker: "MarketWorker") -> None:
        if shutdown_requested():
            return
        if not worker.worker_config.inventory_accumulation_enabled:
            worker.dashboard["entry_block_reason"] = "inventory_accumulation_disabled"
            return
        cid = worker.canonical_market_id()
        if not cid or not worker.active_market:
            return
        if worker.order_state.name == "PENDING" or worker._order_lock.locked():
            worker.dashboard["inventory_entry_status"] = EntryStatus.EXECUTING.value
            return
        if worker.market_seconds_left() <= worker.worker_config.min_time_before_close_seconds:
            worker.dashboard["entry_block_reason"] = "too_close_to_resolution"
            return

        wc = worker.worker_config
        cfg = config_from_worker(wc)
        now = time.time()
        snap = InventorySnapshot.from_ledger(worker.inventory, fee_rate=wc.spread_capture_fee_rate)
        wkey = self._key(worker)
        rec = inventory_strategy_store.cycle_for_market(wkey, cid)
        first_fill_at = rec.first_fill_at if rec else 0.0

        if (
            snap.yes_shares >= cfg.target_shares_per_side - 1e-9
            and snap.no_shares >= cfg.target_shares_per_side - 1e-9
        ):
            idle_decision = evaluate_momentum_inventory_tick(
                snap,
                now=now,
                yes_history=list(worker._ask_history.get("YES", [])),
                no_history=list(worker._ask_history.get("NO", [])),
                yes_exe=worker.executable_buy_partial("YES", wc.min_order_size),
                no_exe=worker.executable_buy_partial("NO", wc.min_order_size),
                cfg=cfg,
                first_fill_at=first_fill_at,
            )
            self._publish_dashboard(worker, idle_decision)
            worker.dashboard["strategy_state"] = InventoryPhase.TARGET_REACHED.value
            return

        size_probe = min(cfg.max_purchase_size, cfg.target_shares_per_side)
        yes_exe = worker.executable_buy_partial("YES", size_probe, require_full_size=False)
        no_exe = worker.executable_buy_partial("NO", size_probe, require_full_size=False)

        yes_hist = list(worker._ask_history.get("YES", []))
        no_hist = list(worker._ask_history.get("NO", []))

        decision = evaluate_momentum_inventory_tick(
            snap,
            now=now,
            yes_history=yes_hist,
            no_history=no_hist,
            yes_exe=yes_exe,
            no_exe=no_exe,
            cfg=cfg,
            first_fill_at=first_fill_at,
        )

        self._publish_dashboard(worker, decision)

        if decision.action != InventoryBuyAction.NONE:
            await self._execute_decision(worker, decision, cid=cid)
            self._publish_dashboard(worker, decision)

    async def on_binance_tick(self, worker: "MarketWorker") -> None:
        await self.on_market_tick(worker)

    async def evaluate(self, worker: "MarketWorker"):
        return None

    async def execute(self, worker: "MarketWorker", decision) -> None:
        return None
