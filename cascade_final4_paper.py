"""Final-4 paper hedge positions: 2.8% stops, profit locks and reduced reversal size."""

import argparse
import asyncio
import fcntl
import math
import time
from pathlib import Path
from uuid import uuid4

from binance_cascade_hunter_dynamic_v2_paper import V2PaperHunter
from binance_cascade_hunter_dynamic_v2 import log
from risk_controls import RiskControls
from paper_account import ReversalStopCrossed, PartialStopCrossed

HARD_STOP_PCT = 0.028
REVERSAL_TRIGGER = 0.018
REVERSAL_SIZE_MULT = 0.45
REVERSAL_CONFIRM_PROFIT = 0.007
REVERSAL_COOLDOWN_SEC = 600
REVERSAL_SIGNAL_MOVE = 0.006
REVERSAL_SIGNAL_VOLUME = 1.5
REVERSAL_SIGNAL_IMBALANCE = 0.15
PROFIT_LOCK_LEVELS = [
    (0.010, 0.0001),
    (0.015, 0.005),
    (0.020, 0.010),
    (0.025, 0.015),
    (0.030, 0.020),
    (0.035, 0.025),
    (0.040, 0.030),
]


class Final4PaperHunter(RiskControls, V2PaperHunter):
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
        self.last_reversal_time = {}
        self.reversal_stats = {}
        super().__init__(state_path, fee_rate, reset, market_data)
        for key, p in self.positions.items():
            p.setdefault("symbol", self.exchange.positions[key].get("symbol", key))
            p.setdefault("position_id", key)
            p.setdefault("is_reversal", False)
            p.setdefault("was_reversal", p["is_reversal"])
            p.setdefault("profit_lock_trigger", 0.0)

    def settings(self):
        return {
            **super().settings(),
            "mode": "final4_smart_reversal",
            **self.risk_settings(),
            "position_basis": "wallet_total",
            "stop_basis": "original_fill_price",
            "STOP_LOSS_PCT": HARD_STOP_PCT,
            "TAKE_PROFIT_PCT": None,
            "hard_stop_pct": HARD_STOP_PCT,
            "hedge_positions": True,
            "max_ordinary_positions": 2,
            "reversal_trigger": REVERSAL_TRIGGER,
            "reversal_size_mult": REVERSAL_SIZE_MULT,
            "reversal_confirm_profit": REVERSAL_CONFIRM_PROFIT,
            "max_combined_loss_pct": None,
            "reversal_cooldown_sec": REVERSAL_COOLDOWN_SEC,
            "reversal_signal_move": REVERSAL_SIGNAL_MOVE,
            "reversal_signal_volume": REVERSAL_SIGNAL_VOLUME,
            "reversal_signal_imbalance": REVERSAL_SIGNAL_IMBALANCE,
            "original_partial_fraction": 0.50,
            "post_reversal_stop_pct": 0.007,
            "post_reversal_stop_basis": "current_price",
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
            was_reversal=False,
            profit_locked=0.0,
            profit_lock_trigger=0.0,
            stop_kind="SL",
        )

    async def enter(self, symbol, side, price):
        now = time.time()
        if not self.entry_permitted(symbol):
            return False
        if self.has_market_position(symbol) or len(self.positions) >= min(
            2, self.strategy.MAX_CONCURRENT_POSITIONS
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
                params={
                    "quoteNotional": quote,
                    "positionId": symbol,
                    **self.guarded_entry_params(price),
                },
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
                "stopPrice": (
                    p["sl"]
                    if reason
                    in ("SL", "TIGHT_1", "TIGHT_2", "TIGHT_AFTER_REV", "PROFIT_LOCK")
                    else None
                ),
            },
        )
        self.exchange.trades[-1]["trigger_price"] = float(trigger_price)
        if p.get("is_reversal") or p.get("was_reversal"):
            self.last_reversal_time[symbol] = time.time()
        self.last_trade[symbol] = time.time()
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
                        candidate = self.fee_aware_stop(p, lock)
                        if direction * (price - candidate) <= 0:
                            continue
                        self.tighten_stop(p, candidate, "PROFIT_LOCK")
                        p.update(
                            profit_locked=direction * (p["sl"] / p["entry"] - 1),
                            profit_lock_trigger=trigger,
                        )
                        log.info(
                            "PROFIT LOCK %s [%s] | actual=%.3f%% | stop=%.8f",
                            symbol,
                            key,
                            p["profit_locked"] * 100,
                            p["sl"],
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

    async def detect_cascade(self, symbol):
        # Reversal confirmation uses recent accepted candles, not another slow REST candle scan.
        sc = self.scanner
        if sc.execution_quote(symbol) is None:
            return None
        book = await asyncio.wait_for(
            self.exchange.fetch_order_book(symbol, limit=20), 2.0
        )
        if sc.execution_quote(symbol) is None:
            return None
        rows = list(sc.histories.get(symbol, ()))
        if len(rows) < 15:
            return None
        ret = rows[-1][4] / rows[-3][4] - 1
        average = sum(r[5] for r in rows[-15:-3]) / 12
        volume = rows[-1][5] / average if average > 0 else 0
        bid = sum(float(x[1]) for x in book["bids"][:20])
        ask = sum(float(x[1]) for x in book["asks"][:20])
        imb = (bid - ask) / (bid + ask) if bid + ask > 0 else 0
        return {
            "long": ret >= REVERSAL_SIGNAL_MOVE
            and volume >= REVERSAL_SIGNAL_VOLUME
            and imb >= REVERSAL_SIGNAL_IMBALANCE,
            "short": ret <= -REVERSAL_SIGNAL_MOVE
            and volume >= REVERSAL_SIGNAL_VOLUME
            and imb <= -REVERSAL_SIGNAL_IMBALANCE,
            "ret": ret,
            "volume": volume,
            "imbalance": imb,
        }

    def reject_reversal(self, symbol, reason):
        self.reversal_stats[reason] = self.reversal_stats.get(reason, 0) + 1
        self.strategy.log.info("REVERSAL REJECTED %s | %s", symbol, reason)
        self.save()
        return False

    async def try_open_reversal(self, position_id, price, original_pos):
        self.reversal_stats["attempts"] = self.reversal_stats.get("attempts", 0) + 1
        symbol = original_pos.get("symbol", position_id)
        if original_pos.get("reversed") or original_pos.get("is_reversal"):
            return self.reject_reversal(symbol, "already_reversed")
        if self.positions.get(position_id) is not original_pos:
            return self.reject_reversal(symbol, "parent_missing")
        if len(self.positions) >= self.strategy.MAX_CONCURRENT_POSITIONS:
            return self.reject_reversal(symbol, "capacity")
        symbol = original_pos.get("symbol", position_id)
        if (
            time.time() - self.last_reversal_time.get(symbol, 0)
            <= REVERSAL_COOLDOWN_SEC
        ):
            return self.reject_reversal(symbol, "cooldown")
        reverse_side = "sell" if original_pos["side"] == "buy" else "buy"
        self.strategy.log.info("REVERSAL CHECK %s | price=%.8f", symbol, price)
        try:
            sig = await self.detect_cascade(symbol)
        except Exception as error:
            log.warning("Reversal data check failed %s: %s", symbol, error)
            return self.reject_reversal(symbol, "signal_data_error")
        if not sig or not (
            sig.get("long") if reverse_side == "buy" else sig.get("short")
        ):
            self.reversal_stats["signal_rejected"] = (
                self.reversal_stats.get("signal_rejected", 0) + 1
            )
            self.strategy.log.info(
                "REVERSAL REJECTED %s | opposite signal missing | metrics=%s",
                symbol,
                sig,
            )
            self.save()
            return False
        reverse_id = symbol + "_REV_" + uuid4().hex
        try:
            fill = await self.exchange.create_order(
                symbol,
                "market",
                reverse_side,
                1,
                params={
                    **self.guarded_entry_params(price),
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
            child.update(
                is_reversal=True,
                was_reversal=True,
                parent=position_id,
                reverse_of=position_id,
            )
            self.positions[reverse_id] = child
            original_pos.update(
                reversed=True, reverse_side=reverse_side, reverse_position_id=reverse_id
            )
            self.save()
            price = float(fill["price"])
            self.reversal_stats["opened"] = self.reversal_stats.get("opened", 0) + 1
            # تخفيف الأصلية فور نجاح المعاكسة، ثم شد وقف المتبقي.
            await self.partial_close_original(
                position_id, 0.50, price, "REVERSAL_HEDGE"
            )
            original_pos = self.positions.get(position_id)
            if original_pos:
                direction = 1 if original_pos["side"] == "buy" else -1
                tight_sl = price * (1 - direction * 0.007)
                self.tighten_stop(original_pos, tight_sl, "TIGHT_AFTER_REV")
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
            self.reversal_stats["execution_rejected"] = (
                self.reversal_stats.get("execution_rejected", 0) + 1
            )
            self.save()
            log.warning("Reversal failed %s: %s", symbol, error)
            return False

    async def partial_close_original(self, position_id, fraction, price, reason):
        p = self.positions.get(position_id)
        if not p:
            return False
        amount = float(
            self.exchange.amount_to_precision(p["symbol"], p["amount"] * fraction)
        )
        if amount <= 0 or amount >= p["amount"] * 0.95:
            return False
        try:
            side = "sell" if p["side"] == "buy" else "buy"
            await self.exchange.create_order(
                p["symbol"],
                "market",
                side,
                amount,
                params={
                    "reduceOnly": True,
                    "positionId": position_id,
                    "exit_reason": reason,
                    "partialStop": p["sl"],
                },
            )
            if position_id in self.exchange.positions:
                p["amount"] = self.exchange.positions[position_id]["amount"]
            else:
                if position_id in self.positions:
                    del self.positions[position_id]
            log.info(
                "%s %s | closed %.0f%% of original", reason, p["symbol"], fraction * 100
            )
            self.save()
            return True
        except PartialStopCrossed as error:
            await self.close_position(
                position_id, p.get("stop_kind", "SL"), error.price
            )
            return True
        except Exception as e:
            log.warning("Partial close original failed: %s", e)
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
