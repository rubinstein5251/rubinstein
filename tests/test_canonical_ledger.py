"""Canonical ledger idempotency and P&L view tests."""

from __future__ import annotations

import os
import tempfile
import unittest

from utils.canonical_ledger import CanonicalLedger
from utils.momentum_inventory import PositionInventory


class CanonicalLedgerTests(unittest.TestCase):
    def test_duplicate_fill_not_double_counted(self):
        path = os.path.join(tempfile.gettempdir(), f"ledger_{id(self)}.json")
        ledger = CanonicalLedger(path=path, snapshot_path=path + ".pnl")
        ok1 = ledger.record_fill(
            worker_key="btc:5m",
            market_id="m1",
            order_id="o1",
            side="YES",
            action="BUY",
            price=0.7,
            quantity=5.0,
        )
        ok2 = ledger.record_fill(
            worker_key="btc:5m",
            market_id="m1",
            order_id="o1",
            side="YES",
            action="BUY",
            price=0.7,
            quantity=5.0,
        )
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual(ledger.worker_summary("btc:5m").realized_pnl, 0.0)

    def test_sell_realized_once(self):
        path = os.path.join(tempfile.gettempdir(), f"ledger2_{id(self)}.json")
        ledger = CanonicalLedger(path=path, snapshot_path=path + ".pnl")
        ledger.record_fill(
            worker_key="btc:5m",
            market_id="m1",
            order_id="s1",
            side="NO",
            action="SELL",
            price=0.45,
            quantity=5.0,
            realized_pnl_delta=-1.25,
            count_win_loss=True,
        )
        snap = ledger.worker_summary("btc:5m")
        self.assertAlmostEqual(snap.realized_pnl, -1.25)
        self.assertEqual(snap.losses, 1)

    def test_accounting_view_matches_inventory(self):
        inv = PositionInventory()
        inv.record_buy("YES", 5.0, 0.87)
        inv.record_buy("NO", 5.0, 0.11)
        from utils.inventory_cost import InventorySnapshot, matched_combined_average

        snap = InventorySnapshot.from_ledger(inv)
        combined = matched_combined_average(snap)
        view = CanonicalLedger().build_accounting_view(
            worker_key="btc:5m",
            unrealized_pnl=0.1,
            inventory_snapshot={
                "matched_pairs": inv.matched_pairs,
                "matched_combined_cost": combined,
                "yes_shares": inv.yes_shares,
                "no_shares": inv.no_shares,
                "unpaired_yes": snap.unpaired_yes,
                "unpaired_no": snap.unpaired_no,
            },
        )
        self.assertAlmostEqual(view["total_net_pnl"], view["realized_pnl"] + 0.1)
        self.assertEqual(view["matched_pairs"], 5.0)


if __name__ == "__main__":
    unittest.main()
