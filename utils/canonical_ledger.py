"""Canonical fill ledger and P&L snapshots — single source for dashboards and JSON."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple


@dataclass(frozen=True)
class FillRecord:
    fill_id: str
    worker_key: str
    market_id: str
    order_id: str
    side: str
    action: str
    price: float
    quantity: float
    fee: float
    timestamp: float
    realized_pnl_delta: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkerAccounting:
    worker_key: str
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    wins: int = 0
    losses: int = 0
    fill_ids: List[str] = field(default_factory=list)
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_key": self.worker_key,
            "realized_pnl": round(self.realized_pnl, 4),
            "fees_paid": round(self.fees_paid, 4),
            "wins": self.wins,
            "losses": self.losses,
            "fill_count": len(self.fill_ids),
            "updated_at": self.updated_at,
        }


def _atomic_write_json(path: str, payload: Mapping[str, Any]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class CanonicalLedger:
    """Idempotent fill log + per-worker realized P&L totals."""

    def __init__(
        self,
        path: str = "canonical_ledger.json",
        snapshot_path: str = "bot_pnl.json",
    ) -> None:
        self._path = path
        self._snapshot_path = snapshot_path
        self._lock = threading.Lock()
        self._fills: List[FillRecord] = []
        self._fill_id_set: set[str] = set()
        self._workers: Dict[str, WorkerAccounting] = {}
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
        for blob in raw.get("fills") or []:
            if not isinstance(blob, dict):
                continue
            rec = FillRecord(
                fill_id=str(blob.get("fill_id", "")),
                worker_key=str(blob.get("worker_key", "")),
                market_id=str(blob.get("market_id", "")),
                order_id=str(blob.get("order_id", "")),
                side=str(blob.get("side", "")),
                action=str(blob.get("action", "")).upper(),
                price=float(blob.get("price", 0)),
                quantity=float(blob.get("quantity", 0)),
                fee=float(blob.get("fee", 0)),
                timestamp=float(blob.get("timestamp", 0)),
                realized_pnl_delta=float(blob.get("realized_pnl_delta", 0)),
            )
            if rec.fill_id and rec.fill_id not in self._fill_id_set:
                self._fills.append(rec)
                self._fill_id_set.add(rec.fill_id)
        for key, wb in (raw.get("workers") or {}).items():
            if isinstance(wb, dict):
                self._workers[str(key)] = WorkerAccounting(
                    worker_key=str(key),
                    realized_pnl=float(wb.get("realized_pnl", 0)),
                    fees_paid=float(wb.get("fees_paid", 0)),
                    wins=int(wb.get("wins", 0)),
                    losses=int(wb.get("losses", 0)),
                    fill_ids=list(wb.get("fill_ids") or []),
                    updated_at=float(wb.get("updated_at", 0)),
                )

    def _persist(self) -> None:
        payload = {
            "fills": [f.to_dict() for f in self._fills[-5000:]],
            "workers": {k: v.to_dict() for k, v in self._workers.items()},
            "updated_at": time.time(),
        }
        _atomic_write_json(self._path, payload)

    def make_fill_id(
        self,
        *,
        worker_key: str,
        order_id: Optional[str],
        side: str,
        action: str,
        quantity: float,
        price: float,
    ) -> str:
        oid = order_id or "none"
        return f"{worker_key}:{oid}:{action}:{side}:{quantity:.6f}:{price:.6f}"

    def record_fill(
        self,
        *,
        worker_key: str,
        market_id: str,
        order_id: Optional[str],
        side: str,
        action: str,
        price: float,
        quantity: float,
        fee: float = 0.0,
        realized_pnl_delta: float = 0.0,
        count_win_loss: bool = False,
    ) -> bool:
        """Return False if duplicate fill_id (already applied)."""
        if quantity <= 0 or price <= 0:
            return False
        fill_id = self.make_fill_id(
            worker_key=worker_key,
            order_id=order_id,
            side=side.upper(),
            action=action.upper(),
            quantity=quantity,
            price=price,
        )
        with self._lock:
            if fill_id in self._fill_id_set:
                return False
            rec = FillRecord(
                fill_id=fill_id,
                worker_key=worker_key,
                market_id=market_id,
                order_id=str(order_id or ""),
                side=side.upper(),
                action=action.upper(),
                price=float(price),
                quantity=float(quantity),
                fee=float(fee),
                timestamp=time.time(),
                realized_pnl_delta=float(realized_pnl_delta),
            )
            self._fills.append(rec)
            self._fill_id_set.add(fill_id)
            w = self._workers.setdefault(worker_key, WorkerAccounting(worker_key=worker_key))
            w.realized_pnl = round(w.realized_pnl + realized_pnl_delta, 4)
            w.fees_paid = round(w.fees_paid + fee, 4)
            w.fill_ids.append(fill_id)
            if count_win_loss and abs(realized_pnl_delta) > 1e-9:
                if realized_pnl_delta > 0:
                    w.wins += 1
                else:
                    w.losses += 1
            w.updated_at = time.time()
            self._persist()
            return True

    def worker_summary(self, worker_key: str) -> WorkerAccounting:
        with self._lock:
            return self._workers.get(worker_key, WorkerAccounting(worker_key=worker_key))

    def portfolio_totals(self) -> Tuple[float, int, int, float]:
        with self._lock:
            realized = sum(w.realized_pnl for w in self._workers.values())
            fees = sum(w.fees_paid for w in self._workers.values())
            wins = sum(w.wins for w in self._workers.values())
            losses = sum(w.losses for w in self._workers.values())
            return round(realized, 4), wins, losses, round(fees, 4)

    def build_accounting_view(
        self,
        *,
        worker_key: str,
        unrealized_pnl: float,
        inventory_snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        w = self.worker_summary(worker_key)
        realized = round(w.realized_pnl, 4)
        unrealized = round(float(unrealized_pnl), 4)
        fees = round(w.fees_paid, 4)
        net_total = round(realized + unrealized, 4)
        matched = float(inventory_snapshot.get("matched_pairs") or 0)
        combined = inventory_snapshot.get("matched_combined_cost")
        return {
            "realized_pnl": realized,
            "cumulative_realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_net_pnl": net_total,
            "overall_net_pnl": net_total,
            "fees_paid": fees,
            "wins": w.wins,
            "losses": w.losses,
            "matched_pairs": matched,
            "matched_combined_cost": combined,
            "yes_shares": inventory_snapshot.get("yes_shares", 0),
            "no_shares": inventory_snapshot.get("no_shares", 0),
            "unpaired_yes": inventory_snapshot.get("unpaired_yes", 0),
            "unpaired_no": inventory_snapshot.get("unpaired_no", 0),
            "unmatched_yes_shares": inventory_snapshot.get("unpaired_yes", 0),
            "unmatched_no_shares": inventory_snapshot.get("unpaired_no", 0),
        }

    def sync_bot_pnl_snapshot(self, asset_stats: Optional[Dict[str, Dict]] = None) -> None:
        """Write bot_pnl.json from ledger totals (does not overwrite per-asset trade arrays)."""
        realized, wins, losses, fees = self.portfolio_totals()
        existing: dict = {}
        if os.path.isfile(self._snapshot_path):
            try:
                with open(self._snapshot_path, "r", encoding="utf-8") as f:
                    existing = json.load(f) or {}
            except (OSError, json.JSONDecodeError):
                existing = {}
        payload = {
            "total_pnl": realized,
            "realized_pnl": realized,
            "unrealized_pnl_note": "per-worker via dashboard API",
            "fees_paid": fees,
            "wins": wins,
            "losses": losses,
            "win_rate": (
                f"{round(100.0 * wins / (wins + losses), 2)}%" if wins + losses else "0%"
            ),
            "total_trades": wins + losses,
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "assets": asset_stats or existing.get("assets") or {},
            "source": "canonical_ledger",
        }
        _atomic_write_json(self._snapshot_path, payload)

    def backfill_worker_realized_if_missing(
        self,
        worker_key: str,
        inventory_realized: float,
    ) -> float:
        """Backfill ledger only when sells were recorded on inventory but never notified.

        Never overwrite ledger net P&L with inventory gross (fees would be wrong).
        """
        with self._lock:
            w = self._workers.setdefault(worker_key, WorkerAccounting(worker_key=worker_key))
            inv_val = round(float(inventory_realized), 4)
            if abs(w.realized_pnl) > 1e-4 or abs(inv_val) <= 1e-4:
                return 0.0
            w.realized_pnl = inv_val
            w.updated_at = time.time()
            self._persist()
            return inv_val

    def hydrate_worker_from_saved_stats(
        self,
        worker_key: str,
        *,
        total_pnl: float,
        wins: int,
        losses: int,
    ) -> bool:
        """One-time import from legacy per-asset JSON when ledger has no realized rows."""
        saved = round(float(total_pnl), 4)
        if abs(saved) <= 1e-9:
            return False
        fill_id = f"legacy:{worker_key}"
        with self._lock:
            if fill_id in self._fill_id_set:
                return False
            w = self._workers.setdefault(worker_key, WorkerAccounting(worker_key=worker_key))
            if abs(w.realized_pnl) > 1e-4:
                return False
            rec = FillRecord(
                fill_id=fill_id,
                worker_key=worker_key,
                market_id="legacy",
                order_id="legacy",
                side="LEGACY",
                action="IMPORT",
                price=1.0,
                quantity=1.0,
                fee=0.0,
                timestamp=time.time(),
                realized_pnl_delta=saved,
            )
            self._fills.append(rec)
            self._fill_id_set.add(fill_id)
            w.realized_pnl = saved
            w.wins = int(wins)
            w.losses = int(losses)
            w.fill_ids.append(fill_id)
            w.updated_at = time.time()
            self._persist()
            return True

    def reconcile_inventory_realized(self, worker_key: str, inventory_realized: float) -> float:
        """Deprecated alias — use backfill_worker_realized_if_missing."""
        return self.backfill_worker_realized_if_missing(worker_key, inventory_realized)


canonical_ledger = CanonicalLedger()
