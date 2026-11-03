"""Structured candidate-signal logging and grouped performance reports."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional


@dataclass
class CandidateSignalRecord:
    ts: float
    asset: str
    window: str
    market_id: str
    side: str
    seconds_remaining: float
    decision: str
    rejection_reason: str
    returns: Dict[str, float] = field(default_factory=dict)
    acceleration: float = 0.0
    realized_volatility: float = 0.0
    momentum_direction: str = ""
    momentum_strength: float = 0.0
    reference_price: float = 0.0
    current_underlying: float = 0.0
    yes_ask: float = 0.0
    no_ask: float = 0.0
    spread: float = 0.0
    estimated_probability: float = 0.0
    gross_edge: float = 0.0
    net_edge: float = 0.0
    signal_score: float = 0.0
    score_components: Dict[str, float] = field(default_factory=dict)
    order_submitted: bool = False
    fok_result: str = ""
    fill_price: float = 0.0
    exit_reason: str = ""
    realized_net_pnl: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SignalCandidateLog:
    def __init__(
        self,
        path: str = "signal_candidates.jsonl",
        *,
        max_memory: int = 5000,
    ) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._memory: List[CandidateSignalRecord] = []
        self._max_memory = max_memory

    def append(self, record: CandidateSignalRecord) -> None:
        line = json.dumps(record.to_dict(), separators=(",", ":"))
        with self._lock:
            self._memory.append(record)
            if len(self._memory) > self._max_memory:
                self._memory = self._memory[-self._max_memory :]
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def records(self) -> List[CandidateSignalRecord]:
        with self._lock:
            return list(self._memory)

    @staticmethod
    def _edge_bucket(net_edge: float) -> str:
        if net_edge < 0:
            return "<0"
        if net_edge < 0.05:
            return "0-5c"
        if net_edge < 0.10:
            return "5-10c"
        return "10c+"

    @staticmethod
    def _score_bucket(score: float) -> str:
        if score < 50:
            return "<50"
        if score < 75:
            return "50-74"
        if score < 90:
            return "75-89"
        return "90+"

    @staticmethod
    def _time_bucket(seconds_remaining: float) -> str:
        if seconds_remaining < 60:
            return "<60s"
        if seconds_remaining < 180:
            return "60-180s"
        return "180s+"

    def build_report(self, records: Optional[Iterable[CandidateSignalRecord]] = None) -> dict[str, Any]:
        rows = list(records) if records is not None else self.records()
        groups: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "entered": 0, "rejected": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
        )

        def bump(key: str, rec: CandidateSignalRecord) -> None:
            g = groups[key]
            g["count"] += 1
            if rec.decision == "ENTER":
                g["entered"] += 1
            else:
                g["rejected"] += 1
            if rec.realized_net_pnl > 0:
                g["wins"] += 1
            elif rec.realized_net_pnl < 0:
                g["losses"] += 1
            g["net_pnl"] += rec.realized_net_pnl

        for rec in rows:
            asset_key = f"asset:{rec.asset}"
            bump(asset_key, rec)
            bump(f"direction:{rec.side or 'none'}", rec)
            bump(f"time:{self._time_bucket(rec.seconds_remaining)}", rec)
            bump(f"vol:{round(rec.realized_volatility, 4)}", rec)
            bump(f"score:{self._score_bucket(rec.signal_score)}", rec)
            bump(f"edge:{self._edge_bucket(rec.net_edge)}", rec)
            align = "aligned" if rec.momentum_direction in ("UP", "DOWN") else "neutral"
            bump(f"tf:{align}", rec)

        return {
            "generated_at": time.time(),
            "record_count": len(rows),
            "groups": dict(groups),
        }

    def persist_report(self, report_path: str = "signal_performance_report.json") -> None:
        report = self.build_report()
        try:
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
        except OSError:
            pass


signal_candidate_log = SignalCandidateLog(
    path=os.getenv("SIGNAL_CANDIDATE_LOG_PATH", "signal_candidates.jsonl"),
)
