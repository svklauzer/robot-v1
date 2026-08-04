from types import SimpleNamespace

from services.trade_plan import TradePlanBuilder
from core.config import settings


class DummyHTX:
    def price_to_precision(self, symbol, value):
        return value

    def amount_to_precision(self, symbol, value):
        # ccxt по умолчанию УСЕКАЕТ объём вниз (TRUNCATE), а не округляет —
        # мок должен вести себя так же, иначе объём «подрастает» на шаг и
        # ломает инвариант кэпа нотионала.
        import math
        return math.floor(float(value) * 1e6) / 1e6

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
    old_notional = settings.LIVE_MAX_ORDER_NOTIONAL_USDT
    try:
        settings.MAX_POSITION_MARGIN_PCT = 0.35
        settings.ENABLE_FUTURES = False
        settings.EXECUTION_MARKET = "spot"
        # Тест проверяет марж-долю, а не кэп нотионала — отключаем кэп, иначе он
        # доминирует (25 USDT дал бы qty 0.25).
        settings.LIVE_MAX_ORDER_NOTIONAL_USDT = 0.0

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
        settings.LIVE_MAX_ORDER_NOTIONAL_USDT = old_notional


def test_trade_plan_notional_never_exceeds_live_cap_any_coin():
    """Инвариант: план не рождает нотионал выше LIVE_MAX_ORDER_NOTIONAL_USDT —
    ни для дорогой монеты, ни для дешёвой, ни при тесном стопе. Это то, что
    держит бумагу и live на одном размере (иначе live отклонил бы ордер кэпом)."""
    builder = TradePlanBuilder()
    builder.htx = DummyHTX()
    builder.cost_engine = DummyCostEngine()

    old_market = settings.EXECUTION_MARKET
    old_fut = settings.ENABLE_FUTURES
    old_notional = settings.LIVE_MAX_ORDER_NOTIONAL_USDT
    try:
        settings.EXECUTION_MARKET = "spot"
        settings.ENABLE_FUTURES = False
        settings.LIVE_MAX_ORDER_NOTIONAL_USDT = 25.0

        # Дорогая монета (BTC ~65000), дешёвая (ADA ~0.65), разные стопы.
        cases = [
            ("BTC/USDT", 65000.0, 64000.0, 66000.0, 67000.0),
            ("ADA/USDT", 0.65, 0.6435, 0.66, 0.67),   # тесный стоп 0.3% — раздувал размер
            ("ETH/USDT", 3200.0, 3168.0, 3232.0, 3264.0),
        ]
        for sym, entry, stop, tp1, tp2 in cases:
            plan = builder.build_plan(
                symbol=sym, side="long", entry_price=entry, stop_price=stop,
                tp1=tp1, tp2=tp2, balance_usdt=950.0, risk_pct=0.4,
            )
            if plan.is_valid:
                assert plan.entry_notional <= 25.0 + 1e-6, (
                    f"{sym}: нотионал {plan.entry_notional} > кэп 25 — бумага разойдётся с live"
                )
    finally:
        settings.EXECUTION_MARKET = old_market
        settings.ENABLE_FUTURES = old_fut
        settings.LIVE_MAX_ORDER_NOTIONAL_USDT = old_notional


def test_trade_plan_rejects_low_expected_net_pnl_targets():
    builder = TradePlanBuilder()
    builder.htx = DummyHTX()
    builder.cost_engine = DummyCostEngine()

    old_min_tp1 = settings.MIN_NET_PNL_TP1_USDT
    old_min_tp2 = settings.MIN_NET_PNL_TP2_USDT
    old_cap = settings.MAX_POSITION_MARGIN_PCT
    try:
        settings.MIN_NET_PNL_TP1_USDT = 5.0
        settings.MIN_NET_PNL_TP2_USDT = 8.0
        settings.MAX_POSITION_MARGIN_PCT = 0.35

        plan = builder.build_plan(
            symbol="BTC/USDT",
            side="long",
            entry_price=100.0,
            stop_price=99.0,
            tp1=100.8,
            tp2=101.4,
            balance_usdt=1000.0,
            risk_pct=1.0,
        )

        assert plan.is_valid is False
        assert plan.reject_reason in {
            "tp1_net_pnl_below_min_usdt",
            "tp2_net_pnl_below_min_usdt",
        }
    finally:
        settings.MIN_NET_PNL_TP1_USDT = old_min_tp1
        settings.MIN_NET_PNL_TP2_USDT = old_min_tp2
        settings.MAX_POSITION_MARGIN_PCT = old_cap
