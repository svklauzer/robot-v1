"""Регресс-тест на инцидент (#okx-satellite-exchange-routing-2026-09-02):

Живая позиция BTC/USDT, открытая на HTX, начала вестись клиентом ТЕКУЩЕЙ
активной биржи (OKX) сразу после ручного переключения ACTIVE_EXCHANGE — цена,
комиссии и само закрытие ушли бы не на ту биржу. Причина: SignalLifecycleManager
держал общие self.market/self.exit_policy, которые резолвили клиента один раз
и не знали, где каждый конкретный сигнал реально открыт.

Фикс: Signal.exchange фиксируется при создании и не меняется; process_signal/
_process_signal_core конструируют MarketDataService/ExitPolicyService ПОД
БИРЖУ КОНКРЕТНОГО СИГНАЛА (signal.exchange), а не под текущий ACTIVE_EXCHANGE.
Этот файл проверяет именно это — воспроизводя ровно сценарий инцидента: сигнал
с exchange="htx" при settings.ACTIVE_EXCHANGE="okx".
"""
from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import services.signal_lifecycle as signal_lifecycle
from core.config import settings
from core.db import Base
from models.bot import Bot
from models.position import Position
from models.signal import Signal
from models.user import User
from services.cost_engine import CostEngine
from services.execution_engine import ExecutionEngine
from services.exit_policy import ExitPolicyService
from services.htx_client import HTXClient
from services.market_data import MarketDataService
from services.okx_client import OKXClient
from services.signal_lifecycle import SignalLifecycleManager
from services.trade_plan import TradePlanBuilder


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__,
        Bot.__table__,
        Signal.__table__,
        Position.__table__,
    ])
    Session = sessionmaker(bind=engine)
    return Session()


class _RecordingMarketDataService:
    """Заглушка вместо реального MarketDataService — без сети, только фиксирует,
    под какую биржу её сконструировали."""

    seen_exchanges: list = []

    def __init__(self, exchange: str | None = None):
        _RecordingMarketDataService.seen_exchanges.append(exchange)

    def ticker_snapshot(self, symbol: str) -> dict:
        # 101.5 — вне зоны входа [100, 101], но в пределах guard-буфера
        # (base_min=99 stop, base_max=104 tp2 -> buffer=6 -> [93, 110]).
        # Это держит process_signal на "published + вне зоны входа" — ветке,
        # где ExecutionEngine НЕ конструируется, и тест остаётся сфокусирован
        # ровно на факте резолва клиента под нужную биржу.
        return {"symbol": symbol, "last": 101.5, "bid": 101.4, "ask": 101.6, "source": "test"}


class _RecordingExitPolicyService:
    seen_exchanges: list = []

    def __init__(self, exchange: str | None = None):
        _RecordingExitPolicyService.seen_exchanges.append(exchange)


def test_open_htx_signal_is_routed_through_htx_even_when_okx_is_active(monkeypatch):
    """Ровно сценарий инцидента: сигнал открыт на HTX, ACTIVE_EXCHANGE=okx."""
    _RecordingMarketDataService.seen_exchanges = []
    _RecordingExitPolicyService.seen_exchanges = []
    monkeypatch.setattr(signal_lifecycle, "MarketDataService", _RecordingMarketDataService)
    monkeypatch.setattr(signal_lifecycle, "ExitPolicyService", _RecordingExitPolicyService)
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)

    db = _db_session()
    try:
        user = User(email="owner@example.com", password_hash="hash")
        db.add(user)
        db.flush()
        bot = Bot(user_id=user.id, name="Main Robot", status="running", mode="paper", config_json={})
        db.add(bot)
        db.flush()

        signal = Signal(
            bot_id=bot.id,
            symbol="BTC/USDT",
            side="long",
            exchange="htx",
            status="published",
            entry_zone_json={"from": 100.0, "to": 101.0},
            stop_price=99.0,
            tp_json={"tp1": 102.0, "tp2": 104.0},
            confidence=80.0,
            rationale="test",
            grade="A",
            is_public=False,
            plan_json={},
        )
        db.add(signal)
        db.flush()

        manager = SignalLifecycleManager()
        asyncio.run(manager.process_signal(db, bot, signal))

        assert _RecordingMarketDataService.seen_exchanges == ["htx"], (
            "MarketDataService сконструирован не под биржу сигнала — "
            f"{_RecordingMarketDataService.seen_exchanges}"
        )
        assert _RecordingExitPolicyService.seen_exchanges == ["htx"], (
            "ExitPolicyService сконструирован не под биржу сигнала — "
            f"{_RecordingExitPolicyService.seen_exchanges}"
        )
    finally:
        db.close()


