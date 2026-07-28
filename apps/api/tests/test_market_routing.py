"""Маршрут сделки: рынок определяется стороной, а не общей настройкой.

Лонг мы держим монетой на споте, шорт возможен только контрактом. Значит одна
пара торгуется на двух площадках с разной комиссией (0.2% против 0.05%), разным
символом ccxt (BTC/USDT против BTC/USDT:USDT) и разными единицами объёма.

Пока рынок был один на всю систему, половина сделок считалась по чужой цене
исполнения, а в live ордер по спотовому символу с market_type="swap" уходил бы
на спот: ccxt резолвит рынок ПО СИМВОЛУ, params.defaultType на это не влияет.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import market_routing


@pytest.fixture
def futures_on(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FUTURES", True)
    monkeypatch.setattr(settings, "ENABLE_FUTURES_EXECUTION", False)
    monkeypatch.setattr(settings, "FUTURES_LEVERAGE", 1)


def test_short_goes_to_the_derivative(futures_on):
    route = market_routing.resolve("BTC/USDT", "short")

    assert route.market_type == "swap"
    assert route.exchange_symbol == "BTC/USDT:USDT"
    assert route.base_symbol == "BTC/USDT"


def test_long_goes_to_spot_by_default(futures_on):
    route = market_routing.resolve("BTC/USDT", "long")

    assert route.market_type == "spot"
    assert route.exchange_symbol == "BTC/USDT"
    assert route.leverage == 1
    assert route.margin_mode is None


def test_long_follows_config_to_the_derivative(monkeypatch, futures_on):
    monkeypatch.setattr(settings, "ENABLE_FUTURES_EXECUTION", True)

    route = market_routing.resolve("BTC/USDT", "long")

    assert route.market_type == "swap"
    assert route.exchange_symbol == "BTC/USDT:USDT"


def test_short_without_futures_is_impossible(monkeypatch):
    """Продать на споте то, чего нет, нельзя — отказ должен быть явным."""
    monkeypatch.setattr(settings, "ENABLE_FUTURES", False)

    with pytest.raises(ValueError, match="short_requires_futures"):
        market_routing.resolve("BTC/USDT", "short")


def test_contract_symbol_is_not_doubled(futures_on):
    route = market_routing.resolve("BTC/USDT:USDT", "short")

    assert route.exchange_symbol == "BTC/USDT:USDT"
    assert route.base_symbol == "BTC/USDT"


def test_open_trade_keeps_its_market_when_config_changes(monkeypatch, futures_on):
    """Сделка закрывается там же, где открылась.

    Смена настроек на лету не должна переоценивать открытую позицию по чужой
    комиссии и уводить выход на другую площадку.
    """
    opened_on_spot = {"routing": market_routing.resolve("BTC/USDT", "long").as_dict()}
    assert opened_on_spot["routing"]["market_type"] == "spot"

    monkeypatch.setattr(settings, "ENABLE_FUTURES_EXECUTION", True)

    restored = market_routing.from_payload(opened_on_spot, "BTC/USDT", "long")

    assert restored.market_type == "spot"
    assert restored.exchange_symbol == "BTC/USDT"


def test_legacy_signal_without_routing_is_restored_from_side(futures_on):
    """Сигналы, созданные до появления маршрута, тоже должны сопровождаться."""
    restored = market_routing.from_payload({"qty": 1.0}, "BTC/USDT", "short")

    assert restored.market_type == "swap"
    assert restored.exchange_symbol == "BTC/USDT:USDT"


def test_plan_costs_follow_the_trade_market(monkeypatch, futures_on):
    """Экономика плана обязана считаться по комиссии своего рынка.

    Это и есть цена ошибки: спотовый round-trip 0.4% против свопового 0.1%.
    На типичном ходе 0.5–2% разница решает, есть у сделки edge или нет.
    """
    from services.cost_engine import CostEngine

    engine = CostEngine()
    monkeypatch.setattr(settings, "SPOT_TAKER_FEE", 0.002)
    monkeypatch.setattr(settings, "FUTURES_TAKER_FEE", 0.0005)
    monkeypatch.setattr(engine.htx, "trading_fee_rates", lambda *_a, **_k: {})

    spot = engine.estimate(
        symbol="BTC/USDT", market_type="spot", side="long",
        entry_price=100.0, exit_price=101.0, qty=1.0,
        liquidity="taker", holding_funding_periods=0, leverage=1,
    )
    swap = engine.estimate(
        symbol="BTC/USDT", market_type="swap", side="short",
        entry_price=100.0, exit_price=99.0, qty=1.0,
        liquidity="taker", holding_funding_periods=1, leverage=1,
    )

    assert spot.fee_rate == pytest.approx(0.002)
    assert swap.fee_rate == pytest.approx(0.0005)
    assert spot.total_cost > swap.total_cost
