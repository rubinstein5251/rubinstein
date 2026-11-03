"""Persistent counters for entry pipeline stages and rejection reasons."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


@dataclass
class EntryRejectionSnapshot:
    candidates_evaluated: int = 0
    enter_decisions: int = 0
    orders_submitted: int = 0
    fok_filled: int = 0
    fok_failed: int = 0
    by_reason: Dict[str, int] = field(default_factory=dict)
    by_stage: Dict[str, int] = field(default_factory=dict)
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates_evaluated": self.candidates_evaluated,
            "enter_decisions": self.enter_decisions,
            "orders_submitted": self.orders_submitted,
            "fok_filled": self.fok_filled,
            "fok_failed": self.fok_failed,
            "by_reason": dict(sorted(self.by_reason.items(), key=lambda x: -x[1])),
            "by_stage": dict(sorted(self.by_stage.items(), key=lambda x: -x[1])),
            "updated_at": self.updated_at,
        }


class EntryRejectionCounterStore:
    def __init__(self, path: str = "entry_rejection_counters.json") -> None:
        self._path = path
        self._lock = threading.Lock()
        self._global = EntryRejectionSnapshot()
        self._by_worker: Dict[str, EntryRejectionSnapshot] = {}

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

        def _snap(blob: Mapping[str, Any]) -> EntryRejectionSnapshot:
            return EntryRejectionSnapshot(
                candidates_evaluated=int(blob.get("candidates_evaluated", 0)),
                enter_decisions=int(blob.get("enter_decisions", 0)),
                orders_submitted=int(blob.get("orders_submitted", 0)),
                fok_filled=int(blob.get("fok_filled", 0)),
                fok_failed=int(blob.get("fok_failed", 0)),
                by_reason=dict(blob.get("by_reason") or {}),
                by_stage=dict(blob.get("by_stage") or {}),
                updated_at=float(blob.get("updated_at", 0)),
            )

        if "global" in raw:
            self._global = _snap(raw["global"])
            workers = raw.get("workers") or {}
            if isinstance(workers, dict):
                for k, v in workers.items():
                    if isinstance(v, dict):
                        self._by_worker[str(k)] = _snap(v)

    def _persist(self) -> None:
        payload = {
            "global": self._global.to_dict(),
            "workers": {k: v.to_dict() for k, v in self._by_worker.items()},
        }
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except OSError:
            pass

    def record(
        self,
        worker_key: str,
        *,
        stage: str,
        reason: Optional[str] = None,
        candidate: bool = False,
        enter: bool = False,
        order_submitted: bool = False,
        fok_filled: bool = False,
        fok_failed: bool = False,
    ) -> None:
        with self._lock:
            if not self._by_worker:
                self._load()
            targets = [self._global, self._by_worker.setdefault(worker_key, EntryRejectionSnapshot())]
            for snap in targets:
                if candidate:
                    snap.candidates_evaluated += 1
                if enter:
                    snap.enter_decisions += 1
                if order_submitted:
                    snap.orders_submitted += 1
                if fok_filled:
                    snap.fok_filled += 1
                if fok_failed:
                    snap.fok_failed += 1
                if reason:
                    snap.by_reason[reason] = snap.by_reason.get(reason, 0) + 1
                if stage:
                    snap.by_stage[stage] = snap.by_stage.get(stage, 0) + 1
                snap.updated_at = time.time()
            self._persist()

    def snapshot(self, worker_key: Optional[str] = None) -> EntryRejectionSnapshot:
        with self._lock:
            if not self._by_worker and os.path.isfile(self._path):
                self._load()
            if worker_key:
                return self._by_worker.get(worker_key, EntryRejectionSnapshot())
            return self._global

    def dashboard_payload(self, worker_key: str) -> dict[str, Any]:
        s = self.snapshot(worker_key)
        g = self.snapshot(None)
        return {
            "entry_candidates_evaluated": s.candidates_evaluated,
            "entry_rejection_by_reason": dict(s.by_reason),
            "entry_rejection_by_stage": dict(s.by_stage),
            "entry_orders_submitted": s.orders_submitted,
            "entry_fok_filled": s.fok_filled,
            "entry_fok_failed": s.fok_failed,
            "global_entry_rejection_by_reason": dict(g.by_reason),
        }


entry_rejection_counters = EntryRejectionCounterStore()
