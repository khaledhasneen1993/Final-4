"""Final-4 paper hedge positions: 3% stops, profit locks and one reversal per position."""

import argparse
import asyncio
import fcntl
import math
import time
from pathlib import Path
from uuid import uuid4

from binance_cascade_hunter_dynamic_v2_paper import V2PaperHunter
from binance_cascade_hunter_dynamic_v2 import log
from paper_account import ReversalStopCrossed

HARD_STOP_PCT = 0.03
REVERSAL_TRIGGER = 0.018
REVERSAL_SIZE_MULT = 0.60
REVERSAL_CONFIRM_PROFIT = 0.012
PROFIT_LOCK_LEVELS = [
    (0.010, 0.000),
    (0.015, 0.005),
    (0.020, 0.010),
    (0.025, 0.015),
    (0.030, 0.020),
    (0.035, 0.025),
    (0.040, 0.030),
]


class Final4PaperHunter(V2PaperHunter):
    def __init__(
        self,
        state_path="paper_state_final4.json",
        fee_rate=0.0005,
        reset=False,
        market_data=None,
    ):
        if Path(state_path).exists() and not reset:
            from migrate_state import migrate

            migrate(state_path)
        super().__init__(state_path, fee_rate, reset, market_data)
        for key, p in self.positions.items():
            p.setdefault("symbol", self.exchange.positions[key].get("symbol", key))
            p.setdefault("position_id", key)
            p.setdefault("is_reversal", False)
            p.setdefault("profit_lock_trigger", 0.0)

    def settings(self):
        return {
            **super().settings(),
            "mode": "final4_smart_reversal",
            "STOP_LOSS_PCT": HARD_STOP_PCT,
            "TAKE_PROFIT_PCT": None,
            "hard_stop_pct": HARD_STOP_PCT,
            "hedge_positions": True,
            "reversal_trigger": REVERSAL_TRIGGER,
            "reversal_size_mult": REVERSAL_SIZE_MULT,
            "reversal_confirm_profit": REVERSAL_CONFIRM_PROFIT,
            "profit_lock_levels": [list(level) for level in PROFIT_LOCK_LEVELS],
        }

    def has_market_position(self, symbol):
        return any(p.get("symbol", key) == symbol for key, p in self.positions.items())

    def position(self, key, symbol, fill):
        side, entry = fill["side"], fill.get("entry", fill.get("price"))
        return dict(
            symbol=symbol,
            position_id=key,
            side=side,
            entry=entry,
            amount=fill["amount"],
            original_entry=entry,
            original_notional=entry * fill["amount"],
            initial_sl=entry
            * (1 - HARD_STOP_PCT if side == "buy" else 1 + HARD_STOP_PCT),
            sl=entry * (1 - HARD_STOP_PCT if side == "buy" else 1 + HARD_STOP_PCT),
            tp=None,
            adds_done=0,
            trailing=False,
            best_price=entry,
            reversed=False,
            reverse_of=None,
            parent=None,
            is_reversal=False,
            profit_locked=0.0,
            profit_lock_trigger=0.0,
            stop_kind="SL",
        )

    async def enter(self, symbol, side, price):
        now = time.time()
        if (
            self.has_market_position(symbol)
            or len(self.positions) >= self.strategy.MAX_CONCURRENT_POSITIONS
        ):
            return False
        if now - self.last_trade.get(symbol, -math.inf) < self.strategy.COOLDOWN_SEC:
            return False
        try:
            quote = (
                max(0.0, self.exchange.balance)
                * self.strategy.MAX_POSITION_PCT
                * self.strategy.LEVERAGE
            )
            fill = await self.exchange.create_order(
                symbol,
                "market",
                side,
                1,
                params={"quoteNotional": quote, "positionId": symbol},
            )
            self.positions[symbol] = self.position(symbol, symbol, fill)
            self.last_trade[symbol] = now
            self.exchange.trades[-1].update(
                entry_reason="CASCADE_MOVE_VOLUME_ORDERBOOK",
                stop_loss_price=self.positions[symbol]["sl"],
            )
            self.save()
            return True
        except Exception as error:
            log.warning("Entry failed %s: %s", symbol, error)
            return False

    @staticmethod
    def tighten_stop(p, candidate, kind):
        improves = candidate > p["sl"] if p["side"] == "buy" else candidate < p["sl"]
        if improves:
            p["sl"], p["stop_kind"] = candidate, kind

    def promote_reversal(self, p):
        p.update(is_reversal=False, parent=None, reverse_of=None)

    async def close_position(self, position_id, reason, trigger_price):
        p = self.positions.get(position_id)
        if p is None:
            return False
        symbol = p.get("symbol", position_id)
        await self.exchange.create_order(
            symbol,
            "market",
            "sell" if p["side"] == "buy" else "buy",
            p["amount"],
            params={
                "reduceOnly": True,
                "positionId": position_id,
                "exit_reason": reason,
            },
        )
        self.exchange.trades[-1]["trigger_price"] = float(trigger_price)
        del self.positions[position_id]
        for child in self.positions.values():
            if child.get("parent") == position_id:
                self.promote_reversal(child)
        self.save()
        log.info("EXIT %s %s [%s]", reason, symbol, position_id)
        return True

    async def manage_exits(self):
        for key in list(self.positions):
            p = self.positions.get(key)
            if p is None:
                continue
            try:
                symbol = p.get("symbol", key)
                price = float((await self.exchange.fetch_ticker(symbol))["last"])
                direction = 1 if p["side"] == "buy" else -1
                move = direction * (price / p["entry"] - 1)
                if direction * (price - p["sl"]) <= 0:
                    await self.close_position(key, p.get("stop_kind", "SL"), price)
                    continue
                for trigger, lock in PROFIT_LOCK_LEVELS:
                    if move + 1e-12 >= trigger and trigger > p.get(
                        "profit_lock_trigger", 0
                    ):
                        self.tighten_stop(
                            p, p["entry"] * (1 + direction * lock), "PROFIT_LOCK"
                        )
                        p.update(profit_locked=lock, profit_lock_trigger=trigger)
                        log.info(
                            "PROFIT LOCK %s [%s] | +%.1f%%", symbol, key, lock * 100
                        )
                if p.get("is_reversal"):
                    await self.check_reversal_success(key, price)
                elif (
                    not p.get("reversed")
                    and -direction * (price / p["original_entry"] - 1) + 1e-12
                    >= REVERSAL_TRIGGER
                ):
                    await self.try_open_reversal(key, price, p)
                self.save()
            except Exception as error:
                log.warning("Position management failed %s: %s", key, error)
                self.save()

    async def try_open_reversal(self, position_id, price, original_pos):
        if original_pos.get("reversed") or original_pos.get("is_reversal"):
            return False
        if self.positions.get(position_id) is not original_pos:
            return False
        if len(self.positions) >= self.strategy.MAX_CONCURRENT_POSITIONS:
            return False
        symbol = original_pos.get("symbol", position_id)
        reverse_side = "sell" if original_pos["side"] == "buy" else "buy"
        sig = await self.detect_cascade(symbol)
        if not sig or not (
            sig.get("long") if reverse_side == "buy" else sig.get("short")
        ):
            return False
        reverse_id = symbol + "_REV_" + uuid4().hex
        try:
            fill = await self.exchange.create_order(
                symbol,
                "market",
                reverse_side,
                1,
                params={
                    "hedge": True,
                    "positionId": reverse_id,
                    "hedgeParent": position_id,
                    "parentStop": original_pos["sl"],
                    "parentEntry": original_pos["original_entry"],
                    "reversalTrigger": REVERSAL_TRIGGER,
                    "quoteNotional": original_pos["original_notional"]
                    * REVERSAL_SIZE_MULT,
                },
            )
            child = self.position(reverse_id, symbol, fill)
            child.update(is_reversal=True, parent=position_id, reverse_of=position_id)
            self.positions[reverse_id] = child
            original_pos.update(
                reversed=True, reverse_side=reverse_side, reverse_position_id=reverse_id
            )
            self.save()
            log.info(
                "REVERSAL %s %s [%s] | @ %.8f",
                reverse_side,
                symbol,
                reverse_id,
                fill["price"],
            )
            return True
        except ReversalStopCrossed as error:
            await self.close_position(
                position_id, original_pos.get("stop_kind", "SL"), error.price
            )
            return False
        except Exception as error:
            log.warning("Reversal failed %s: %s", symbol, error)
            return False

    async def check_reversal_success(self, position_id, price):
        p = self.positions.get(position_id)
        if not p or not p.get("is_reversal"):
            return False
        direction = 1 if p["side"] == "buy" else -1
        if direction * (price / p["entry"] - 1) + 1e-12 < REVERSAL_CONFIRM_PROFIT:
            return False
        parent = p.get("parent")
        if parent in self.positions:
            await self.close_position(parent, "REVERSAL_WIN", price)
        self.promote_reversal(p)
        self.save()
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="paper_state_final4.json")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    path = Path(args.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Reversal bot already running on this state file.\n")
        bot = Final4PaperHunter(path, reset=args.reset)
        try:
            asyncio.run(bot.start())
        except KeyboardInterrupt:
            log.info("Final-4 reversal stopped; state saved")


if __name__ == "__main__":
    main()
