"""Persisted inventory-accumulation strategy state per market."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class AccumulationPhase(str, Enum):
    WAITING_FOR_FIRST_ENTRY = "WAITING_FOR_FIRST_ENTRY"
    FIRST_ENTRY_PENDING = "FIRST_ENTRY_PENDING"
    WAITING_FOR_OPPOSITE_ENTRY = "WAITING_FOR_OPPOSITE_ENTRY"
    OPPOSITE_ENTRY_PENDING = "OPPOSITE_ENTRY_PENDING"
    PAIR_COMPLETED = "PAIR_COMPLETED"
    ENTRY_WINDOW_CLOSED = "ENTRY_WINDOW_CLOSED"
    RISK_BLOCKED = "RISK_BLOCKED"
    # Legacy (migrated on read)
    IDLE = "WAITING_FOR_FIRST_ENTRY"
    FIRST_LEG_OPEN = "WAITING_FOR_OPPOSITE_ENTRY"
    HEDGE_DUE = "WAITING_FOR_OPPOSITE_ENTRY"
    ACCUMULATING = "PAIR_COMPLETED"
    TARGET_ACHIEVED = "PAIR_COMPLETED"
    CLOSED = "ENTRY_WINDOW_CLOSED"


@dataclass
class MarketCycleRecord:
    cycle_id: str
    worker_key: str
    condition_id: str
    phase: AccumulationPhase
    first_leg_side: str
    first_fill_at: float
    hedge_deadline_at: float
    initial_hedge_complete: bool
    target_achieved: bool
    hedge_retries: int
    metadata: Dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0


def _db_path() -> str:
    return os.getenv("INVENTORY_STRATEGY_DB", "market_inventory_store.db")


class InventoryStrategyStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    worker_key TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    first_leg_side TEXT NOT NULL DEFAULT '',
                    first_fill_at REAL NOT NULL DEFAULT 0,
                    hedge_deadline_at REAL NOT NULL DEFAULT 0,
                    initial_hedge_complete INTEGER NOT NULL DEFAULT 0,
                    target_achieved INTEGER NOT NULL DEFAULT 0,
                    hedge_retries INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cycles_worker ON market_cycles(worker_key, phase)"
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _normalize_phase(raw: str) -> AccumulationPhase:
        token = str(raw or "").strip()
        legacy_map = {
            "IDLE": AccumulationPhase.WAITING_FOR_FIRST_ENTRY,
            "FIRST_LEG_OPEN": AccumulationPhase.WAITING_FOR_OPPOSITE_ENTRY,
            "HEDGE_DUE": AccumulationPhase.WAITING_FOR_OPPOSITE_ENTRY,
            "ACCUMULATING": AccumulationPhase.PAIR_COMPLETED,
            "TARGET_ACHIEVED": AccumulationPhase.PAIR_COMPLETED,
            "CLOSED": AccumulationPhase.ENTRY_WINDOW_CLOSED,
        }
        if token in legacy_map:
            return legacy_map[token]
        try:
            return AccumulationPhase(token)
        except ValueError:
            return AccumulationPhase.WAITING_FOR_FIRST_ENTRY

    def _row(self, row: sqlite3.Row) -> MarketCycleRecord:
        return MarketCycleRecord(
            cycle_id=str(row["cycle_id"]),
            worker_key=str(row["worker_key"]),
            condition_id=str(row["condition_id"]),
            phase=self._normalize_phase(str(row["phase"])),
            first_leg_side=str(row["first_leg_side"] or ""),
            first_fill_at=float(row["first_fill_at"] or 0),
            hedge_deadline_at=float(row["hedge_deadline_at"] or 0),
            initial_hedge_complete=bool(int(row["initial_hedge_complete"] or 0)),
            target_achieved=bool(int(row["target_achieved"] or 0)),
            hedge_retries=int(row["hedge_retries"] or 0),
            metadata=json.loads(row["metadata"] or "{}"),
            updated_at=float(row["updated_at"] or 0),
        )

    def active_cycle(self, worker_key: str) -> Optional[MarketCycleRecord]:
        phases = (
            AccumulationPhase.WAITING_FOR_FIRST_ENTRY.value,
            AccumulationPhase.FIRST_ENTRY_PENDING.value,
            AccumulationPhase.WAITING_FOR_OPPOSITE_ENTRY.value,
            AccumulationPhase.OPPOSITE_ENTRY_PENDING.value,
            "FIRST_LEG_OPEN",
            "HEDGE_DUE",
            "ACCUMULATING",
            "TARGET_ACHIEVED",
            "IDLE",
        )
        ph = ",".join("?" for _ in phases)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT * FROM market_cycles
                WHERE worker_key = ? AND phase IN ({ph})
                ORDER BY updated_at DESC LIMIT 1
                """,
                (worker_key, *phases),
            ).fetchone()
        return self._row(row) if row else None

    def cycle_for_market(self, worker_key: str, condition_id: str) -> Optional[MarketCycleRecord]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM market_cycles
                WHERE worker_key = ? AND condition_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (worker_key, condition_id),
            ).fetchone()
        return self._row(row) if row else None

    def upsert(self, rec: MarketCycleRecord) -> None:
        now = time.time()
        rec.updated_at = now
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO market_cycles (
                    cycle_id, worker_key, condition_id, phase,
                    first_leg_side, first_fill_at, hedge_deadline_at,
                    initial_hedge_complete, target_achieved, hedge_retries,
                    metadata, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id) DO UPDATE SET
                    phase=excluded.phase,
                    first_leg_side=excluded.first_leg_side,
                    first_fill_at=excluded.first_fill_at,
                    hedge_deadline_at=excluded.hedge_deadline_at,
                    initial_hedge_complete=excluded.initial_hedge_complete,
                    target_achieved=excluded.target_achieved,
                    hedge_retries=excluded.hedge_retries,
                    metadata=excluded.metadata,
                    updated_at=excluded.updated_at
                """,
                (
                    rec.cycle_id,
                    rec.worker_key,
                    rec.condition_id,
                    rec.phase.value,
                    rec.first_leg_side,
                    rec.first_fill_at,
                    rec.hedge_deadline_at,
                    1 if rec.initial_hedge_complete else 0,
                    1 if rec.target_achieved else 0,
                    rec.hedge_retries,
                    json.dumps(rec.metadata),
                    now,
                ),
            )
            self._conn.commit()

    def get_or_create_cycle(self, worker_key: str, condition_id: str) -> MarketCycleRecord:
        existing = self.cycle_for_market(worker_key, condition_id)
        if existing:
            return existing
        now = time.time()
        rec = MarketCycleRecord(
            cycle_id=str(uuid.uuid4()),
            worker_key=worker_key,
            condition_id=condition_id,
            phase=AccumulationPhase.WAITING_FOR_FIRST_ENTRY,
            first_leg_side="",
            first_fill_at=0.0,
            hedge_deadline_at=0.0,
            initial_hedge_complete=False,
            target_achieved=False,
            hedge_retries=0,
            metadata={"entries": {}},
            updated_at=now,
        )
        self.upsert(rec)
        return rec

    def start_cycle(
        self,
        worker_key: str,
        condition_id: str,
        *,
        first_leg_side: str,
        hedge_delay_seconds: float = 0.0,
    ) -> MarketCycleRecord:
        """Begin opposite-side wait after confirmed first-leg fill."""
        now = time.time()
        rec = self.cycle_for_market(worker_key, condition_id)
        if rec is None:
            rec = self.get_or_create_cycle(worker_key, condition_id)
        rec.first_leg_side = first_leg_side
        rec.first_fill_at = now
        rec.hedge_deadline_at = now
        rec.phase = AccumulationPhase.WAITING_FOR_OPPOSITE_ENTRY
        rec.initial_hedge_complete = False
        rec.metadata.setdefault("entries", {})
        self.upsert(rec)
        return rec


inventory_strategy_store = InventoryStrategyStore()
