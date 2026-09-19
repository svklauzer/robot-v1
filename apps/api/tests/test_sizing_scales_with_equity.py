"""Размер позиции масштабируется со счётом и плечом (#sizing-scales-with-equity-2026-09-19).

Требование владельца: «любое плечо, любой депозит — система распределяет
средства автоматически и не привязывается к сумме депозита; всё, что на торговом
счету, то она и использует, с ростом позиций».

Потолок нотионала одного ордера был абсолютным (250 USDT). На счёте 300 с плечом
5 он резал позицию до шестой части доступной экспозиции, а на счёте 30 000
держал систему на том же размере, что и на 300. Доля от экспозиции решает обе
задачи одной настройкой.
"""
from __future__ import annotations

import inspect

import pytest

from core.config import settings


@pytest.fixture
def pct(monkeypatch):
    def _set(value: float):
        monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_PCT", value)
    return _set


def test_absolute_cap_stays_until_the_share_is_switched_on(pct):
    """Ноль по умолчанию: включение меняет размер позиций, то есть эпоху."""
    pct(0.0)
    assert settings.max_order_notional(3000, 1) == settings.LIVE_MAX_ORDER_NOTIONAL_USDT
    assert settings.max_order_notional(300, 5) == settings.LIVE_MAX_ORDER_NOTIONAL_USDT


def test_share_of_exposure_replaces_the_absolute_cap(pct):
    pct(20.0)
    # 300 USDT × 5 = 1500 экспозиции, пятая часть на ордер.
    assert settings.max_order_notional(300, 5) == pytest.approx(300.0)


def test_cap_grows_with_the_deposit_and_with_the_leverage(pct):
    pct(20.0)
    assert settings.max_order_notional(600, 5) == pytest.approx(600.0)   # вдвое больше счёт
    assert settings.max_order_notional(300, 10) == pytest.approx(600.0)  # вдвое больше плечо
    assert settings.max_order_notional(30000, 5) == pytest.approx(30000.0)


def test_unknown_equity_falls_back_to_the_absolute_cap(pct):
    """В live баланс мог не прочитаться. Предохранитель обязан работать и там,
    где размер счёта в этот момент неизвестен."""
    pct(20.0)
    assert settings.max_order_notional(None, 5) == settings.LIVE_MAX_ORDER_NOTIONAL_USDT
    assert settings.max_order_notional(0, 5) == settings.LIVE_MAX_ORDER_NOTIONAL_USDT


def test_leverage_below_one_never_shrinks_the_cap(pct):
    pct(20.0)
    assert settings.max_order_notional(1000, 0) == pytest.approx(200.0)
    assert settings.max_order_notional(1000, None) == pytest.approx(200.0)


def test_sizing_and_submission_use_the_same_formula():
    """Расхождение между сайзингом и отправкой уже стоило отклонённых ордеров в
    live при полном плане в бумаге (#live-notional-parity-2026-08-04)."""
    from services import live_executor, trade_plan

    assert "settings.max_order_notional(balance_usdt, leverage_value)" in inspect.getsource(trade_plan)
    assert "settings.max_order_notional(" in inspect.getsource(live_executor.LiveExecutor.place_market)


def test_submission_asks_for_the_balance_only_when_the_share_is_on():
    """Иначе на каждый ордер уходил бы лишний запрос баланса."""
    source = inspect.getsource(__import__("services.live_executor", fromlist=["x"]).LiveExecutor.place_market)
    head = source.split("cap = float(settings.max_order_notional", 1)[0]
    assert 'LIVE_MAX_ORDER_NOTIONAL_PCT' in head, "капитал спрашивается без проверки доли"


def test_preflight_reports_the_effective_cap():
    from services import live_preflight

    source = inspect.getsource(live_preflight.LivePreflight._check_capital)
    assert "settings.max_order_notional(capital, leverage)" in source
    assert "order_cap_scales_with_equity" in source


def test_blueprint_carries_the_setting():
    """Торговая настройка живёт в config.py и в render.yaml одновременно."""
    from pathlib import Path

    blueprint = (Path(__file__).resolve().parents[3] / "render.yaml").read_text(encoding="utf-8")
    block = blueprint.split("key: LIVE_MAX_ORDER_NOTIONAL_PCT", 1)[1].split("- key:", 1)[0]
    assert 'value: "0"' in block
