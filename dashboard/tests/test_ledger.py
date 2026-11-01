import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ledger import round_trips


class LedgerTests(unittest.TestCase):
    def test_scale_fees_short_reentry(self):
        def fill(side, amount, price, fee, close=False, pnl=0):
            return dict(
                symbol="BTC/USDT:USDT",
                side=side,
                amount=amount,
                price=price,
                fee_usdt=fee,
                reduceOnly=close,
                pnl_usdt=pnl,
                timestamp="2026-10-30",
            )

        fills = [
            fill("sell", 2, 100, 0.1),
            fill("sell", 1, 110, 0.055),
            fill("buy", 3, 90, 0.135, True, 40),
            fill("buy", 1, 91, 0.0455),
        ]
        opened, closed = round_trips(fills)
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["adds"], 1)
        self.assertAlmostEqual(closed[0]["average_entry"], 310 / 3)
        self.assertAlmostEqual(closed[0]["net_pnl_usdt"], 39.71)
        self.assertEqual(opened[0]["side"], "buy")

    def test_partial_is_not_a_completed_position(self):
        def fill(side, amount, price, fee, close=False, pnl=0, partial=False):
            return dict(
                symbol="BTC",
                side=side,
                amount=amount,
                price=price,
                fee_usdt=fee,
                reduceOnly=close,
                pnl_usdt=pnl,
                partial_close=partial,
                timestamp="2026-10-31",
            )

        fills = [
            fill("buy", 2, 100, 0.1),
            fill("buy", 2, 98, 0.098),
            fill("sell", 2, 96, 0.096, True, -6, True),
        ]
        opened, closed = round_trips(fills)
        self.assertEqual(len(opened), 1)
        self.assertFalse(closed)
        self.assertEqual(opened[0]["remaining_amount"], 2)
        fills.append(fill("sell", 2, 100, 0.1, True, 2))
        opened, closed = round_trips(fills)
        self.assertFalse(opened)
        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0]["net_pnl_usdt"], -4.394)

    def test_two_opposite_positions_same_market_stay_independent(self):
        def fill(pid, side, close=False, price=100, pnl=0):
            return dict(
                position_id=pid,
                symbol="BTC",
                side=side,
                amount=1,
                price=price,
                fee_usdt=0.05,
                reduceOnly=close,
                pnl_usdt=pnl,
                timestamp="2026-11-01",
            )

        rows = [
            fill("parent", "buy"),
            fill("child", "sell"),
            fill("parent", "sell", True, 98, -2),
        ]
        result = round_trips(rows)
        opened, closed = result[:2]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["position_id"], "child")
        self.assertEqual(opened[0]["side"], "sell")
        self.assertEqual(closed[0]["position_id"], "parent")
        self.assertAlmostEqual(closed[0]["net_pnl_usdt"], -2.1)
        rows.append(fill("child", "buy", True, 97, 3))
        opened, closed = round_trips(rows)[:2]
        self.assertFalse(opened)
        self.assertEqual(len(closed), 2)
        self.assertAlmostEqual(sum(p["net_pnl_usdt"] for p in closed), 0.8)

    def test_empty(self):
        self.assertEqual(round_trips([]), ([], []))


if __name__ == "__main__":
    unittest.main()
