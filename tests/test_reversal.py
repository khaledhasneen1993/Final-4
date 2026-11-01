"""Final-4 hard-stop, profit-lock and hedge behavior (offline)."""

import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from cascade_final4_paper import Final4PaperHunter, PROFIT_LOCK_LEVELS
from fixtures import DynamicPublicData

S = "BTC/USDT:USDT"


class Final4Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = DynamicPublicData()
        self.path = Path(self.tmp.name) / "state.json"
        self.bot = Final4PaperHunter(self.path, market_data=self.data)
        self.bot.detect_cascade = AsyncMock(
            return_value={"long": False, "short": False}
        )

    async def entry(self, side="buy"):
        self.data.price = 100
        self.assertTrue(await self.bot.enter(S, side, 100))

    async def hedge(self, side="buy"):
        await self.entry(side)
        self.data.price = 98.2 if side == "buy" else 101.8
        self.bot.detect_cascade.return_value = {
            "long": side == "sell",
            "short": side == "buy",
        }
        await self.bot.manage_exits()
        child = next(k for k in self.bot.positions if k != S)
        return child

    async def test_initial_margin_fill_basis_and_three_percent_stop(self):
        self.data.price = 101
        self.assertTrue(await self.bot.enter(S, "buy", 99))
        p = self.bot.positions[S]
        self.assertEqual(p["entry"], 101)
        self.assertAlmostEqual(p["sl"], 101 * 0.97)
        self.assertAlmostEqual(self.bot.exchange.used, 10, places=2)
        self.assertIsNone(p["tp"])
        self.assertEqual(self.bot.settings()["STOP_LOSS_PCT"], 0.03)
        self.assertEqual(self.bot.settings()["MAX_CONCURRENT_POSITIONS"], 5)

    async def test_all_profit_locks_both_directions_and_never_loosen(self):
        for side in ["buy", "sell"]:
            self.bot = Final4PaperHunter(
                Path(self.tmp.name) / (side + ".json"), market_data=self.data
            )
            await self.entry(side)
            direction = 1 if side == "buy" else -1
            for trigger, lock in PROFIT_LOCK_LEVELS:
                self.data.price = 100 * (1 + direction * trigger)
                await self.bot.manage_exits()
                self.assertAlmostEqual(
                    self.bot.positions[S]["sl"], 100 * (1 + direction * lock)
                )
                self.assertEqual(self.bot.positions[S]["stop_kind"], "PROFIT_LOCK")
            stop = self.bot.positions[S]["sl"]
            self.data.price = 100 * (1 + direction * 0.035)
            await self.bot.manage_exits()
            self.assertEqual(self.bot.positions[S]["sl"], stop)

    async def test_first_one_percent_level_sets_price_break_even(self):
        await self.entry()
        self.data.price = 101
        await self.bot.manage_exits()
        self.assertEqual(self.bot.positions[S]["sl"], 100)
        self.data.price = 99.99
        await self.bot.manage_exits()
        self.assertFalse(self.bot.positions)
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "PROFIT_LOCK")

    async def test_both_hedge_directions_have_separate_margin_and_market_symbol(self):
        for side in ["buy", "sell"]:
            self.bot = Final4PaperHunter(
                Path(self.tmp.name) / (side + ".json"), market_data=self.data
            )
            self.bot.detect_cascade = AsyncMock()
            child = await self.hedge(side)
            self.assertEqual(len(self.bot.exchange.positions), 2)
            self.assertNotEqual(
                self.bot.positions[S]["side"], self.bot.positions[child]["side"]
            )
            f = self.bot.exchange.positions[child]
            self.assertEqual(f["symbol"], S)
            self.assertAlmostEqual(f["entry"] * f["amount"], 300, delta=0.11)
            self.assertAlmostEqual(self.bot.exchange.used, 16, delta=0.01)
            self.assertTrue(self.bot.positions[child]["is_reversal"])
            self.assertTrue(self.bot.positions[S]["reversed"])
            expected = self.bot.exchange.balance + sum(
                (self.data.price - p["entry"])
                * p["amount"]
                * (1 if p["side"] == "buy" else -1)
                for p in self.bot.exchange.positions.values()
            )
            self.assertAlmostEqual(self.bot.exchange.equity, expected)

    async def test_success_closes_parent_keeps_child_and_independent_ledger(self):
        child = await self.hedge()
        self.data.price = 97.02
        await self.bot.manage_exits()
        self.assertNotIn(S, self.bot.positions)
        self.assertIn(child, self.bot.positions)
        self.assertFalse(self.bot.positions[child]["is_reversal"])
        self.assertIsNone(self.bot.positions[child]["parent"])
        self.assertEqual(self.bot.exchange.trades[-1]["position_id"], S)
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "REVERSAL_WIN")
        self.assertTrue(self.bot.has_market_position(S))
        self.bot.last_trade.clear()
        self.assertFalse(await self.bot.enter(S, "buy", self.data.price))
        from continuous_scanner import ContinuousScanner

        self.assertTrue(ContinuousScanner(self.bot).has_position(S))

    async def test_child_stop_does_not_close_parent_or_open_another_child(self):
        child = await self.hedge()
        self.data.price = 101.2
        await self.bot.manage_exits()
        self.assertIn(S, self.bot.positions)
        self.assertNotIn(child, self.bot.positions)
        self.assertTrue(self.bot.positions[S]["reversed"])
        self.assertFalse(
            await self.bot.try_open_reversal(S, 98.2, self.bot.positions[S])
        )
        self.assertEqual(len(self.bot.exchange.trades), 3)
        self.assertEqual(self.bot.exchange.trades[-1]["position_id"], child)

    async def test_close_parent_directly_preserves_and_promotes_child(self):
        child = await self.hedge()
        await self.bot.close_position(S, "SL", self.data.price)
        self.assertEqual(set(self.bot.positions), {child})
        self.assertFalse(self.bot.positions[child]["is_reversal"])

    async def test_confirmation_does_not_happen_below_one_point_two(self):
        child = await self.hedge()
        self.data.price = self.bot.positions[child]["entry"] * 0.989
        await self.bot.manage_exits()
        self.assertEqual(len(self.bot.positions), 2)

    async def test_reversal_needs_opposite_signal_and_capacity(self):
        await self.entry()
        self.data.price = 98.2
        await self.bot.manage_exits()
        self.assertEqual(len(self.bot.positions), 1)
        self.bot.detect_cascade.return_value = {"long": False, "short": True}
        for i in range(4):
            key = f"OTHER{i}"
            self.bot.positions[key] = {"symbol": key}
        self.assertFalse(
            await self.bot.try_open_reversal(S, 98.2, self.bot.positions[S])
        )
        self.assertEqual(len(self.bot.exchange.trades), 1)

    async def test_insufficient_margin_does_not_mark_reversed(self):
        await self.entry()
        self.bot.exchange.balance = 10.1
        self.data.price = 98.2
        self.bot.detect_cascade.return_value = {"long": False, "short": True}
        self.assertFalse(
            await self.bot.try_open_reversal(S, 98.2, self.bot.positions[S])
        )
        self.assertFalse(self.bot.positions[S]["reversed"])
        self.assertEqual(len(self.bot.exchange.positions), 1)

    async def test_gap_past_stop_before_reversal_fill_closes_original(self):
        await self.entry()
        calls = 0

        async def ticker(symbol):
            nonlocal calls
            self.assertEqual(symbol, S)
            calls += 1
            return {"last": 98.2 if calls == 1 else 96, "timestamp": time.time() * 1000}

        self.data.fetch_ticker = ticker
        self.bot.detect_cascade.return_value = {"long": False, "short": True}
        await self.bot.manage_exits()
        self.assertFalse(self.bot.positions)
        self.assertEqual(len(self.bot.exchange.trades), 2)
        self.assertTrue(self.bot.exchange.trades[-1]["reduceOnly"])

    async def test_rebound_before_fill_cancels_reversal(self):
        await self.entry()
        self.data.price = 100
        self.bot.detect_cascade.return_value = {"long": False, "short": True}
        self.assertFalse(
            await self.bot.try_open_reversal(S, 98.2, self.bot.positions[S])
        )
        self.assertFalse(self.bot.positions[S]["reversed"])
        self.assertEqual(len(self.bot.exchange.trades), 1)

    async def test_restore_both_sides_and_no_synthetic_tickers(self):
        child = await self.hedge()
        self.bot.save()
        restored = Final4PaperHunter(self.path, market_data=self.data)
        self.assertEqual(restored.positions, self.bot.positions)
        self.assertEqual(restored.exchange.positions, self.bot.exchange.positions)
        restored.detect_cascade = AsyncMock(return_value=None)
        original = self.data.fetch_ticker

        async def checked(symbol):
            self.assertEqual(symbol, S)
            return await original(symbol)

        self.data.fetch_ticker = checked
        await restored.manage_exits()
        self.assertEqual(len(restored.exchange.trades), 2)

    async def test_stale_reversal_fill_does_not_mutate_account(self):
        await self.entry()
        before = copy.deepcopy(self.bot.exchange.positions)

        async def stale(symbol):
            return {"last": 98.2, "timestamp": (time.time() - 30) * 1000}

        self.data.fetch_ticker = stale
        self.bot.detect_cascade.return_value = {"long": False, "short": True}
        self.assertFalse(
            await self.bot.try_open_reversal(S, 98.2, self.bot.positions[S])
        )
        self.assertEqual(self.bot.exchange.positions, before)
        self.assertFalse(self.bot.positions[S]["reversed"])


if __name__ == "__main__":
    unittest.main()
