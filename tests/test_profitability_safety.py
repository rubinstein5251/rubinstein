"""Profitability gates, falling-knife, and execution safety tests."""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

from utils.falling_knife_filter import (
    FallingKnifeState,
    check_stabilized,
    evaluate_falling_knife,
    record_buy_on_side,
)
from utils.inventory_cost import (
    InventorySnapshot,
    PurchaseDecision,
    evaluate_dca_purchase,
    evaluate_initial_hedge,
    evaluate_paired_dca_purchase,
)
from utils.inventory_balance import EffectiveInventory, plan_single_leg_buy
from utils.profitability_gate import (
    evaluate_first_leg_gate,
    evaluate_initial_hedge_gate,
    evaluate_paired_buy_gate,
    projected_net_profit_per_pair,
)


class ProfitabilityGateTests(unittest.TestCase):
    def test_forced_hedge_yes35_no67_rejected(self):
        snap = InventorySnapshot(yes_shares=100, no_shares=0, yes_cost=35, no_cost=0)
        gate = evaluate_initial_hedge_gate(
            snap, "NO", 100, 0.67, max_combined_cost=0.96, min_net_profit_per_pair=0.02, slippage_reserve=0.005,
        )
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "REJECT_COMBINED_COST_TOO_HIGH")

    def test_initial_hedge_eval_wrapper_rejects(self):
        snap = InventorySnapshot(yes_shares=100, no_shares=0, yes_cost=35, no_cost=0)
        ev = evaluate_initial_hedge(
            snap, "NO", 100, 0.67, max_slippage=0.01, limit_price=0.68,
            max_combined_cost=0.96, min_net_profit_per_pair=0.02, slippage_reserve=0.005,
        )
        self.assertEqual(ev.decision, PurchaseDecision.SKIP)
        self.assertEqual(ev.reason, "REJECT_COMBINED_COST_TOO_HIGH")

    def test_combined_96_still_rejects_when_fees_eat_edge(self):
        snap = InventorySnapshot(yes_shares=0, no_shares=0, yes_cost=0, no_cost=0, fee_rate=0.05)
        gate = evaluate_paired_buy_gate(
            snap, 5, 0.475, 0.475,
            max_combined_cost=1.0,
            min_net_profit_per_pair=0.02,
            slippage_reserve=0.0,
            require_improvement=False,
        )
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "REJECT_NET_PROFIT_TOO_LOW")

    def test_full_size_executable_cost_rejects_paired_dca(self):
        snap = InventorySnapshot(yes_shares=10, no_shares=10, yes_cost=3.5, no_cost=3.5)
        ev = evaluate_paired_dca_purchase(
            snap, 5, 0.50, 0.50,
            combined_cost_target=0.96, max_combined_cost=0.96,
            min_net_profit_per_pair=0.02, slippage_reserve=0.01,
        )
        self.assertEqual(ev.decision, PurchaseDecision.SKIP)

    def test_first_leg_rejects_unprofitable_opposite(self):
        gate = evaluate_first_leg_gate(
            side="YES",
            qty=5,
            leg_executable_price=0.35,
            opposite_executable_price=0.67,
            fee_rate=0.0,
            max_combined_cost=0.96,
            min_net_profit_per_pair=0.02,
            slippage_reserve=0.005,
            max_entry_price=0.35,
        )
        self.assertFalse(gate.allowed)


