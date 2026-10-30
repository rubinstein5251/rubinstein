"""Persist markets blocked after stop-loss (survives restarts)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Optional


def _db_path() -> str:
    return os.getenv("STOPPED_OUT_DB", os.getenv("INVENTORY_STRATEGY_DB", "market_inventory_store.db"))


class StoppedOutStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30.0)
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stopped_out_markets (
                    worker_key TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    stopped_at REAL NOT NULL,
                    PRIMARY KEY (worker_key, condition_id)
                )
                """
            )
            self._conn.commit()

    def is_stopped_out(self, worker_key: str, condition_id: str) -> bool:
        if not condition_id:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM stopped_out_markets WHERE worker_key = ? AND condition_id = ? LIMIT 1",
                (worker_key, condition_id),
            ).fetchone()
        return row is not None

    def mark_stopped_out(self, worker_key: str, condition_id: str) -> None:
        if not condition_id:
            return
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO stopped_out_markets (worker_key, condition_id, stopped_at)
                VALUES (?, ?, ?)
                ON CONFLICT(worker_key, condition_id) DO UPDATE SET stopped_at = excluded.stopped_at
                """,
                (worker_key, condition_id, now),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


stopped_out_store = StoppedOutStore()
