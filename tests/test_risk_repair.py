"""Offline regressions for failures observed in the paper session."""

import asyncio
import copy
import importlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fixtures import DynamicPublicData
from continuous_scanner import ContinuousScanner
import continuous_scanner as stream

IS_THREE = (Path(__file__).resolve().parents[1] / "cascade_final3_paper.py").exists()
mod = importlib.import_module(
    "cascade_final3_paper" if IS_THREE else "cascade_final4_paper"
)
Hunter = mod.Final3PaperHunter if IS_THREE else mod.Final4PaperHunter
S = "BTC/USDT:USDT"
E = "ETH/USDT:USDT"


class RiskRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "account.json"
        self.data = DynamicPublicData()
        self.bot = Hunter(self.path, market_data=self.data)
        self.sc = self.bot.scanner
        self.now = int(time.time() // 60) * 60000
        self.mono = 100.0
        self.wall = self.now / 1000
        self.sc.clock = lambda: self.mono
        self.sc.wall_clock = lambda: self.wall

        async def candles(symbol, timeframe, limit):
            return [
                [self.now // 60000 * 60000 - (29 - i) * 60000, 100, 100, 100, 100, 1]
                for i in range(30)
            ]

        self.data.fetch_ohlcv = candles
        await self.sc.refresh_universe()
        await self.sc.seed(self.sc.symbols)

    def update(self, symbol=S, price=100, volume=1):
        self.sc.connected = True
        self.sc.ingest(
            {
                "e": "kline",
                "E": self.now + 1,
                "s": symbol.split(":")[0].replace("/", ""),
                "st": 1,
                "k": {
                    "s": symbol.split(":")[0].replace("/", ""),
                    "i": "1m",
                    "t": self.now // 60000 * 60000,
                    "o": "100",
                    "h": str(max(price, 100)),
                    "l": str(min(price, 100)),
                    "c": str(price),
                    "v": str(volume),
                },
            }
        )
        self.now += 1

    async def test_stream_quote_is_used_for_execution_instead_of_different_rest_price(
        self,
    ):
        self.update(price=100)
        self.data.price = 106
        rest = AsyncMock(side_effect=AssertionError("REST should not be used"))
        self.data.fetch_ticker = rest
        self.assertTrue(await self.bot.enter(S, "buy", 100))
        self.assertEqual(self.bot.positions[S]["entry"], 100)
        self.assertEqual(
            self.bot.exchange.trades[-1]["price_source"], "websocket_kline_last"
        )
        rest.assert_not_awaited()
        self.assertAlmostEqual(self.bot.exchange.used, 1)

    async def test_stale_stream_never_masks_stale_rest(self):
        self.update()
        self.mono += 4
        self.wall += 4
        self.data.fetch_ticker = AsyncMock(
            return_value={"last": 100, "timestamp": (time.time() - 20) * 1000}
        )
        self.assertFalse(await self.bot.enter(S, "buy", 100))
        self.assertFalse(self.bot.exchange.trades)

    async def test_signal_execution_divergence_rejected_without_fees_or_position(self):
        self.data.price = 106
        self.assertFalse(await self.bot.enter(S, "buy", 100))
        self.assertEqual(self.bot.exchange.balance, 100)
        self.assertFalse(self.bot.exchange.positions)

    async def test_rest_older_than_two_seconds_rejected(self):
        self.data.fetch_ticker = AsyncMock(
            return_value={"last": 100, "timestamp": (time.time() - 3) * 1000}
        )
        self.assertFalse(await self.bot.enter(S, "buy", 100))
        self.assertFalse(self.bot.exchange.trades)

    async def test_fee_aware_lock_exits_net_positive_at_its_stop_both_sides(self):
        for side in ("buy", "sell"):
            self.bot = Hunter(
                Path(self.tmp.name) / (side + ".json"), market_data=self.data
            )
            self.data.price = 100
            self.assertTrue(await self.bot.enter(S, side, 100))
            self.data.price = 101 if side == "buy" else 99
            await self.bot.manage_exits()
            stop = self.bot.positions[S]["sl"]
            self.data.price = stop
            await self.bot.manage_exits()
            self.assertNotIn(S, self.bot.positions)
            self.assertGreater(self.bot.exchange.trades[-1]["position_net_pnl_usdt"], 0)

    async def test_recovered_stop_does_not_close_on_an_obsolete_trigger(self):
        self.assertTrue(await self.bot.enter(S, "buy", 100))
        self.data.price = 100
        with self.assertRaisesRegex(ValueError, "Stop condition no longer met"):
            await self.bot.close_position(S, "SL", 97)
        self.assertIn(S, self.bot.positions)
        self.assertEqual(len(self.bot.exchange.trades), 1)

    async def test_drawdown_blocks_entries_survives_restart_and_does_not_block_exits(
        self,
    ):
        self.assertTrue(await self.bot.enter(S, "buy", 100))
        self.bot.exchange.balance = 93
        self.assertFalse(await self.bot.enter(E, "buy", 100))
        restored = Hunter(self.path, market_data=self.data)
        self.assertTrue(restored.risk_memory["entries_paused"])
        self.assertFalse(await restored.enter(E, "buy", 100))
        await restored.close_position(S, "TEST", 100)
        self.assertFalse(restored.positions)

    async def test_close_cooldown_blocks_immediate_reentry(self):
        self.assertTrue(await self.bot.enter(S, "buy", 100))
        await self.bot.close_position(S, "TEST", 100)
        self.assertFalse(await self.bot.enter(S, "buy", 100))
        self.bot.last_trade[S] -= 301
        self.assertTrue(await self.bot.enter(S, "buy", 100))

    async def test_partial_stream_freeze_keeps_healthy_connection(self):
        self.update(S)
        self.update(E)
        self.mono += 16
        self.wall += 16
        self.sc.received[S] = self.mono
        self.sc.event_times[S] = self.wall * 1000
        with patch.object(stream, "WATCHDOG_INTERVAL", 0.001):
            task = asyncio.create_task(
                self.sc.freshness_watchdog(tuple(self.sc.symbols))
            )
            await asyncio.sleep(0.006)
            self.assertFalse(task.done())
            self.assertTrue(self.sc.connected)
            self.assertIsNone(self.sc.signal(E))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_old_risk_account_with_open_positions_is_not_silently_rewritten(self):
        self.assertTrue(await self.bot.enter(S, "buy", 100))
        state = json.loads(self.path.read_text())
        from migrate_state import LEGACY_APPROVED_SETTINGS

        state["settings"] = LEGACY_APPROVED_SETTINGS
        self.path.write_text(json.dumps(state))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "OPEN positions"):
            Hunter(self.path, market_data=self.data)
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(IS_THREE, "Final-3 partial feature")
    async def test_partial_rechecks_adverse_level_after_price_recovers(self):
        self.assertTrue(await self.bot.enter(S, "buy", 100))
        self.data.price = 100
        self.assertFalse(await self.bot.partial_close(S, 0.4, 98.7, "PARTIAL_1"))
        self.assertEqual(self.bot.positions[S]["amount"], 0.5)
        self.assertEqual(len(self.bot.exchange.trades), 1)

    @unittest.skipIf(IS_THREE, "Final-4 reversal feature")
    async def test_live_reversal_with_lighter_confirmation_opens_and_records(self):
        self.assertTrue(await self.bot.enter(S, "buy", 100))
        self.update(price=98.2, volume=2)
        self.data.fetch_order_book = AsyncMock(
            return_value={"bids": [[98.2, 1]], "asks": [[98.2, 9]]}
        )
        await self.bot.manage_exits()
        self.assertEqual(len(self.bot.positions), 2)
        self.assertTrue(self.bot.positions[S]["reversed"])
        self.assertEqual(self.bot.reversal_stats["opened"], 1)
        restored = Hunter(self.path, market_data=self.data)
        self.assertEqual(restored.reversal_stats["opened"], 1)
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "REVERSAL_HEDGE")

    @unittest.skipIf(IS_THREE, "Final-4 reversal feature")
    async def test_reversal_missing_signal_has_persistent_rejection_reason(self):
        self.assertTrue(await self.bot.enter(S, "buy", 100))
        self.data.price = 98.2
        await self.bot.manage_exits()
        self.assertEqual(self.bot.reversal_stats["signal_rejected"], 1)
        restored = Hunter(self.path, market_data=self.data)
        self.assertEqual(restored.reversal_stats, self.bot.reversal_stats)

    async def test_idle_old_profile_upgrade_preserves_wallet_and_ledger_with_backup(
        self,
    ):
        from migrate_state import LEGACY_APPROVED_SETTINGS

        self.bot.save()
        state = json.loads(self.path.read_text())
        state["settings"] = LEGACY_APPROVED_SETTINGS
        state.update(balance_usdt=90, equity_usdt=90, free_usdt=90)
        self.path.write_text(json.dumps(state))
        before = self.path.read_bytes()
        restored = Hunter(self.path, market_data=self.data)
        self.assertEqual(restored.exchange.balance, 90)
        self.assertEqual(restored.exchange.trades, [])
        self.assertEqual(next(self.path.parent.glob("*.bak")).read_bytes(), before)
        self.assertFalse(await restored.enter(S, "buy", 100))

    async def test_gap_resync_does_not_close_healthy_stream(self):
        self.update(S)
        self.update(E)
        self.sc.seed = AsyncMock(return_value=[S])
        payload = {
            "e": "kline",
            "E": self.now + 120000,
            "s": "BTCUSDT",
            "st": 1,
            "k": {
                "s": "BTCUSDT",
                "i": "1m",
                "t": self.now // 60000 * 60000 + 120000,
                "o": "100",
                "h": "100",
                "l": "100",
                "c": "100",
                "v": "1",
            },
        }
        self.wall += 120

        class Message:
            type = stream.aiohttp.WSMsgType.TEXT

            def json(self):
                return payload

        class Socket:
            def __aiter__(self):
                async def rows():
                    yield Message()
                    await asyncio.Future()

                return rows()

        reader = asyncio.create_task(self.sc.read_stream(Socket()))
        await asyncio.sleep(0.005)
        self.assertFalse(reader.done())
        self.assertTrue(self.sc.connected)
        self.assertIn(E, self.sc.histories)
        self.sc.seed.assert_awaited_once_with((S,))
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    @unittest.skipUnless(IS_THREE, "Final-3 partial feature")
    async def test_second_partial_stop_improves_first_stop_for_both_directions(self):
        for side in ("buy", "sell"):
            self.bot = Hunter(
                Path(self.tmp.name) / (side + "-partial.json"), market_data=self.data
            )
            self.data.price = 100
            self.assertTrue(await self.bot.enter(S, side, 100))
            self.data.price = 98.79 if side == "buy" else 101.21
            await self.bot.manage_exits()
            first = self.bot.positions[S]["sl"]
            self.data.price = 98 if side == "buy" else 102
            await self.bot.manage_exits()
            second = self.bot.positions[S]["sl"]
            self.assertGreater((1 if side == "buy" else -1) * (second - first), 0)
            self.assertEqual(self.bot.positions[S]["stop_kind"], "TIGHT_2")

    @unittest.skipIf(IS_THREE, "Final-4 reserved hedge slot")
    async def test_ordinary_entries_leave_a_slot_for_reversal(self):
        self.assertTrue(await self.bot.enter(S, "buy", 100))
        self.assertTrue(await self.bot.enter(E, "buy", 100))
        self.assertFalse(await self.bot.enter("SOL/USDT:USDT", "buy", 100))
        self.data.price = 98.2
        self.bot.detect_cascade = AsyncMock(return_value={"short": True})
        self.assertTrue(
            await self.bot.try_open_reversal(S, 98.2, self.bot.positions[S])
        )
        self.assertEqual(len(self.bot.positions), 3)

    async def test_paused_scanner_does_not_keep_requesting_order_books(self):
        self.update(price=102, volume=10)
        self.bot.exchange.balance = 93
        self.bot.risk_memory.update(peak_balance=100, entries_paused=True)
        self.data.fetch_order_book = AsyncMock()
        await self.sc.evaluate(S)
        self.data.fetch_order_book.assert_not_awaited()
        self.assertFalse(self.bot.exchange.trades)