class FallingKnifeTests(unittest.TestCase):
    def _hist_drop(self, start: float, end: float, n: int = 8) -> list[tuple[float, float]]:
        now = time.time()
        step = 15.0 / max(n - 1, 1)
        out = []
        for i in range(n):
            frac = i / max(n - 1, 1)
            px = start + (end - start) * frac
            out.append((now - 15.0 + i * step, px))
        out.append((now, end))
        return out

    def test_sudden_decline_triggers_knife(self):
        hist = self._hist_drop(0.50, 0.40)
        state = FallingKnifeState()
        block, reason, _ = evaluate_falling_knife(
            side="YES",
            ask=0.40,
            history=hist,
            state=state,
            enabled=True,
            window_seconds=15.0,
            fast_window_seconds=1.0,
            max_price_drop_fraction=0.03,
            fast_drop_fraction=0.015,
            cooldown_seconds=5.0,
            recovery_cents=1.5,
            stabilization_window_seconds=15.0,
            stabilization_tolerance_percent=0.02,
            max_consecutive_buys=10,
        )
        self.assertTrue(block)
        self.assertEqual(reason, "REJECT_FALLING_KNIFE_COOLDOWN")

    def test_cooldown_without_stabilization_blocks_reentry(self):
        hist = self._hist_drop(0.50, 0.40)
        state = FallingKnifeState()
        state.sides["YES"].block_until = time.time() + 30
        state.sides["YES"].latched = True
        block, reason, _ = evaluate_falling_knife(
            side="YES",
            ask=0.41,
            history=hist,
            state=state,
            enabled=True,
            window_seconds=15.0,
            fast_window_seconds=1.0,
            max_price_drop_fraction=0.50,
            fast_drop_fraction=0.50,
            cooldown_seconds=5.0,
            recovery_cents=1.5,
            stabilization_window_seconds=15.0,
            stabilization_tolerance_percent=0.001,
            max_consecutive_buys=10,
        )
        self.assertTrue(block)
        self.assertEqual(reason, "REJECT_NOT_STABILIZED")

    def test_stabilization_allows_recheck(self):
        now = time.time()
        hist = [(now - 10, 0.40), (now - 5, 0.401), (now, 0.402)]
        self.assertTrue(
            check_stabilized(
                hist, 0.402,
                stabilization_window_seconds=15.0,
                stabilization_tolerance_percent=0.02,
            )
        )

    def test_consecutive_buys_cap(self):
        state = FallingKnifeState()
        state.sides["YES"].consecutive_buys = 3
        block, reason, _ = evaluate_falling_knife(
            side="YES",
            ask=0.35,
            history=[(time.time(), 0.35)],
            state=state,
            enabled=True,
            window_seconds=15.0,
            fast_window_seconds=1.0,
            max_price_drop_fraction=0.99,
            fast_drop_fraction=0.99,
            cooldown_seconds=0.0,
            recovery_cents=0.0,
            stabilization_window_seconds=15.0,
            stabilization_tolerance_percent=0.5,
            max_consecutive_buys=3,
        )
        self.assertTrue(block)
        self.assertEqual(reason, "REJECT_MAX_CONSECUTIVE_BUYS")

    def test_record_buy_resets_opposite_side(self):
        state = FallingKnifeState()
        record_buy_on_side(state, "YES")
        record_buy_on_side(state, "YES")
        self.assertEqual(state.sides["YES"].consecutive_buys, 2)
        self.assertEqual(state.sides["NO"].consecutive_buys, 0)


class InventoryAndExecutionTests(unittest.TestCase):
    def test_pending_in_effective_inventory(self):
        snap = InventorySnapshot(yes_shares=10, no_shares=10, yes_cost=7, no_cost=3)
        eff = EffectiveInventory(yes_confirmed=10, no_confirmed=10, pending_yes=5, pending_no=0)
        plan = plan_single_leg_buy(
            snap=snap,
            eff=eff,
            side="YES",
            requested_qty=5,
            min_order_size=5,
            max_share_imbalance=0,
            max_unmatched_shares=0,
            balance_inventory_before_buy=True,
            allow_unmatched_accumulation=False,
            purpose="dca_accumulation",
        )
        self.assertEqual(plan.approval.value, "rejected")

    def test_unmatched_limit_rejects_rebalance(self):
        snap = InventorySnapshot(yes_shares=25, no_shares=10, yes_cost=16, no_cost=2.5)
        eff = EffectiveInventory(yes_confirmed=25, no_confirmed=10)
        plan = plan_single_leg_buy(
            snap=snap,
            eff=eff,
            side="NO",
            requested_qty=5,
            min_order_size=5,
            max_share_imbalance=0,
            max_unmatched_shares=4,
            balance_inventory_before_buy=True,
            allow_unmatched_accumulation=False,
            purpose="dca_rebalance",
        )
        self.assertEqual(plan.approval.value, "rejected")

    def test_idempotent_fill_processing(self):
        class Stub:
            _processed_fill_order_ids = set()

            def record_fill_idempotent(self, oid, side, qty, px):
                key = f"{oid}:{side}:{qty:.6f}:{px:.6f}"
                if key in self._processed_fill_order_ids:
                    return False
                self._processed_fill_order_ids.add(key)
                return True

        s = Stub()
        self.assertTrue(s.record_fill_idempotent("o1", "YES", 5, 0.35))
        self.assertFalse(s.record_fill_idempotent("o1", "YES", 5, 0.35))

    def test_net_profit_math(self):
        self.assertAlmostEqual(projected_net_profit_per_pair(0.96, slippage_reserve=0.01), 0.03)


class DryRunAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_does_not_call_live_place_without_auth(self):
        import strategies.fok_execution as ex

        class W:
            def is_dry_run(self):
                return True

            def refresh_order_book(self, side):
                pass

            def executable_buy(self, side, size):
                return (0.30, size)

            async def place_strategy_fok_buy(self, side, price, size, purpose=""):
                return ("dry-run", float(size))

            async def resolve_fill_price(self, oid, size, price):
                return 0.30

            def _untrack_order(self, oid):
                pass

            def resolved_entry_fok_limit(self):
                from utils.entry_limit import resolve_entry_fok_limit
                return resolve_entry_fok_limit(0.35)

            worker_config = SimpleNamespace(
                fill_timeout_ms=500,
                fill_poll_ms=50,
                max_slippage=0.005,
            )

        qty, px, oid = await ex.fok_buy_limit(W(), "YES", 0.35, 5.0, purpose="test")
        self.assertGreater(qty, 0)
        self.assertEqual(oid, "dry-run")


if __name__ == "__main__":
    unittest.main()