def test_manager_no_longer_holds_a_shared_market_or_exit_policy_client():
    """self.market/self.exit_policy общими на все сигналы больше не существуют —
    иначе они снова "залипнут" на клиенте активной на момент старта биржи."""
    manager = SignalLifecycleManager()
    assert not hasattr(manager, "market")
    assert not hasattr(manager, "exit_policy")


def test_signal_created_without_explicit_exchange_defaults_to_htx():
    """server_default="htx" обязан сработать и для старых записей (все реально
    были открыты на HTX — OKX не существовал до этой миграции), и для любого
    нового кода, который забудет проставить exchange явно."""
    db = _db_session()
    try:
        user = User(email="owner2@example.com", password_hash="hash")
        db.add(user)
        db.flush()
        bot = Bot(user_id=user.id, name="Main Robot", status="running", mode="paper", config_json={})
        db.add(bot)
        db.flush()

        signal = Signal(
            bot_id=bot.id,
            symbol="ETH/USDT",
            side="long",
            status="published",
            entry_zone_json={"from": 100.0, "to": 101.0},
            stop_price=99.0,
            tp_json={"tp1": 102.0, "tp2": 104.0},
            confidence=80.0,
            rationale="test",
            grade="A",
            is_public=False,
            plan_json={},
        )
        db.add(signal)
        db.flush()
        db.refresh(signal)

        assert signal.exchange == "htx"
    finally:
        db.close()


# ── конструкторы: явный exchange резолвит нужного клиента ───────────────────

def test_cost_engine_constructor_respects_explicit_exchange(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    assert isinstance(CostEngine(exchange="htx").htx, HTXClient)


def test_trade_plan_builder_constructor_respects_explicit_exchange(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "htx", raising=False)
    builder = TradePlanBuilder(exchange="okx")
    assert isinstance(builder.htx, OKXClient)
    assert isinstance(builder.cost_engine.htx, OKXClient)


def test_execution_engine_constructor_respects_explicit_exchange(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    engine = ExecutionEngine(None, exchange="htx")
    assert isinstance(engine.client, HTXClient)
    assert isinstance(engine.cost_engine.htx, HTXClient)
    assert isinstance(engine.plan_builder.htx, HTXClient)


def test_exit_policy_service_constructor_respects_explicit_exchange(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "htx", raising=False)
    assert isinstance(ExitPolicyService(exchange="okx").htx, OKXClient)


def test_market_data_service_constructor_respects_explicit_exchange(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "htx", raising=False)
    assert isinstance(MarketDataService(exchange="okx").client, OKXClient)


def test_all_five_constructors_default_to_active_exchange_when_no_override(monkeypatch):
    """exchange=None (обычные вызовы для НОВЫХ сигналов) — поведение как раньше."""
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    assert isinstance(CostEngine().htx, OKXClient)
    assert isinstance(TradePlanBuilder().htx, OKXClient)
    assert isinstance(ExecutionEngine(None).client, OKXClient)
    assert isinstance(ExitPolicyService().htx, OKXClient)
    assert isinstance(MarketDataService().client, OKXClient)
