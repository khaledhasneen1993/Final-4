import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from cascade_final4_paper import Final4PaperHunter
from fixtures import DynamicPublicData
import test_continuous as base

BTC, ETH, SOL = base.BTC, base.ETH, base.SOL


class FreshnessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ContinuousTests.asyncSetUp

    async def test_partial_freeze_detected_while_other_symbols_update(self):
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            self.scanner.ingest(base.event(symbol))
        self.mono += 16
        self.wall += 16
        self.scanner.ingest(base.event("BTCUSDT", offset=16001))
        self.assertEqual(
            set(self.scanner.stale_symbols((BTC, ETH, SOL), 100)), {ETH, SOL}
        )
        self.assertIsNone(self.scanner.signal(ETH))
        self.assertEqual(self.scanner.stale_symbols((BTC,), 100), [])

    async def test_never_received_symbols_have_startup_grace(self):
        self.assertEqual(self.scanner.stale_symbols((BTC,), 100), [])
        self.mono += 16
        self.assertEqual(self.scanner.stale_symbols((BTC,), 100), [BTC])

    async def test_watchdog_reconnects_open_silent_socket_and_reseeds(self):
        tracker = {"opened": 0, "closed": 0, "seeds": 0}
        opened_twice = asyncio.Event()
        original_seed = self.scanner.seed

        async def seed(symbols):
            tracker["seeds"] += 1
            return await original_seed(symbols)

        outer = self

        class Socket:
            async def __aenter__(self):
                tracker["opened"] += 1
                self.number = tracker["opened"]
                if self.number == 2:
                    opened_twice.set()
                return self

            async def __aexit__(self, *args):
                tracker["closed"] += 1

            def __aiter__(self):
                async def messages():
                    await asyncio.sleep(0.02)
                    if self.number == 1:
                        outer.mono += 16
                    await asyncio.Future()
                    yield None

                return messages()

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def ws_connect(self, *args, **kwargs):
                return Socket()

        self.scanner.seed = seed
        with patch(
            "continuous_scanner.aiohttp.ClientSession", return_value=Session()
        ), patch("continuous_scanner.WATCHDOG_INTERVAL", 0.005):
            task = asyncio.create_task(self.scanner.stream_loop())
            try:
                await asyncio.wait_for(opened_twice.wait(), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertGreaterEqual(tracker["seeds"], 2)
        self.assertEqual(tracker["opened"], tracker["closed"])
        self.assertFalse(self.scanner.connected)

    async def test_stale_frames_do_not_reset_freshness(self):
        self.scanner.ingest(base.event())
        self.mono += 16
        self.wall += 16
        self.scanner.ingest(base.event(offset=2))
        self.assertEqual(self.scanner.received[BTC], 100)
        self.assertEqual(self.scanner.stale_symbols((BTC,), 100), [BTC])

    async def test_hung_seed_times_out(self):
        async def hung(*args, **kwargs):
            await asyncio.Future()

        self.data.fetch_ohlcv = hung
        with patch("continuous_scanner.SEED_TIMEOUT", 0.01):
            self.assertEqual(await self.scanner.seed((BTC,)), [])

    async def test_watchdog_cancellation_is_clean(self):
        task = asyncio.create_task(self.scanner.freshness_watchdog((BTC,)))
        await asyncio.sleep(0)
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)


class ExecutionFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = DynamicPublicData()
        self.bot = Final4PaperHunter(
            Path(self.temp.name) / "state.json", market_data=self.data
        )

    async def test_stale_missing_and_future_ticker_do_not_mutate_account(self):
        for timestamp in (
            None,
            (time.time() - 60) * 1000,
            (time.time() + 60) * 1000,
            float("nan"),
        ):

            async def ticker(symbol):
                return {"last": 100, "timestamp": timestamp}

            self.data.fetch_ticker = ticker
            with self.assertRaises(ValueError):
                await self.bot.exchange.create_order(BTC, "market", "buy", 1)
            self.assertEqual(self.bot.exchange.balance, 100)
            self.assertEqual(self.bot.exchange.trades, [])
            self.assertEqual(self.bot.exchange.positions, {})

    async def test_stale_close_preserves_open_position(self):
        await self.bot.enter(BTC, "buy", 100)
        before = self.bot.exchange.balance

        async def stale(symbol):
            return {"last": 90, "timestamp": (time.time() - 60) * 1000}

        self.data.fetch_ticker = stale
        await self.bot.manage_exits()
        self.assertIn(BTC, self.bot.positions)
        self.assertIn(BTC, self.bot.exchange.positions)
        self.assertEqual(len(self.bot.exchange.trades), 1)
        self.assertEqual(self.bot.exchange.balance, before)

    async def test_slow_ticker_times_out_without_fill(self):
        async def hung(symbol):
            await asyncio.Future()

        self.data.fetch_ticker = hung
        with patch("paper_account.TICKER_TIMEOUT", 0.01):
            with self.assertRaises(asyncio.TimeoutError):
                await self.bot.exchange.create_order(BTC, "market", "buy", 1)
        self.assertEqual(self.bot.exchange.trades, [])

    async def test_execution_recheck_rejects_stale_second_quote(self):
        await self.bot.enter(BTC, "buy", 100)
        calls = 0

        async def ticker(symbol):
            nonlocal calls
            calls += 1
            return {
                "last": 92,
                "timestamp": (time.time() - (60 if calls > 1 else 0)) * 1000,
            }

        self.data.fetch_ticker = ticker
        await self.bot.manage_exits()
        self.assertEqual(calls, 2)
        self.assertIn(BTC, self.bot.positions)
        self.assertEqual(len(self.bot.exchange.trades), 1)
