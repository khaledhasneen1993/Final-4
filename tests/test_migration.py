import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from cascade_final4_paper import Final4PaperHunter
from migrate_state import previous_reversal_settings, unfinished_reversal_settings
from fixtures import DynamicPublicData

S = "BTC/USDT:USDT"


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_two_sided_account_changes_only_repository_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old-reversal.json"
            data = DynamicPublicData()
            bot = Final4PaperHunter(path, market_data=data)
            await bot.enter(S, "buy", 100)
            bot.detect_cascade = AsyncMock(return_value={"short": True, "long": False})
            data.price = 98.2
            await bot.manage_exits()
            self.assertEqual(len(bot.positions), 2)
            old = json.loads(path.read_text())
            old["settings"] = previous_reversal_settings()
            path.write_text(json.dumps(old))
            before = path.read_bytes()
            restored = Final4PaperHunter(path, market_data=data)
            after = json.loads(path.read_text())
            self.assertEqual(after, {**old, "settings": restored.settings()})
            self.assertEqual(restored.positions, bot.positions)
            self.assertEqual(restored.exchange.balance, bot.exchange.balance)
            self.assertEqual(next(path.parent.glob("*.bak")).read_bytes(), before)
            await restored.manage_exits()
            self.assertEqual(len(restored.exchange.trades), 2)

    async def test_unfinished_plain_reversal_account_can_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.json"
            data = DynamicPublicData()
            bot = Final4PaperHunter(path, market_data=data)
            await bot.enter(S, "buy", 100)
            state = json.loads(path.read_text())
            state["settings"] = unfinished_reversal_settings()
            path.write_text(json.dumps(state))
            restored = Final4PaperHunter(path, market_data=data)
            self.assertEqual(restored.positions[S]["sl"], 97)
            self.assertEqual(restored.exchange.balance, bot.exchange.balance)
            self.assertEqual(len(list(path.parent.glob("*.bak"))), 1)

    async def test_partial_strategy_state_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrong.json"
            data = DynamicPublicData()
            bot = Final4PaperHunter(path, market_data=data)
            await bot.enter(S, "buy", 100)
            state = json.loads(path.read_text())
            state["settings"]["mode"] = "final3_staged_partial"
            path.write_text(json.dumps(state))
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                Final4PaperHunter(path, market_data=data)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
