"""Entry side selection for the directional FOK strategy (minimum ask threshold)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from utils.clob_helpers import clamp_buy_price

_MAX_BUY = 0.99


class EntryDecision(Enum):
    BUY = auto()
    SKIP = auto()


@dataclass(frozen=True)
class EntrySideChoice:
    decision: EntryDecision
    side: Optional[str]
    best_ask: float
    reason: str


def best_ask_meets_minimum(best_ask: float, min_entry_price: float) -> bool:
    ask = float(best_ask)
    if ask <= 0 or ask > _MAX_BUY + 1e-9:
        return False
    min_e = clamp_buy_price(float(min_entry_price))
    ask = clamp_buy_price(ask)
    return ask >= min_e - 1e-9 and ask <= _MAX_BUY + 1e-9


def pick_entry_side(
    yes_ask: float,
    no_ask: float,
    *,
    min_entry_price: float,
) -> EntrySideChoice:
    """Pick at most one side when best ask is at or above the minimum entry price."""
    min_e = clamp_buy_price(float(min_entry_price))
    candidates: list[tuple[str, float]] = []
    for side, raw in (("YES", yes_ask), ("NO", no_ask)):
        if not raw or float(raw) <= 0:
            continue
        raw_f = float(raw)
        if raw_f > _MAX_BUY + 1e-9:
            continue
        ask = clamp_buy_price(raw_f)
        if ask >= min_e - 1e-9 and ask <= _MAX_BUY + 1e-9:
            candidates.append((side, ask))
    if not candidates:
        return EntrySideChoice(EntryDecision.SKIP, None, 0.0, "price_below_minimum")
    candidates.sort(key=lambda row: (row[1], 0 if row[0] == "YES" else 1))
    side, px = candidates[0]
    return EntrySideChoice(EntryDecision.BUY, side, px, "eligible")
