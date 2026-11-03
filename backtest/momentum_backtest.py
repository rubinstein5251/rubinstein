"""
Chronological backtest scaffold for momentum + delayed-adjustment strategy.

Requires aligned timestamped external prices and Polymarket order-book snapshots.
No synthetic profitability claims — run on collected data only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional


@dataclass
class BacktestMetrics:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    notes: List[str] = field(default_factory=list)


def run_backtest(rows: List[Mapping[str, Any]]) -> BacktestMetrics:
    """
    Placeholder runner: iterates rows in order without look-ahead.

    Each row should include external price, book asks, reference open, resolution outcome.
    """
    metrics = BacktestMetrics()
    if not rows:
        metrics.notes.append("no_data")
        return metrics
    metrics.notes.append("scaffold_only_collect_historical_snapshots")
    return metrics
