from core.config import settings

class PortfolioEngine:
    def position_size(self, balance_usdt: float, entry_price: float, stop_price: float, multiplier: float = 1.0) -> float:
        risk_capital = balance_usdt * (settings.RISK_PER_TRADE_PCT / 100.0)
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return 0.0

        qty = (risk_capital / stop_distance) * multiplier
        qty_in_asset = qty / entry_price
        return max(qty_in_asset, 0.0)