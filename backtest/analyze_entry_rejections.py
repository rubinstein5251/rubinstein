"""
Compare legacy all-window momentum vs tiered trigger/confirm on logged candidates.

Usage: python backtest/analyze_entry_rejections.py [path/to/signal_candidates.jsonl]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.momentum_engine import MomentumDirection, _evaluate_tiered_direction  # noqa: E402


def _legacy_vote_conflict(returns: Mapping[str, float]) -> str:
    """Replicate pre-tiered vote logic on stored return snapshots."""
    parsed: dict[int, float] = {}
    for k, v in returns.items():
        try:
            parsed[int(str(k).replace("s", ""))] = float(v)
        except ValueError:
            continue
    if not parsed:
        return "insufficient_history"
    up_votes = sum(1 for r in parsed.values() if r > 0)
    down_votes = sum(1 for r in parsed.values() if r < 0)
    short_w = min(parsed.keys())
    long_w = max(parsed.keys())
    short_r = parsed[short_w]
    long_r = parsed[long_w]
    aligned_up = up_votes >= max(2, len(parsed) - 1) and short_r > 0 and long_r > 0
    aligned_down = down_votes >= max(2, len(parsed) - 1) and short_r < 0 and long_r < 0
    if aligned_up and aligned_down:
        return "conflicting_timeframes"
    if aligned_up or aligned_down:
        return "direction_ok"
    return "conflicting_timeframes"


def _tiered_on_returns(returns: Mapping[str, float]) -> str:
    parsed: dict[int, float] = {}
    for k, v in returns.items():
        try:
            parsed[int(str(k).replace("s", ""))] = float(v)
        except ValueError:
            continue
    direction, reason = _evaluate_tiered_direction(
        parsed,
        trigger_w=5,
        confirm_w=15,
        support_w=30,
        context_w=60,
        trigger_min_abs_return=0.00005,
        confirm_tolerance=0.0001,
        support_opposing_tolerance=0.002,
        context_reversal_min_abs_return=0.004,
    )
    if direction in (MomentumDirection.UP, MomentumDirection.DOWN):
        return "direction_ok"
    return reason


def analyze(path: Path) -> dict[str, Any]:
    legacy = Counter()
    tiered = Counter()
    logged = Counter()
    rows = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows += 1
            row = json.loads(line)
            logged[row.get("rejection_reason") or row.get("decision", "")] += 1
            rets = row.get("returns") or {}
            legacy[_legacy_vote_conflict(rets)] += 1
            tiered[_tiered_on_returns(rets)] += 1
    return {
        "rows": rows,
        "logged_rejection_counts": dict(logged.most_common()),
        "simulated_legacy_momentum": dict(legacy.most_common()),
        "simulated_tiered_momentum": dict(tiered.most_common()),
        "legacy_would_pass": legacy.get("direction_ok", 0),
        "tiered_would_pass": tiered.get("direction_ok", 0),
    }


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "signal_candidates.jsonl")
    report = analyze(path)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
