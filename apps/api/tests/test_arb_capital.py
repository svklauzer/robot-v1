"""Нотионал арбитража считается от капитала (#arb-capital-2026-08-03).

Оба движка брали абсолютную константу 100 при любом депозите: на счёте 200 это
половина денег, на счёте 20 000 — простой. Направленные движки давно сайзятся
долей эквити от `effective_equity_usdt()`; арбитражи из схемы выпали.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import arb_capital


# ── доля вместо константы ───────────────────────────────────────────────────
def test_notional_scales_with_equity():
    small = arb_capital.funding_arb_notional(equity=500.0)
    large = arb_capital.funding_arb_notional(equity=2000.0)
    assert large == pytest.approx(small * 4, rel=1e-6)


def test_current_paper_equity_reproduces_old_constant():
    """Дефолт подобран так, чтобы при 950 размер совпал с прежними ~100.

    Механизм меняется, поведение — нет: поднимать долю нужно осознанно,
    а не получить скачок размера вместе с рефакторингом.
    """
    assert arb_capital.funding_arb_notional(equity=950.0) == pytest.approx(99.75, abs=0.5)
    assert arb_capital.cross_farb_notional(equity=950.0) == pytest.approx(99.75, abs=0.5)


# ── границы ─────────────────────────────────────────────────────────────────
def test_tiny_equity_clamps_to_exchange_minimum():
    """Эквити ВЫВОДИМ из настроек, а не зашиваем.

    Было `equity=50` при минимуме 20: доля 10.5% давала 5.25 → клэмп срабатывал.
    После снижения минимума до 5 те же 5.25 стали ВЫШЕ него, и тест падал, хотя
    клэмп цел. Берём заведомо малое эквити относительно текущих настроек —
    проверяем механизм, а не совпадение констант.
    """
    share = float(getattr(settings, "FUNDING_ARB_NOTIONAL_PCT", 0.105)) or 0.105
    floor = float(settings.FUNDING_ARB_MIN_NOTIONAL_USDT)
    tiny_equity = floor / share / 2.0      # доля от него заведомо ниже пола
    assert arb_capital.funding_arb_notional(equity=tiny_equity) == pytest.approx(floor)


def test_huge_equity_clamps_to_ceiling():
    assert arb_capital.funding_arb_notional(equity=10 ** 6) == pytest.approx(
        float(settings.FUNDING_ARB_MAX_NOTIONAL_USDT)
    )


def test_zero_share_still_respects_minimum(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_ARB_NOTIONAL_PCT", 0.0, raising=False)
    assert arb_capital.funding_arb_notional(equity=950.0) == pytest.approx(
        float(settings.FUNDING_ARB_MIN_NOTIONAL_USDT)
    )


def test_negative_share_is_treated_as_zero(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_ARB_NOTIONAL_PCT", -0.5, raising=False)
    assert arb_capital.funding_arb_notional(equity=950.0) > 0


# ── чтение капитала ─────────────────────────────────────────────────────────
def test_broken_equity_source_falls_back_to_paper_deposit(monkeypatch):
    """Сбой чтения баланса не должен молча останавливать движок нулём."""
    import services.live_executor as le

    class _Broken:
        def effective_equity_usdt(self):
            raise RuntimeError("exchange down")

    monkeypatch.setattr(le, "LIVE_EXECUTOR", _Broken(), raising=False)
    assert arb_capital.available_equity() == pytest.approx(
        float(settings.RISK_EQUITY_USDT)
    )


def test_zero_balance_falls_back_not_to_zero(monkeypatch):
    import services.live_executor as le

    class _Empty:
        def effective_equity_usdt(self):
            return 0.0

    monkeypatch.setattr(le, "LIVE_EXECUTOR", _Empty(), raising=False)
    assert arb_capital.available_equity() > 0


# ── интеграция ──────────────────────────────────────────────────────────────
class _Opportunity:
    """Стаб вместо ORM-модели: `HedgeBuilder.build` читает ровно три поля.

    Настоящая FundingArbOpportunity — это таблица, и тянуть её сюда значило бы
    завязать тест на схему БД ради трёх чисел.
    """

    spot_price = 0.07
    swap_price = 0.07
    funding_rate = 0.0004


def test_hedge_builder_uses_capital_share(monkeypatch):
    from services.funding_arbitrage import HedgeBuilder

    # Тест про долю капитала, а не про кэп нотионала ордера — изолируем от него.
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 10000.0, raising=False)
    monkeypatch.setattr(arb_capital, "available_equity", lambda: 2000.0)
    built = HedgeBuilder().build(_Opportunity())
    assert built["notional_usdt"] == pytest.approx(210.0, abs=1.0)


def test_explicit_notional_overrides_share(monkeypatch):
    """Ручное открытие через API задаёт размер явно и долей не перебивается."""
    from services.funding_arbitrage import HedgeBuilder

    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 10000.0, raising=False)
    monkeypatch.setattr(arb_capital, "available_equity", lambda: 2000.0)
    built = HedgeBuilder().build(_Opportunity(), notional_usdt=75.0)
    assert built["notional_usdt"] == pytest.approx(75.0)


def test_hedge_builder_respects_live_order_notional_cap(monkeypatch):
    """А вот сам кэп: нога хеджа не больше LIVE_MAX_ORDER_NOTIONAL_USDT, иначе
    live отклонил бы ордер, а бумага насчитала бы полный размер."""
    from services.funding_arbitrage import HedgeBuilder

    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 25.0, raising=False)
    monkeypatch.setattr(arb_capital, "available_equity", lambda: 2000.0)
    built = HedgeBuilder().build(_Opportunity(), notional_usdt=200.0)
    assert built["notional_usdt"] <= 25.0 + 1e-6
