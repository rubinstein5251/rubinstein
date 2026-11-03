"""BTC/ETH momentum entry; percentage take-profit and stop-loss FOK exits."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Optional

from strategies.fok_execution import fok_buy_limit, fok_sell_with_retries
from utils.entry_limit import compute_momentum_buy_fok_limit_price, resolve_max_entry_price
from utils.graceful_shutdown import shutdown_requested
from utils.market_reference import infer_oracle_from_gamma, resolve_market_reference
from utils.momentum_engine import MomentumDirection, compute_momentum
from utils.momentum_strategy_entry import StrategyEntryDecision, evaluate_momentum_entry
from utils.momentum_risk import stop_loss_price, stop_loss_triggered
from utils.profitability_gate import check_executable_depth
from utils.stopped_out_store import stopped_out_store
from utils.take_profit import (
    take_profit_price,
    take_profit_triggered,
    validate_take_profit_for_entry,
)
from utils.exit_performance import exit_performance_store
from utils.order_book_math import executable_sell_from_bids_at_min
from utils.signal_candidate_log import CandidateSignalRecord, signal_candidate_log
from utils.signal_quality_gate import (
    SignalDecision,
    config_from_worker,
    evaluate_signal_quality,
)
from utils.entry_rejection_counters import entry_rejection_counters

if TYPE_CHECKING:
    from bot import MarketWorker

_LOG = logging.getLogger("momentum_delayed_adjustment")


class MomentumDelayedAdjustmentStrategy:
    def __init__(self) -> None:
        self._position_opened_at: dict[str, float] = {}
        self._exit_submit_guard: set[str] = set()
        self._market_open_ask: dict[str, dict[str, float]] = {}
    def _key(self, worker: "MarketWorker") -> str:
        return worker.worker_config.key

    @staticmethod
    def _valid_trade_side(raw) -> Optional[str]:
        side = str(raw or "").upper().strip()
        if side in ("YES", "NO"):
            return side
        return None

    def _primary_side(self, worker: "MarketWorker") -> Optional[str]:
        for raw in (
            worker.dashboard.get("bought_side"),
            getattr(worker, "position_side", None),
        ):
            side = self._valid_trade_side(raw)
            if side:
                return side
        return self._position_side(worker)

    def _position_side(self, worker: "MarketWorker") -> Optional[str]:
        inv = worker.inventory
        ys, ns = inv.yes_shares, inv.no_shares
        from bot import MIN_SHARES

        has_yes = ys >= MIN_SHARES - 1e-9
        has_no = ns >= MIN_SHARES - 1e-9
        if has_yes and has_no:
            bought = self._valid_trade_side(
                worker.dashboard.get("bought_side")
                or getattr(worker, "position_side", None)
            )
            if bought:
                return bought
            return "YES" if ys >= ns else "NO"
        if has_yes:
            return "YES"
        if has_no:
            return "NO"
        return None

    def _directional_shares(self, worker: "MarketWorker", side: str) -> float:
        from bot import MIN_SHARES

        inv = worker.inventory
        ys, ns = inv.yes_shares, inv.no_shares
        primary = self._valid_trade_side(side)
        if not primary:
            return 0.0
        if primary == "YES" and ns >= MIN_SHARES - 1e-9 and ys < MIN_SHARES - 1e-9:
            return 0.0
        if primary == "NO" and ys >= MIN_SHARES - 1e-9 and ns < MIN_SHARES - 1e-9:
            return 0.0
        return inv.shares(primary)

    def _external_context(self, worker: "MarketWorker"):
        from utils.external_market_data import snapshot_from_binance_signal

        signal = worker.binance_signal()
        cfg = worker.worker_config
        snap = snapshot_from_binance_signal(
            signal,
            max_age_seconds=cfg.external_max_data_age_seconds,
        )
        history = list(getattr(signal, "_price_history", []) or []) if signal else []
        return signal, snap, history

    def _remember_open_asks(self, worker: "MarketWorker", cid: str) -> None:
        if not cid:
            return
        if cid in self._market_open_ask:
            return
        yes_ask = float(worker.best_ask("YES"))
        no_ask = float(worker.best_ask("NO"))
        if yes_ask <= 0 and no_ask <= 0:
            return
        self._market_open_ask[cid] = {"YES": yes_ask, "NO": no_ask}

    def _open_ask(self, cid: str, side: str) -> Optional[float]:
        blob = self._market_open_ask.get(cid) or {}
        val = blob.get(side)
        return float(val) if val and val > 0 else None

    def _compute_momentum(self, worker: "MarketWorker", history, snap) -> "MomentumSnapshot":
        cfg = worker.worker_config
        windows = tuple(int(w) for w in cfg.momentum_windows_seconds)
        return compute_momentum(
            history,
            windows_sec=windows,
            min_strength=cfg.min_momentum_strength,
            max_data_age_sec=cfg.external_max_data_age_seconds,
            last_update_ts=snap.timestamp if snap else 0.0,
            order_book_imbalance=snap.order_book_imbalance if snap else 0.0,
            volume_confirmation_threshold=cfg.volume_confirmation_threshold,
            volatility_cap=cfg.momentum_volatility_cap,
            trigger_window_seconds=int(cfg.momentum_trigger_window_seconds),
            confirm_window_seconds=int(cfg.momentum_confirm_window_seconds),
            support_window_seconds=int(cfg.momentum_support_window_seconds),
            context_window_seconds=int(cfg.momentum_context_window_seconds),
            trigger_min_abs_return=float(cfg.momentum_trigger_min_abs_return),
            confirm_tolerance=float(cfg.momentum_confirm_tolerance),
            support_opposing_tolerance=float(cfg.momentum_support_opposing_tolerance),
            context_reversal_min_abs_return=float(cfg.momentum_context_reversal_min_abs_return),
            require_volume_confirmation=bool(cfg.momentum_require_volume_confirmation),
        )

    def _record_entry_rejection(
        self,
        worker: "MarketWorker",
        *,
        stage: str,
        reason: str,
        candidate: bool = False,
    ) -> None:
        wkey = self._key(worker)
        entry_rejection_counters.record(
            wkey, stage=stage, reason=reason, candidate=candidate,
        )
        worker.dashboard["entry_rejection_by_reason"] = dict(
            entry_rejection_counters.snapshot(wkey).by_reason
        )
        worker.dashboard["entry_rejection_by_stage"] = dict(
            entry_rejection_counters.snapshot(wkey).by_stage
        )

    def _log_signal_candidate(
        self,
        worker: "MarketWorker",
        *,
        cid: str,
        momentum,
        ref,
        choice,
        quality,
        decision: str,
        rejection_reason: str,
        order_submitted: bool = False,
        fok_result: str = "",
        fill_price: float = 0.0,
    ) -> None:
        prob = choice.probability if choice else None
        side = quality.side if quality else (choice.side if choice else "")
        yes_ask = float(worker.best_ask("YES"))
        no_ask = float(worker.best_ask("NO"))
        record = CandidateSignalRecord(
            ts=time.time(),
            asset=worker.asset_type,
            window=worker.window_slug,
            market_id=cid,
            side=side or "",
            seconds_remaining=float(worker.market_seconds_left()),
            decision=decision,
            rejection_reason=rejection_reason,
            returns={f"{k}s": round(v * 100, 6) for k, v in momentum.returns.items()},
            acceleration=round(momentum.acceleration, 8),
            realized_volatility=round(momentum.realized_volatility, 8),
            momentum_direction=momentum.direction.name,
            momentum_strength=round(momentum.strength, 6),
            reference_price=round(ref.reference_price, 4) if ref.reference_price else 0.0,
            current_underlying=round(ref.current_price, 4) if ref.current_price else 0.0,
            yes_ask=round(yes_ask, 4),
            no_ask=round(no_ask, 4),
            spread=round(quality.spread, 4) if quality else 0.0,
            estimated_probability=round(prob.p_yes if side == "YES" else prob.p_no, 4) if prob and side else 0.0,
            gross_edge=round(quality.gross_edge, 4) if quality else 0.0,
            net_edge=round(quality.net_edge, 4) if quality else 0.0,
            signal_score=round(quality.total_score, 2) if quality else 0.0,
            score_components=quality.components.as_dict() if quality else {},
            order_submitted=order_submitted,
            fok_result=fok_result,
            fill_price=fill_price,
        )
        signal_candidate_log.append(record)
        _LOG.info(json.dumps({"event": "signal_candidate", **record.to_dict()}))
        wkey = f"{worker.asset_type}:{worker.window_slug}"
        entry_rejection_counters.record(
            wkey,
            stage="candidate_log",
            reason=rejection_reason or decision,
            candidate=True,
            enter=decision == "ENTER",
            order_submitted=order_submitted,
            fok_filled=fok_result == "FILLED",
            fok_failed=fok_result == "FAILED",
        )

    def _market_reference(self, worker: "MarketWorker", history, current_price: float):
        am = worker.active_market or {}
        start_ts = int(am.get("start_ts") or 0)
        if start_ts <= 0 and am.get("slug"):
            try:
                start_ts = int(str(am["slug"]).rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                start_ts = 0
        oracle = str(am.get("oracle_label") or "chainlink_proxy")
        return resolve_market_reference(
            market_start_ts=start_ts,
            interval_seconds=worker.worker_config.interval_seconds,
            price_history=history,
            current_binance_price=current_price,
            oracle_label=oracle,
            now_ts=time.time(),
            binance_symbol=f"{worker.asset_type.upper()}USDT",
        )

    def _publish_dashboard(self, worker: "MarketWorker") -> None:
        cfg = worker.worker_config
        cid = worker.canonical_market_id() or ""
        wkey = self._key(worker)
        stopped = stopped_out_store.is_stopped_out(wkey, cid) if cid else False
        side = self._position_side(worker)
        inv = worker.inventory
        max_res = worker.resolved_momentum_max_entry()

        signal, snap, history = self._external_context(worker)
        ref = self._market_reference(worker, history, snap.price if snap else 0.0)
        momentum = self._compute_momentum(worker, history, snap)

        worker.dashboard["strategy_name"] = "BTC/ETH Momentum"
        worker.dashboard.update(entry_rejection_counters.dashboard_payload(wkey))
        worker.dashboard["max_entry_price"] = max_res.minimum_price
        worker.dashboard["max_entry_price_c"] = round(max_res.minimum_price * 100, 2)
        worker.dashboard["min_net_edge"] = cfg.min_net_edge
        worker.dashboard["min_net_edge_pct"] = round(cfg.min_net_edge * 100, 2)
        worker.dashboard["stop_loss_pct"] = cfg.stop_loss_pct
        worker.dashboard["take_profit_pct"] = cfg.take_profit_pct
        worker.dashboard.update(exit_performance_store.dashboard_payload(wkey))
        worker.dashboard["order_size"] = cfg.order_size_max
        worker.dashboard["execution_type"] = "FOK"
        worker.dashboard["market_id"] = cid
        worker.dashboard["market_stopped_out"] = stopped
        worker.dashboard["position_side"] = side or ""
        worker.dashboard["reference_price"] = round(ref.reference_price, 2) if ref.reference_price else 0.0
        worker.dashboard["current_underlying_price"] = round(ref.current_price, 2) if ref.current_price else 0.0
        worker.dashboard["resolution_oracle"] = ref.oracle_label
        worker.dashboard["reference_synchronized"] = ref.synchronized
        worker.dashboard["seconds_remaining"] = worker.market_seconds_left()

        for w in (5, 15, 30, 60, 120):
            worker.dashboard[f"return_{w}s"] = round(momentum.returns.get(w, 0.0) * 100, 4)
        worker.dashboard["momentum_direction"] = momentum.direction.name
        worker.dashboard["momentum_strength"] = round(momentum.strength, 4)
        worker.dashboard["realized_volatility"] = round(momentum.realized_volatility, 6)
        worker.dashboard["volume_confirmation"] = momentum.volume_confirmed
        worker.dashboard["signal_confidence"] = round(momentum.confidence, 4)

        yes_ask = float(worker.best_ask("YES"))
        no_ask = float(worker.best_ask("NO"))
        worker.dashboard["yes_ask"] = round(yes_ask, 4)
        worker.dashboard["no_ask"] = round(no_ask, 4)
        worker.dashboard["yes_ask_c"] = round(yes_ask * 100, 2)
        worker.dashboard["no_ask_c"] = round(no_ask * 100, 2)

        choice = evaluate_momentum_entry(
            yes_ask=yes_ask,
            no_ask=no_ask,
            reference=ref,
            momentum=momentum,
            seconds_remaining=worker.market_seconds_left(),
            interval_seconds=cfg.interval_seconds,
            min_net_edge=cfg.min_net_edge,
            max_entry_price=max_res.minimum_price,
            fee_rate=cfg.estimated_fee_rate,
            slippage_buffer=cfg.execution_slippage_buffer,
            min_time_remaining=cfg.min_time_before_close_seconds,
        )
        cid_dash = worker.canonical_market_id() or ""
        self._remember_open_asks(worker, cid_dash)
        worker.dashboard["signal_quality_threshold"] = cfg.min_signal_quality_score
        if choice.decision == StrategyEntryDecision.ENTER and choice.side:
            exe_preview = worker.executable_buy(choice.side, float(cfg.order_size_max))
            if exe_preview:
                gate_cfg = config_from_worker(cfg)
                q_preview = evaluate_signal_quality(
                    momentum=momentum,
                    reference=ref,
                    probability=choice.probability,
                    side=choice.side,
                    yes_ask=yes_ask,
                    no_ask=no_ask,
                    yes_bid=float(worker.best_bid("YES")),
                    no_bid=float(worker.best_bid("NO")),
                    executable_vwap=float(exe_preview[0]),
                    executable_filled=float(exe_preview[1]),
                    order_size=float(cfg.order_size_max),
                    seconds_remaining=float(worker.market_seconds_left()),
                    interval_seconds=cfg.interval_seconds,
                    min_time_remaining=float(cfg.min_time_before_close_seconds),
                    token_ask_at_open=self._open_ask(cid_dash, choice.side),
                    cfg=gate_cfg,
                    quote_stale=getattr(worker, "_ws_market_data_stale", False),
                )
                worker.dashboard["signal_quality_score"] = q_preview.total_score
                worker.dashboard["signal_quality_components"] = q_preview.components.as_dict()
                worker.dashboard["entry_net_edge"] = round(q_preview.net_edge, 4)
                worker.dashboard["entry_decision"] = (
                    "ENTER" if q_preview.decision == SignalDecision.ENTER else "SKIP"
                )
                worker.dashboard["entry_block_reason"] = (
                    "" if q_preview.decision == SignalDecision.ENTER else q_preview.reason
                )
            else:
                worker.dashboard["entry_decision"] = "SKIP"
                worker.dashboard["entry_block_reason"] = "no_liquidity"
        else:
            worker.dashboard["entry_decision"] = "SKIP"
            worker.dashboard["entry_block_reason"] = choice.reason
            worker.dashboard["signal_quality_score"] = 0.0
        prob = choice.probability
        if prob:
            worker.dashboard["estimated_yes_probability"] = round(prob.p_yes, 4)
            worker.dashboard["estimated_no_probability"] = round(prob.p_no, 4)
            worker.dashboard["yes_net_edge"] = round(prob.yes_edge, 4)
            worker.dashboard["no_net_edge"] = round(prob.no_edge, 4)
            worker.dashboard["selected_trading_direction"] = prob.selected_side or ""
        else:
            worker.dashboard["estimated_yes_probability"] = 0.0
            worker.dashboard["estimated_no_probability"] = 0.0
            worker.dashboard["yes_net_edge"] = 0.0
            worker.dashboard["no_net_edge"] = 0.0
            worker.dashboard["selected_trading_direction"] = ""

        if side:
            entry = inv.avg_cost(side)
            worker.dashboard["entry_fill_price"] = round(entry, 4)
            worker.dashboard["entry_fill_price_c"] = round(entry * 100, 2)
            worker.dashboard["position_size"] = round(inv.shares(side), 4)
            bid = float(worker.best_bid(side))
            worker.dashboard["executable_bid"] = round(bid, 4)
            worker.dashboard["executable_bid_c"] = round(bid * 100, 2) if bid > 0 else 0
            stop_px = stop_loss_price(entry, cfg.stop_loss_pct)
            worker.dashboard["stop_loss_trigger_price"] = round(stop_px, 4)
            worker.dashboard["stop_loss_trigger_c"] = round(stop_px * 100, 2)
            tp_res = take_profit_price(entry, cfg.take_profit_pct)
            worker.dashboard["take_profit_trigger_price"] = round(tp_res.trigger_price, 4)
            worker.dashboard["take_profit_trigger_c"] = round(tp_res.trigger_price * 100, 2)
            if hasattr(worker, "sync_accounting_dashboard"):
                worker.sync_accounting_dashboard()
            worker.dashboard["exit_order_type"] = worker.dashboard.get("exit_order_type") or "FOK"
            if bid > 0 and take_profit_triggered(bid, tp_res.trigger_price):
                worker.dashboard.setdefault("strategy_state", "take_profit_triggered")
            elif bid > 0 and stop_loss_triggered(bid, entry, cfg.stop_loss_pct):
                worker.dashboard.setdefault("strategy_state", "stop_loss_triggered")
            else:
                worker.dashboard.setdefault("strategy_state", "position_open")
        elif worker.dashboard.get("entry_order_status") != "FILLED":
            worker.dashboard["entry_fill_price"] = 0.0
            worker.dashboard["entry_fill_price_c"] = 0.0
            worker.dashboard["position_size"] = 0.0
            worker.dashboard["executable_bid"] = 0.0
            worker.dashboard["executable_bid_c"] = 0.0
            worker.dashboard["stop_loss_trigger_price"] = 0.0
            worker.dashboard["stop_loss_trigger_c"] = 0.0
            worker.dashboard["take_profit_trigger_price"] = 0.0
            worker.dashboard["take_profit_trigger_c"] = 0.0
            worker.dashboard["unrealized_pnl"] = 0.0
            if hasattr(worker, "sync_accounting_dashboard"):
                worker.sync_accounting_dashboard()
            worker.dashboard["exit_fill_price"] = 0.0
            worker.dashboard["exit_fill_price_c"] = 0.0
            worker.dashboard["exit_reason"] = ""

        if hasattr(worker, "sync_accounting_dashboard"):
            worker.sync_accounting_dashboard()
        worker.dashboard["entry_candidate_side"] = choice.side or ""
        if not side and choice.decision == StrategyEntryDecision.ENTER:
            worker.dashboard["strategy_state"] = worker.dashboard.get("strategy_state") or "edge_detected"
        elif momentum.stale:
            worker.dashboard.setdefault("strategy_state", "waiting_for_market_data")
        elif not ref.synchronized:
            worker.dashboard.setdefault("strategy_state", "waiting_for_market_data")
        elif momentum.direction == MomentumDirection.NEUTRAL:
            worker.dashboard.setdefault("strategy_state", "waiting_for_momentum")
        elif momentum.direction in (MomentumDirection.UP, MomentumDirection.DOWN):
            worker.dashboard.setdefault("strategy_state", "momentum_detected")

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
        signal, snap, _ = self._external_context(worker)
        if signal is None:
            return "binance_feed_not_connected"
        if snap is None:
            return "binance_price_unavailable"
        if snap.stale:
            worker.dashboard["external_price_age_sec"] = round(snap.age_seconds, 2)
            return "stale_external_price"
        if not cfg.momentum_strategy_enabled:
            return "strategy_disabled"
        return None

    async def _record_buy(
        self,
        worker: "MarketWorker",
        side: str,
        qty: float,
        fill_px: float,
        limit_px: float,
        oid: Optional[str],
        momentum_dir: MomentumDirection,
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
        notify = getattr(worker, "notify_confirmed_fill", None)
        if callable(notify):
            notify(side=side, action="BUY", quantity=qty, price=fill_px, order_id=oid)
        cid = worker.canonical_market_id() or ""
        wkey = self._key(worker)
        self._position_opened_at[f"{wkey}:{cid}"] = time.time()
        worker.dashboard["buy_limit_price"] = limit_px
        worker.dashboard["buy_limit_price_c"] = round(limit_px * 100, 2)
        worker.dashboard["entry_fill_price"] = round(fill_px, 4)
        worker.dashboard["entry_fill_price_c"] = round(fill_px * 100, 2)
        worker.dashboard["filled_size"] = round(qty, 4)
        worker.dashboard["entry_order_status"] = "FILLED"
        worker.dashboard["strategy_state"] = "position_open"
        worker.dashboard["bought_side"] = side
        label = f"{worker.asset_type.upper()} {worker.window_slug}"
        print(
            f"✅ [BUY FILLED] {label} | {side} | limit_price={limit_px:.4f} | "
            f"avg_fill_price={fill_px:.4f} | filled_size={qty:.2f}"
        )

    async def _record_exit_sell(
        self,
        worker: "MarketWorker",
        side: str,
        qty: float,
        px: float,
        oid: Optional[str],
        *,
        exit_reason: str,
        entry: float,
        target_price: float,
    ) -> None:
        from config import MIN_SHARES

        if qty <= MIN_SHARES - 1e-9:
            return
        if oid and not worker.record_fill_idempotent(oid, side, qty, px):
            return
        if px < target_price - 1e-9 and exit_reason == "take_profit":
            return

        fee_rate = worker.worker_config.estimated_fee_rate
        cost_basis = entry * qty
        proceeds = px * qty
        gross_pnl = proceeds - cost_basis
        exit_fees = fee_rate * proceeds
        entry_fees = fee_rate * cost_basis
        net_pnl = gross_pnl - entry_fees - exit_fees
        fees = entry_fees + exit_fees

        wkey = self._key(worker)
        cid = worker.canonical_market_id() or ""
        opened = self._position_opened_at.get(f"{wkey}:{cid}", time.time())
        holding = max(0.0, time.time() - opened)

        worker.inventory.record_sell(side, qty, px)
        worker.log_trade(side, px, "SELL", size=qty, order_id=oid)
        notify = getattr(worker, "notify_confirmed_fill", None)
        if callable(notify):
            notify(
                side=side,
                action="SELL",
                quantity=qty,
                price=px,
                order_id=oid,
                realized_pnl_delta=net_pnl,
                count_win_loss=True,
            )
        worker.dashboard["exit_order_status"] = "FILLED"
        worker.dashboard["exit_order_type"] = "FOK"
        worker.dashboard["exit_fill_price"] = round(px, 4)
        worker.dashboard["exit_fill_price_c"] = round(px * 100, 2)
        worker.dashboard["exit_avg_fill_price"] = round(px, 4)
        worker.dashboard["exit_target_price"] = round(target_price, 4)
        worker.dashboard["exit_gross_pnl"] = round(gross_pnl, 4)
        worker.dashboard["exit_fees"] = round(fees, 4)
        worker.dashboard["exit_net_pnl"] = round(net_pnl, 4)
        worker.dashboard["closed_quantity"] = round(qty, 4)
        worker.dashboard["entry_avg_fill_price"] = round(entry, 4)

        remaining = worker.inventory.shares(side)
        if remaining >= MIN_SHARES - 1e-9:
            worker.position_size = remaining
            worker.dashboard["position_size"] = round(remaining, 4)
            worker.dashboard["strategy_state"] = "exit_partial_fill"
            worker.dashboard["exit_reason"] = exit_reason
            if exit_reason == "stop_loss":
                worker.dashboard["stop_loss_event"] = "STOP_LOSS_PARTIAL_FILL"
            log_pnl = getattr(worker, "log_pnl", None)
            if callable(log_pnl):
                log_pnl(
                    "STOP_LOSS" if exit_reason == "stop_loss" else "TAKE_PROFIT",
                    net_pnl,
                    {
                        "order_id": oid,
                        "side": side,
                        "quantity": qty,
                        "exit_price": px,
                        "partial": True,
                    },
                )
            if hasattr(worker, "sync_accounting_dashboard"):
                worker.sync_accounting_dashboard()
            return

        display_reason = "TAKE_PROFIT" if exit_reason == "take_profit" else "STOP_LOSS"
        worker.dashboard["exit_reason"] = display_reason
        if exit_reason == "take_profit":
            worker.dashboard["strategy_state"] = "position_closed_take_profit"
            exit_performance_store.record_take_profit(
                wkey, pnl=net_pnl, fees=fees, holding_sec=holding,
            )
        else:
            worker.dashboard["strategy_state"] = "position_closed_stop_loss"
            worker.dashboard["stop_loss_event"] = "STOP_LOSS_FILLED"
            exit_performance_store.record_stop_loss(
                wkey, pnl=net_pnl, fees=fees, holding_sec=holding,
            )
        log_pnl = getattr(worker, "log_pnl", None)
        if callable(log_pnl):
            log_pnl(
                "STOP_LOSS" if exit_reason == "stop_loss" else "TAKE_PROFIT",
                net_pnl,
                {
                    "order_id": oid,
                    "side": side,
                    "quantity": qty,
                    "exit_price": px,
                    "partial": False,
                },
            )
        if hasattr(worker, "sync_accounting_dashboard"):
            worker.sync_accounting_dashboard()
        worker.position_side = None
        worker.position_size = 0.0
        worker.entry_price = 0.0
        worker.entry_count = 0
        worker.dashboard["buy_limit_price"] = 0.0
        worker.dashboard["buy_limit_price_c"] = 0.0
        self._exit_submit_guard.discard(f"{wkey}:{cid}")

    def _sell_liquidity_ok(
        self, worker: "MarketWorker", side: str, size: float, min_price: float,
    ) -> bool:
        worker.refresh_order_book(side)
        cached = worker._book_cache.get(side)
        if not cached:
            return False
        book = cached[1]
        bids = book.get("bids") if isinstance(book, dict) else getattr(book, "bids", None)
        from bot import MIN_FILL_DELTA

        exe = executable_sell_from_bids_at_min(
            bids, size, min_price, require_full_size=True,
        )
        return bool(exe and exe[0] >= min_price - 1e-9 and exe[1] >= size - MIN_FILL_DELTA)

    async def _handle_take_profit(self, worker: "MarketWorker", cid: str) -> None:
        from bot import MIN_SHARES, OrderState

        cfg = worker.worker_config
        primary = self._primary_side(worker)
        if not primary:
            return
        inv = worker.inventory
        shares = self._directional_shares(worker, primary)
        if shares < MIN_SHARES - 1e-9:
            return
        entry = inv.avg_cost(primary)
        tp_res = take_profit_price(entry, cfg.take_profit_pct)
        if not tp_res.reachable:
            return
        bid = float(worker.best_bid(primary))
        if bid <= 0 or not take_profit_triggered(bid, tp_res.trigger_price):
            return

        wkey = self._key(worker)
        guard_key = f"{wkey}:{cid}"
        if worker._stop_loss_exit_in_flight or guard_key in self._exit_submit_guard:
            return
        tp_limit = tp_res.trigger_price
        if not self._sell_liquidity_ok(worker, primary, shares, tp_limit):
            worker.dashboard["exit_order_status"] = "WAITING_LIQUIDITY"
            return

        async with worker._stop_loss_lock:
            if worker._stop_loss_exit_in_flight:
                return
            worker._stop_loss_exit_in_flight = True
            self._exit_submit_guard.add(guard_key)
            worker.dashboard["exit_order_status"] = "SUBMITTED"
            worker.dashboard["exit_order_type"] = "FOK"
            worker.dashboard["strategy_state"] = "take_profit_fok_submitted"
            worker.dashboard["exit_reason"] = "TAKE_PROFIT_TRIGGERED"
            try:
                if worker.order_state == OrderState.PENDING or worker._order_lock.locked():
                    self._exit_submit_guard.discard(guard_key)
                    return
                async with worker._order_lock:
                    worker.order_state = OrderState.PENDING
                    qty, px, oid = await fok_sell_with_retries(
                        worker,
                        primary,
                        shares,
                        purpose="take_profit",
                        limit_price=tp_limit,
                        max_retries=cfg.stop_loss_exit_max_retries,
                        retry_delay_ms=cfg.stop_loss_exit_retry_ms,
                    )
                    worker.order_state = OrderState.IDLE
                    remaining_after = self._directional_shares(worker, primary)
                    if qty > MIN_SHARES - 1e-9 and px > 0:
                        await self._record_exit_sell(
                            worker,
                            primary,
                            qty,
                            px,
                            oid,
                            exit_reason="take_profit",
                            entry=entry,
                            target_price=tp_limit,
                        )
                    if qty <= MIN_SHARES - 1e-9 or px <= 0:
                        worker.dashboard["exit_order_status"] = "FAILED"
                        worker.dashboard["exit_reason"] = "TAKE_PROFIT_ORDER_FAILED"
                    if remaining_after >= MIN_SHARES - 1e-9:
                        worker.dashboard["strategy_state"] = "exit_retry"
                        worker.dashboard["exit_order_status"] = "RETRY"
                        self._exit_submit_guard.discard(guard_key)
            finally:
                worker._stop_loss_exit_in_flight = False

    async def _handle_stop_loss(self, worker: "MarketWorker", cid: str) -> None:
        from bot import MIN_SHARES, OrderState

        cfg = worker.worker_config
        primary = self._primary_side(worker)
        if not primary:
            return
        inv = worker.inventory
        side = primary
        shares = self._directional_shares(worker, side)
        if shares < MIN_SHARES - 1e-9:
            return
        entry = inv.avg_cost(side)
        bid = float(worker.best_bid(side))
        if bid <= 0 or not stop_loss_triggered(bid, entry, cfg.stop_loss_pct):
            return

        wkey = self._key(worker)
        stopped_out_store.mark_stopped_out(wkey, cid)
        worker.dashboard["market_stopped_out"] = True
        worker.dashboard["strategy_state"] = "stop_loss_triggered"
        worker.dashboard["exit_reason"] = "STOP_LOSS_TRIGGERED"
        worker.dashboard["stop_loss_event"] = "STOP_LOSS_TRIGGERED"
        guard_key = f"{wkey}:{cid}"
        if worker._stop_loss_exit_in_flight or guard_key in self._exit_submit_guard:
            return
        stop_limit = stop_loss_price(entry, cfg.stop_loss_pct)
        async with worker._stop_loss_lock:
            if worker._stop_loss_exit_in_flight:
                return
            worker._stop_loss_exit_in_flight = True
            self._exit_submit_guard.add(guard_key)
            worker.dashboard["exit_order_status"] = "SUBMITTED"
            worker.dashboard["exit_order_type"] = "FOK"
            worker.dashboard["strategy_state"] = "stop_loss_fok_submitted"
            worker.dashboard["exit_reason"] = "STOP_LOSS_TRIGGERED"
            worker.dashboard["stop_loss_event"] = "STOP_LOSS_ORDER_SUBMITTED"
            try:
                if worker.order_state == OrderState.PENDING or worker._order_lock.locked():
                    self._exit_submit_guard.discard(guard_key)
                    return
                if shares < MIN_SHARES - 1e-9:
                    return
                async with worker._order_lock:
                    worker.order_state = OrderState.PENDING
                    qty, px, oid = await fok_sell_with_retries(
                        worker,
                        side,
                        shares,
                        purpose="stop_loss",
                        limit_price=stop_limit,
                        max_retries=cfg.stop_loss_exit_max_retries,
                        retry_delay_ms=cfg.stop_loss_exit_retry_ms,
                    )
                    worker.order_state = OrderState.IDLE
                    remaining_after = self._directional_shares(worker, side)
                    if qty > MIN_SHARES - 1e-9 and px > 0:
                        await self._record_exit_sell(
                            worker,
                            side,
                            qty,
                            px,
                            oid,
                            exit_reason="stop_loss",
                            entry=entry,
                            target_price=stop_limit,
                        )
                    if qty <= MIN_SHARES - 1e-9 or px <= 0:
                        worker.dashboard["exit_order_status"] = "FAILED"
                        worker.dashboard["stop_loss_event"] = "STOP_LOSS_ORDER_FAILED"
                    if remaining_after >= MIN_SHARES - 1e-9:
                        worker.dashboard["strategy_state"] = "exit_retry"
                        worker.dashboard["exit_order_status"] = "RETRY"
                        worker.dashboard["entry_block_reason"] = "stop_loss_exit_incomplete"
                        self._exit_submit_guard.discard(guard_key)
                    else:
                        worker.dashboard["entry_block_reason"] = "stopped_out_market"
                        if qty > MIN_SHARES - 1e-9:
                            worker.dashboard["stop_loss_event"] = "POSITION_CLOSED"
            finally:
                worker._stop_loss_exit_in_flight = False

    async def _try_entry(self, worker: "MarketWorker", cid: str) -> None:
        from bot import MIN_SHARES, OrderState, is_trading_allowed

        if shutdown_requested():
            worker.dashboard["entry_block_reason"] = "shutdown_in_progress"
            return

        cfg = worker.worker_config
        if not cfg.live_trading_enabled and not worker.is_dry_run():
            worker.dashboard["entry_block_reason"] = "live_trading_disabled_use_dry_run"
            worker.dashboard["strategy_state"] = "idle"
            return

        wkey = self._key(worker)
        label = f"{worker.asset_type.upper()} {worker.window_slug}"
        if stopped_out_store.is_stopped_out(wkey, cid):
            worker.dashboard["strategy_state"] = "market_blocked_after_stop_loss"
            worker.dashboard["entry_block_reason"] = "stopped_out_market"
            worker.dashboard["market_stopped_out"] = True
            return

        inv = worker.inventory
        if inv.yes_shares >= MIN_SHARES - 1e-9 and inv.no_shares >= MIN_SHARES - 1e-9:
            worker.dashboard["entry_block_reason"] = "dual_side_inventory_blocked"
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
            worker.dashboard["strategy_state"] = "waiting_for_market_data"
            return
        if not worker.can_enter():
            worker.dashboard["entry_block_reason"] = "outside_entry_window"
            return
        if not is_trading_allowed():
            worker.dashboard["entry_block_reason"] = "trading_schedule_blocked"
            return

        max_res = worker.resolved_momentum_max_entry()
        size = worker.entry_order_size(["YES", "NO"])
        if not size or size < cfg.min_order_size:
            worker.dashboard["entry_block_reason"] = "invalid_order_size"
            return

        signal, snap, history = self._external_context(worker)
        ref = self._market_reference(worker, history, snap.price if snap else 0.0)
        momentum = self._compute_momentum(worker, history, snap)
        self._record_entry_rejection(
            worker, stage="pipeline", reason="candidate_tick", candidate=True,
        )
        worker.dashboard["strategy_state"] = "calculating_probability"

        yes_ask = float(worker.best_ask("YES"))
        no_ask = float(worker.best_ask("NO"))
        choice = evaluate_momentum_entry(
            yes_ask=yes_ask,
            no_ask=no_ask,
            reference=ref,
            momentum=momentum,
            seconds_remaining=worker.market_seconds_left(),
            interval_seconds=cfg.interval_seconds,
            min_net_edge=cfg.min_net_edge,
            max_entry_price=max_res.minimum_price,
            fee_rate=cfg.estimated_fee_rate,
            slippage_buffer=cfg.execution_slippage_buffer,
            min_time_remaining=cfg.min_time_before_close_seconds,
        )

        if choice.decision != StrategyEntryDecision.ENTER or not choice.side:
            worker.dashboard["entry_block_reason"] = choice.reason
            worker.dashboard["strategy_state"] = "waiting_for_price_adjustment"
            self._record_entry_rejection(
                worker, stage="momentum_or_probability", reason=choice.reason, candidate=True,
            )
            self._log_signal_candidate(
                worker,
                cid=cid,
                momentum=momentum,
                ref=ref,
                choice=choice,
                quality=None,
                decision="SKIP",
                rejection_reason=choice.reason,
            )
            return

        side = choice.side
        worker.dashboard["strategy_state"] = "entry_eligible"
        exe = worker.executable_buy(side, float(size))
        if not exe:
            worker.dashboard["entry_block_reason"] = "no_liquidity"
            self._record_entry_rejection(
                worker, stage="order_book", reason="no_liquidity", candidate=True,
            )
            self._log_signal_candidate(
                worker, cid=cid, momentum=momentum, ref=ref, choice=choice, quality=None,
                decision="SKIP", rejection_reason="no_liquidity",
            )
            return

        gate_cfg = config_from_worker(cfg)
        yes_bid = float(worker.best_bid("YES"))
        no_bid = float(worker.best_bid("NO"))
        self._remember_open_asks(worker, cid)
        quality = evaluate_signal_quality(
            momentum=momentum,
            reference=ref,
            probability=choice.probability,
            side=side,
            yes_ask=yes_ask,
            no_ask=no_ask,
            yes_bid=yes_bid,
            no_bid=no_bid,
            executable_vwap=float(exe[0]),
            executable_filled=float(exe[1]),
            order_size=float(size),
            seconds_remaining=float(worker.market_seconds_left()),
            interval_seconds=cfg.interval_seconds,
            min_time_remaining=float(cfg.min_time_before_close_seconds),
            token_ask_at_open=self._open_ask(cid, side),
            cfg=gate_cfg,
            quote_stale=getattr(worker, "_ws_market_data_stale", False),
        )
        worker.dashboard["signal_quality_score"] = quality.total_score
        worker.dashboard["signal_quality_components"] = quality.components.as_dict()
        worker.dashboard["signal_quality_threshold"] = cfg.min_signal_quality_score
        worker.dashboard["entry_net_edge"] = round(quality.net_edge, 4)
        worker.dashboard["entry_gross_edge"] = round(quality.gross_edge, 4)
        worker.dashboard["book_spread"] = round(quality.spread, 4)
        worker.dashboard["estimated_slippage"] = round(quality.estimated_slippage, 4)
        worker.dashboard["momentum_timeframe_alignment"] = (
            "aligned" if quality.reason != "short_long_timeframe_conflict" else "conflict"
        )

        if quality.decision != SignalDecision.ENTER:
            worker.dashboard["entry_block_reason"] = quality.reason
            worker.dashboard["strategy_state"] = "waiting_for_price_adjustment"
            self._record_entry_rejection(
                worker, stage="signal_quality", reason=quality.reason, candidate=True,
            )
            self._log_signal_candidate(
                worker, cid=cid, momentum=momentum, ref=ref, choice=choice, quality=quality,
                decision="SKIP", rejection_reason=quality.reason,
            )
            return

        limit_px, limit_reason = compute_momentum_buy_fok_limit_price(exe[0], max_res.minimum_price)
        if limit_px is None:
            worker.dashboard["entry_block_reason"] = limit_reason
            self._log_signal_candidate(
                worker, cid=cid, momentum=momentum, ref=ref, choice=choice, quality=quality,
                decision="SKIP", rejection_reason=limit_reason,
            )
            return
        tp_ok, tp_reason = validate_take_profit_for_entry(exe[0], cfg.take_profit_pct)
        if not tp_ok:
            worker.dashboard["entry_block_reason"] = tp_reason
            worker.dashboard["strategy_state"] = "waiting_for_price_adjustment"
            self._log_signal_candidate(
                worker, cid=cid, momentum=momentum, ref=ref, choice=choice, quality=quality,
                decision="SKIP", rejection_reason=tp_reason,
            )
            return
        depth_err = check_executable_depth(exe, float(size), min_orderbook_depth=cfg.min_orderbook_depth)
        if depth_err:
            worker.dashboard["entry_block_reason"] = depth_err
            self._log_signal_candidate(
                worker, cid=cid, momentum=momentum, ref=ref, choice=choice, quality=quality,
                decision="SKIP", rejection_reason=depth_err,
            )
            return

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

                worker.refresh_order_book(side)
                exe_fresh = worker.executable_buy(side, float(size))
                if not exe_fresh:
                    worker.dashboard["entry_block_reason"] = "pre_submit_no_liquidity"
                    self._log_signal_candidate(
                        worker, cid=cid, momentum=momentum, ref=ref, choice=choice, quality=quality,
                        decision="SKIP", rejection_reason="pre_submit_no_liquidity",
                    )
                    return
                quality_fresh = evaluate_signal_quality(
                    momentum=momentum,
                    reference=ref,
                    probability=choice.probability,
                    side=side,
                    yes_ask=float(worker.best_ask("YES")),
                    no_ask=float(worker.best_ask("NO")),
                    yes_bid=float(worker.best_bid("YES")),
                    no_bid=float(worker.best_bid("NO")),
                    executable_vwap=float(exe_fresh[0]),
                    executable_filled=float(exe_fresh[1]),
                    order_size=float(size),
                    seconds_remaining=float(worker.market_seconds_left()),
                    interval_seconds=cfg.interval_seconds,
                    min_time_remaining=float(cfg.min_time_before_close_seconds),
                    token_ask_at_open=self._open_ask(cid, side),
                    cfg=gate_cfg,
                    quote_stale=getattr(worker, "_ws_market_data_stale", False),
                )
                if quality_fresh.decision != SignalDecision.ENTER:
                    worker.dashboard["entry_block_reason"] = f"pre_submit_reject:{quality_fresh.reason}"
                    self._log_signal_candidate(
                        worker, cid=cid, momentum=momentum, ref=ref, choice=choice,
                        quality=quality_fresh, decision="SKIP",
                        rejection_reason=quality_fresh.reason,
                    )
                    return
                limit_px, limit_reason = compute_momentum_buy_fok_limit_price(
                    exe_fresh[0], max_res.minimum_price,
                )
                if limit_px is None:
                    worker.dashboard["entry_block_reason"] = f"pre_submit_{limit_reason}"
                    return

                worker.order_state = OrderState.PENDING
                worker.dashboard["strategy_state"] = "buy_fok_submitted"
                worker.dashboard["entry_order_status"] = "SUBMITTED"
                worker.dashboard["signal_quality_score"] = quality_fresh.total_score
                print(
                    f"📤 [BUY FOK] {label} | {side} | limit_price={limit_px:.4f} | "
                    f"score={quality_fresh.total_score:.1f} net_edge={quality_fresh.net_edge:.4f} | "
                    f"size={size:.2f}"
                )
                self._log_signal_candidate(
                    worker, cid=cid, momentum=momentum, ref=ref, choice=choice,
                    quality=quality_fresh, decision="ENTER", rejection_reason="",
                    order_submitted=True,
                )
                qty, fill_px, oid = await fok_buy_limit(
                    worker, side, limit_px, float(size), purpose="momentum_entry",
                )
                worker.order_state = OrderState.IDLE
                if qty <= MIN_SHARES - 1e-9:
                    worker.dashboard["entry_block_reason"] = "entry_fok_failed"
                    worker.dashboard["entry_order_status"] = "FAILED"
                    worker.dashboard["strategy_state"] = "entry_failed"
                    self._log_signal_candidate(
                        worker, cid=cid, momentum=momentum, ref=ref, choice=choice,
                        quality=quality_fresh, decision="ENTER", rejection_reason="entry_fok_failed",
                        order_submitted=True, fok_result="FAILED",
                    )
                    print(
                        f"❌ [BUY FOK FAILED] {label} | {side} | limit_price={limit_px:.4f}"
                    )
                    return
                self._log_signal_candidate(
                    worker, cid=cid, momentum=momentum, ref=ref, choice=choice,
                    quality=quality_fresh, decision="ENTER", rejection_reason="",
                    order_submitted=True, fok_result="FILLED", fill_price=fill_px,
                )
                await self._record_buy(worker, side, qty, fill_px, limit_px, oid, momentum.direction)
                _LOG.info(
                    json.dumps(
                        {
                            "event": "momentum_entry_filled",
                            "market_id": cid,
                            "side": side,
                            "limit_price": limit_px,
                            "avg_fill_price": fill_px,
                            "qty": qty,
                            "momentum": momentum.direction.name,
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

        wkey = self._key(worker)
        await self._handle_take_profit(worker, cid)
        await self._handle_stop_loss(worker, cid)

        has_position = self._position_side(worker) is not None
        if has_position or worker._stop_loss_exit_in_flight:
            self._publish_dashboard(worker)
            return

        if stopped_out_store.is_stopped_out(wkey, cid):
            worker.dashboard["entry_block_reason"] = "stopped_out_market"
            worker.dashboard["strategy_state"] = "market_blocked_after_stop_loss"
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


from strategies.inventory_pair_accumulation import InventoryPairAccumulationStrategy

DirectionalFokStrategy = MomentumDelayedAdjustmentStrategy
MomentumStrategy = MomentumDelayedAdjustmentStrategy
GabagoolStrategy = MomentumDelayedAdjustmentStrategy
SpreadCaptureStrategy = MomentumDelayedAdjustmentStrategy
LateEntryStrategy = MomentumDelayedAdjustmentStrategy
BinanceTriggeredFokStrategy = MomentumDelayedAdjustmentStrategy
BinanceFokStrategy = MomentumDelayedAdjustmentStrategy
InventoryAccumulationStrategy = InventoryPairAccumulationStrategy
