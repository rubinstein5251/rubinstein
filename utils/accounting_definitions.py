"""Shared P&L metric definitions — single source for API, dashboard, and tests."""

from __future__ import annotations

from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from bot import MarketWorker

METRIC_DEFINITIONS = {
    "realized_pnl": (
        "Cumulative realized net P&L from the canonical ledger "
        "(sum of unique confirmed realized_pnl_delta on fills, net of allocated fees)."
    ),
    "unrealized_pnl": (
        "Mark-to-market on open inventory: matched pairs at $1, unmatched legs at best bid, "
        "minus total cost basis (not realized until sold or settled)."
    ),
    "total_net_pnl": "Overall net P&L = cumulative realized net P&L + current unrealized net P&L.",
    "cumulative_realized_pnl": "Alias of realized_pnl (ledger-backed, never reset on position close).",
    "matched_expected_net_margin": (
        "Expected settlement margin on confirmed matched share pairs only; "
        "not counted as realized P&L until settlement/redemption."
    ),
}


def portfolio_realized_and_overall(workers: list["MarketWorker"]) -> dict[str, float]:
    from utils.canonical_ledger import canonical_ledger

    realized, _, _, _ = canonical_ledger.portfolio_totals()
    unrealized = 0.0
    for w in workers:
        try:
            u, _ = w.get_unrealized_pnl()
            unrealized += float(u)
        except Exception:
            continue
    overall = round(realized + unrealized, 4)
    return {
        "cumulative_realized_pnl": round(realized, 4),
        "unrealized_pnl": round(unrealized, 4),
        "overall_net_pnl": overall,
    }


def worker_accounting_row(worker: "MarketWorker") -> dict[str, Any]:
    worker.sync_accounting_dashboard()
    d = worker.dashboard
    inv = worker.inventory
    return {
        "worker_key": worker.worker_config.key,
        "realized_pnl": float(d.get("realized_pnl") or 0),
        "unrealized_pnl": float(d.get("unrealized_pnl") or 0),
        "total_net_pnl": float(d.get("total_net_pnl") or 0),
        "cumulative_realized_pnl": float(d.get("cumulative_realized_pnl") or d.get("realized_pnl") or 0),
        "fees_paid": float(d.get("fees_paid") or 0),
        "yes_shares": round(inv.yes_shares, 4),
        "no_shares": round(inv.no_shares, 4),
        "matched_pairs": round(inv.matched_pairs, 4),
        "definitions": METRIC_DEFINITIONS,
    }


def ledger_matches_dashboard(worker: "MarketWorker") -> bool:
    from utils.canonical_ledger import canonical_ledger

    worker.sync_accounting_dashboard()
    summary = canonical_ledger.worker_summary(worker.worker_config.key)
    dash_realized = float(worker.dashboard.get("realized_pnl") or 0)
    return abs(summary.realized_pnl - dash_realized) < 1e-3
