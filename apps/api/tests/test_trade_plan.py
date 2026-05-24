from types import SimpleNamespace

from services.trade_plan import TradePlanBuilder
from core.config import settings


class DummyHTX:
    def price_to_precision(self, symbol, value):
        return value

    def amount_to_precision(self, symbol, value):
        return round(value, 6)

    def market_limits(self, symbol):
        return {"min_amount": None, "min_cost": None}


class DummyCostEngine:
    def estimate(self, symbol, market_type, side, entry_price, exit_price, qty, liquidity, leverage):
        side_value = str(side).lower()
        if side_value in ["long", "buy"]:
            gross = (exit_price - entry_price) * qty
        else:
            gross = (entry_price - exit_price) * qty
        return SimpleNamespace(net_pnl=gross)


def test_trade_plan_limits_single_position_margin_share():
    builder = TradePlanBuilder()
    builder.htx = DummyHTX()
    builder.cost_engine = DummyCostEngine()

    old_cap = settings.MAX_POSITION_MARGIN_PCT
    old_fut = settings.ENABLE_FUTURES
    old_market = settings.EXECUTION_MARKET
    try:
        settings.MAX_POSITION_MARGIN_PCT = 0.35
        settings.ENABLE_FUTURES = False
        settings.EXECUTION_MARKET = "spot"

        plan = builder.build_plan(
            symbol="BTC/USDT",
            side="long",
            entry_price=100.0,
            stop_price=99.0,
            tp1=101.0,
            tp2=102.0,
            balance_usdt=1000.0,
            risk_pct=2.0,
        )

        assert plan.is_valid is True
        assert plan.required_margin <= 350.0
        assert round(plan.qty, 6) == 3.5
    finally:
        settings.MAX_POSITION_MARGIN_PCT = old_cap
        settings.ENABLE_FUTURES = old_fut
        settings.EXECUTION_MARKET = old_market
