"""Track take-profit, stop-loss, and settlement exit statistics."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict


@dataclass
class ExitPerformanceSnapshot:
    take_profit_exits: int = 0
    stop_loss_exits: int = 0
    settled_positions: int = 0
    take_profit_pnl: float = 0.0
    stop_loss_pnl: float = 0.0
    settlement_pnl: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    take_profit_amounts: list[float] = field(default_factory=list)
    stop_loss_amounts: list[float] = field(default_factory=list)
    holding_times_sec: list[float] = field(default_factory=list)
    updated_at: float = 0.0

    @property
    def take_profit_win_rate(self) -> float:
        total = self.take_profit_exits + self.stop_loss_exits
        if total <= 0:
            return 0.0
        return self.take_profit_exits / total

    @property
    def avg_take_profit_amount(self) -> float:
        if not self.take_profit_amounts:
            return 0.0
        return sum(self.take_profit_amounts) / len(self.take_profit_amounts)

    @property
    def avg_stop_loss_amount(self) -> float:
        if not self.stop_loss_amounts:
            return 0.0
        return sum(self.stop_loss_amounts) / len(self.stop_loss_amounts)

    @property
    def avg_holding_time_sec(self) -> float:
        if not self.holding_times_sec:
            return 0.0
        return sum(self.holding_times_sec) / len(self.holding_times_sec)

    @property
    def profit_factor(self) -> float:
        wins = sum(x for x in self.take_profit_amounts if x > 0)
        losses = abs(sum(x for x in self.stop_loss_amounts if x < 0))
        if losses <= 1e-9:
            return wins if wins > 0 else 0.0
        return wins / losses

    @property
    def max_drawdown(self) -> float:
        return 0.0


class ExitPerformanceStore:
    def __init__(self, path: str = "exit_performance.json") -> None:
        self._path = path
        self._lock = threading.Lock()
        self._by_worker: Dict[str, ExitPerformanceSnapshot] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for key, blob in raw.items():
            if not isinstance(blob, dict):
                continue
            snap = ExitPerformanceSnapshot(
                take_profit_exits=int(blob.get("take_profit_exits", 0)),
                stop_loss_exits=int(blob.get("stop_loss_exits", 0)),
                settled_positions=int(blob.get("settled_positions", 0)),
                take_profit_pnl=float(blob.get("take_profit_pnl", 0)),
                stop_loss_pnl=float(blob.get("stop_loss_pnl", 0)),
                settlement_pnl=float(blob.get("settlement_pnl", 0)),
                gross_pnl=float(blob.get("gross_pnl", 0)),
                fees=float(blob.get("fees", 0)),
                net_pnl=float(blob.get("net_pnl", 0)),
                take_profit_amounts=list(blob.get("take_profit_amounts") or []),
                stop_loss_amounts=list(blob.get("stop_loss_amounts") or []),
                holding_times_sec=list(blob.get("holding_times_sec") or []),
                updated_at=float(blob.get("updated_at", 0)),
            )
            self._by_worker[key] = snap

    def _persist(self) -> None:
        payload = {
            k: asdict(v) for k, v in self._by_worker.items()
        }
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except OSError:
            pass

    def snapshot(self, worker_key: str) -> ExitPerformanceSnapshot:
        with self._lock:
            return self._by_worker.setdefault(worker_key, ExitPerformanceSnapshot())

    def dashboard_payload(self, worker_key: str) -> dict[str, Any]:
        s = self.snapshot(worker_key)
        return {
            "take_profit_exits": s.take_profit_exits,
            "stop_loss_exits": s.stop_loss_exits,
            "settled_positions": s.settled_positions,
            "take_profit_win_rate": round(s.take_profit_win_rate * 100, 2),
            "avg_take_profit_amount": round(s.avg_take_profit_amount, 4),
            "avg_stop_loss_amount": round(s.avg_stop_loss_amount, 4),
            "exit_gross_pnl": round(s.gross_pnl, 4),
            "exit_fees": round(s.fees, 4),
            "exit_net_pnl": round(s.net_pnl, 4),
            "exit_profit_factor": round(s.profit_factor, 4),
            "avg_holding_time_sec": round(s.avg_holding_time_sec, 2),
        }

    def record_take_profit(
        self,
        worker_key: str,
        *,
        pnl: float,
        fees: float,
        holding_sec: float,
    ) -> None:
        with self._lock:
            s = self._by_worker.setdefault(worker_key, ExitPerformanceSnapshot())
            s.take_profit_exits += 1
            s.take_profit_pnl += pnl
            s.take_profit_amounts.append(pnl)
            s.holding_times_sec.append(holding_sec)
            s.fees += fees
            s.gross_pnl += pnl + fees
            s.net_pnl += pnl
            s.updated_at = time.time()
            self._persist()

    def record_stop_loss(
        self,
        worker_key: str,
        *,
        pnl: float,
        fees: float,
        holding_sec: float,
    ) -> None:
        with self._lock:
            s = self._by_worker.setdefault(worker_key, ExitPerformanceSnapshot())
            s.stop_loss_exits += 1
            s.stop_loss_pnl += pnl
            s.stop_loss_amounts.append(pnl)
            s.holding_times_sec.append(holding_sec)
            s.fees += fees
            s.gross_pnl += pnl + fees
            s.net_pnl += pnl
            s.updated_at = time.time()
            self._persist()

    def record_settlement(self, worker_key: str, *, pnl: float, fees: float = 0.0) -> None:
        with self._lock:
            s = self._by_worker.setdefault(worker_key, ExitPerformanceSnapshot())
            s.settled_positions += 1
            s.settlement_pnl += pnl
            s.fees += fees
            s.gross_pnl += pnl + fees
            s.net_pnl += pnl
            s.updated_at = time.time()
            self._persist()


exit_performance_store = ExitPerformanceStore()
