"""Лимитный вход в сопровождении сделки (#limit-entry-2026-09-19).

Рыночный вход срабатывает от касания коридора зоны и книжится по текущей цене.
Лимитный ждёт, пока цена дойдёт до самой цели, и книжится по ней. Разница — те
самые 0.06–0.10%, которые сейчас теряются на дальней границе коридора
(#entry-drift-2026-09-19), и она же причина, по которой часть сигналов лимитом
не исполнится вовсе.

Бумага обязана отказывать ровно там, где откажет биржа: иначе мы поменяли бы
одно расхождение с реальностью на другое.
"""
from __future__ import annotations

import inspect

import pytest

from core.config import settings
from services.entry_zone import limit_target_price
from services.signal_lifecycle import SignalLifecycleManager


class _Signal:
    def __init__(self, side="long", mode="limit_wall", target=1.4097, ttl=45.0):
        self.side = side
        self.plan_json = {"entry_zone_plan": {"mode": mode, "entry_price": target, "ttl_sec": ttl}}


def _trigger(signal, price, zone=(1.4082903, 1.4111097)):
    manager = SignalLifecycleManager.__new__(SignalLifecycleManager)
    return manager.entry_trigger(signal, price, zone[0], zone[1])


@pytest.fixture
def limit_mode(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_ORDER_TYPE", "limit")


@pytest.fixture
def market_mode(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_ORDER_TYPE", "market")


def test_market_entry_fires_on_the_corridor_and_books_the_current_price(market_mode):
    """Прежнее поведение: рыночный ордер берёт то, что есть."""
    ready, fill = _trigger(_Signal(), price=1.4110)

    assert ready is True and fill == 1.4110


def test_limit_entry_ignores_the_far_edge_of_the_corridor(limit_mode):
    """Лонг падает к коридору сверху. Касание верхней границы — ещё не цель, и
    лимитный ордер там не исполнился бы."""
    ready, _ = _trigger(_Signal(side="long", target=1.4097), price=1.4110)

    assert ready is False


def test_limit_entry_fires_at_the_target_and_books_it(limit_mode):
    ready, fill = _trigger(_Signal(side="long", target=1.4097), price=1.4097)

    assert ready is True and fill == 1.4097


def test_limit_entry_books_the_target_even_if_price_overshot(limit_mode):
    """Цена ушла ниже цели — лимит исполнился по СВОЕЙ цене, не лучше."""
    ready, fill = _trigger(_Signal(side="long", target=1.4097), price=1.4050)

    assert ready is True and fill == 1.4097


def test_short_limit_waits_for_the_price_to_rise(limit_mode):
    signal = _Signal(side="short", target=1.4110)

    assert _trigger(signal, price=1.4097)[0] is False
    ready, fill = _trigger(signal, price=1.4115)
    assert ready is True and fill == 1.4110


def test_market_zone_has_no_target_to_wait_for(limit_mode):
    """Зона не переносила вход — ждать нечего, работаем как раньше."""
    signal = _Signal(mode="market", target=None)

    assert limit_target_price(signal.plan_json) is None
    assert _trigger(signal, price=1.4110) == (True, 1.4110)


def test_setting_off_disables_the_limit_even_with_a_target(market_mode):
    assert limit_target_price(_Signal().plan_json) is None


# ── издержки ────────────────────────────────────────────────────────────────
def test_limit_entry_pays_the_maker_fee_and_no_entry_slippage():
    """Лимитный вход не забирает ликвидность: мейкерская ставка, и спред на
    входе не платится — исполнение либо по своей цене, либо никак."""
    from services.cost_engine import CostEngine

    engine = CostEngine.__new__(CostEngine)
    engine.htx = type("X", (), {"trading_fee_rates": staticmethod(lambda **k: {})})()
    engine.venue = "okx"

    taker = engine.estimate(symbol="XRP/USDT", market_type="swap", side="long",
                            entry_price=1.0, exit_price=1.01, qty=100.0, liquidity="taker")
    maker = engine.estimate(symbol="XRP/USDT", market_type="swap", side="long",
                            entry_price=1.0, exit_price=1.01, qty=100.0,
                            liquidity="taker", entry_liquidity="maker")

    assert maker.entry_fee < taker.entry_fee
    assert maker.exit_fee == taker.exit_fee            # выход остаётся рыночным
    assert maker.slippage_buffer < taker.slippage_buffer
    assert maker.total_cost < taker.total_cost


def test_closed_trade_is_costed_by_how_it_actually_opened():
    """Настройка может перевернуться между открытием и закрытием. Издержки
    закрытой сделки обязаны отражать то, как она открывалась на самом деле."""
    source = inspect.getsource(__import__("services.execution_engine", fromlist=["x"]))

    assert '"entry_liquidity": entry_liquidity' in source
    assert 'get("entry_liquidity")' in source


def test_live_entry_goes_through_the_limit_order():
    from services.execution_engine import ExecutionEngine

    source = inspect.getsource(ExecutionEngine._submit_live)
    assert "LIVE_EXECUTOR.place_limit(" in source
    # Выход обязан исполниться, а не ждать своей цены.
    assert "if limit_price and not reduce_only:" in source


def test_unfilled_limit_is_named_in_the_expiry_reason():
    """Без отдельной причины долю неисполнений лимитом не отличить от обычного
    протухания сигнала."""
    source = inspect.getsource(SignalLifecycleManager.expire_stale_signals)

    assert "limit_not_filled" in source


def test_blueprint_carries_the_entry_order_type():
    from pathlib import Path

    blueprint = (Path(__file__).resolve().parents[3] / "render.yaml").read_text(encoding="utf-8")
    block = blueprint.split("key: ENTRY_ORDER_TYPE", 1)[1].split("- key:", 1)[0]
    assert 'value: "market"' in block
