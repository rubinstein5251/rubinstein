"""Small, dependency-free inventory ledger used by the trading engines.

The ledger tracks weighted-average entry cost separately for YES and NO.
SELLs realize PnL against that side's weighted-average cost.  Matched pairs
are informational: one YES + one NO share can redeem for $1 at resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict


_EPS = 1e-12
_VALID_SIDES = ("YES", "NO")


@dataclass
class _Lot:
    shares: float = 0.0
    cost: float = 0.0  # total dollar cost of currently held shares

    @property
    def avg_cost(self) -> float:
        return self.cost / self.shares if self.shares > _EPS else 0.0


class PositionInventory:
    """Weighted-average inventory for a binary UP/DOWN market."""

    def __init__(self) -> None:
        self._lots: Dict[str, _Lot] = {side: _Lot() for side in _VALID_SIDES}
        self.realized_pnl: float = 0.0
        self.total_bought: float = 0.0
        self.total_sold: float = 0.0

    @staticmethod
    def _side(side: str) -> str:
        s = str(side).upper().strip()
        if s not in _VALID_SIDES:
            raise ValueError(f"invalid inventory side: {side!r}")
        return s

    def shares(self, side: str) -> float:
        return max(0.0, self._lots[self._side(side)].shares)

    def avg_cost(self, side: str) -> float:
        return self._lots[self._side(side)].avg_cost

    def cost_basis(self, side: str) -> float:
        return max(0.0, self._lots[self._side(side)].cost)

    def headroom(self, side: str, max_shares: float) -> float:
        return max(0.0, float(max_shares) - self.shares(side))

    @property
    def yes_cost(self) -> float:
        """Current total dollar cost basis of held YES shares."""
        return self.cost_basis("YES")

    @property
    def no_cost(self) -> float:
        """Current total dollar cost basis of held NO shares."""
        return self.cost_basis("NO")

    @property
    def imbalance(self) -> float:
        """Absolute YES/NO share imbalance."""
        return abs(self.yes_shares - self.no_shares)

    @property
    def yes_shares(self) -> float:
        return self.shares("YES")

    @property
    def no_shares(self) -> float:
        return self.shares("NO")

    @property
    def matched_pairs(self) -> float:
        return min(self.yes_shares, self.no_shares)

    @property
    def unpaired_yes(self) -> float:
        return max(0.0, self.yes_shares - self.no_shares)

    @property
    def unpaired_no(self) -> float:
        return max(0.0, self.no_shares - self.yes_shares)

    @property
    def unpaired_shares(self) -> float:
        return abs(self.yes_shares - self.no_shares)

    def record_buy(self, side: str, shares: float, price: float) -> None:
        s = self._side(side)
        qty = float(shares)
        px = float(price)
        if not math.isfinite(qty) or not math.isfinite(px):
            raise ValueError("shares and price must be finite numbers")
        if qty <= 0:
            return
        if px < 0 or px > 1:
            raise ValueError(f"buy price must be between 0 and 1: {price}")
        lot = self._lots[s]
        lot.cost += qty * px
        lot.shares += qty
        self.total_bought += qty

    def record_sell(self, side: str, shares: float, price: float) -> float:
        """Remove shares and return realized PnL for the sale."""
        s = self._side(side)
        qty = float(shares)
        px = float(price)
        if not math.isfinite(qty) or not math.isfinite(px):
            raise ValueError("shares and price must be finite numbers")
        if qty <= 0:
            return 0.0
        if px < 0 or px > 1:
            raise ValueError(f"sell price must be between 0 and 1: {price}")

        lot = self._lots[s]
        qty = min(qty, lot.shares)
        if qty <= _EPS:
            return 0.0

        avg = lot.avg_cost
        cost_removed = avg * qty
        proceeds = px * qty
        pnl = proceeds - cost_removed

        lot.shares -= qty
        lot.cost -= cost_removed
        if lot.shares <= _EPS:
            lot.shares = 0.0
            lot.cost = 0.0

        self.realized_pnl += pnl
        self.total_sold += qty
        return pnl

    def mark_to_market(self, side: str, price: float) -> float:
        px = float(price)
        if not math.isfinite(px) or px < 0 or px > 1:
            raise ValueError("mark price must be finite and between 0 and 1")
        return (px - self.avg_cost(side)) * self.shares(side)

    def reset(self) -> None:
        self._lots = {side: _Lot() for side in _VALID_SIDES}
        self.realized_pnl = 0.0
        self.total_bought = 0.0
        self.total_sold = 0.0

    def snapshot(self) -> dict:
        return {
            "YES": {"shares": self.yes_shares, "avg_cost": self.avg_cost("YES")},
            "NO": {"shares": self.no_shares, "avg_cost": self.avg_cost("NO")},
            "matched_pairs": self.matched_pairs,
            "unpaired_shares": self.unpaired_shares,
            "realized_pnl": self.realized_pnl,
        }
