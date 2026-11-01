"""Conservative paper sizing, entry guards and fee-aware profit protection."""

RISK_PROFILE = dict(
    risk_engine_version=2,
    max_entry_deviation_pct=0.003,
    max_balance_drawdown_pct=0.06,
    execution_max_age_sec=2,
    exit_check_interval_sec=2,
    profit_fee_buffer_pct=0.001,
)


class RiskControls:
    def risk_settings(self):
        return dict(RISK_PROFILE)

    def entry_permitted(self, symbol):
        memory = self.risk_memory
        peak = max(memory.get("peak_balance", 100.0), self.exchange.balance)
        memory["peak_balance"] = peak
        blocked = self.exchange.balance <= peak * (
            1 - RISK_PROFILE["max_balance_drawdown_pct"]
        )
        memory["entries_paused"] = blocked
        if blocked:
            self.strategy.log.warning(
                "ENTRY BLOCKED %s | balance drawdown >=6%%; exits remain active", symbol
            )
            self.save()
        return not blocked

    def risk_paused(self):
        return self.risk_memory.get(
            "entries_paused", False
        ) and self.exchange.balance <= self.risk_memory.get("peak_balance", 100.0) * (
            1 - RISK_PROFILE["max_balance_drawdown_pct"]
        )

    def guarded_entry_params(self, reference):
        return {
            "referencePrice": float(reference),
            "maxDeviation": RISK_PROFILE["max_entry_deviation_pct"],
        }

    def fee_aware_stop(self, p, lock):
        key = p.get("position_id", p.get("symbol"))
        fill = self.exchange.positions.get(key, p)
        amount, entry, fee = p["amount"], p["entry"], self.exchange.fee_rate
        paid = fill.get("fees_paid", entry * amount * fee)
        realized = fill.get("realized_pnl", 0.0)
        buffer = entry * RISK_PROFILE["profit_fee_buffer_pct"]
        if p["side"] == "buy":
            break_even = (entry * amount + paid - realized) / (
                amount * (1 - fee)
            ) + buffer
            return max(entry * (1 + lock), break_even)
        break_even = (entry * amount - paid + realized) / (amount * (1 + fee)) - buffer
        return min(entry * (1 - lock), break_even)
