"""Approved V2 account; optional local runner without dashboard publishing."""


import binance_cascade_hunter_dynamic_v2 as v2
from paper_runtime import DynamicPaperHunter


class V2PaperHunter(DynamicPaperHunter):
    def __init__(self, state_path="paper_state_dynamic_v2.json", fee_rate=0.0005,
                 reset=False, market_data=None):
        super().__init__(state_path, fee_rate, reset, market_data, strategy_config=v2)

    def settings(self):
        return {**super().settings(), "mode": "dynamic_v2_continuous",
                "position_basis": "wallet_total", "stop_basis": "signal_price"}

    async def size_position(self, symbol):
        return await v2.CascadeHunter.size_position(self, symbol)

    async def get_top_symbols(self):
        return await v2.CascadeHunter.get_top_symbols(self)

    async def get_orderbook_imbalance(self, symbol, depth=20):
        return await v2.CascadeHunter.get_orderbook_imbalance(self, symbol, depth)

    async def detect_cascade(self, symbol):
        return await v2.CascadeHunter.detect_cascade(self, symbol)

    async def set_leverage(self, symbol):
        return await v2.CascadeHunter.set_leverage(self, symbol)

    async def exit_watcher(self):
        return await v2.CascadeHunter.exit_watcher(self)


if __name__ == "__main__":
    raise SystemExit("Run Final-4 with: python -u cascade_final4_paper.py")
