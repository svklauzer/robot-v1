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


def test_notional_now_fits_the_capital_envelope():
    """Размер СОЗНАТЕЛЬНО уменьшен (#capital-envelopes-2026-08-21).

    Прежний тест закреплял ~99.75 при эквити 950 («механизм меняется, поведение
    нет»). Но именно поведение и было неверным: доля 10.5% на ногу давала два
    хеджа по ~2 нотионала капитала каждый — около 42% депозита при конверте 20%.
    Три контура вместе обещали ~117% счёта.

    Теперь нотионал выводится из конверта, и инвариант такой: ВСЕ хеджи разом
    умещаются в отведённую долю. Это и проверяем — не конкретное число, а
    непревышение конверта.
    """
    from services import capital_envelopes as env

    equity = 950.0
    leg = arb_capital.funding_arb_notional(equity=equity)
    envelope = env.envelope_usdt(env.ARB, equity=equity, db=None)
    hedges = int(settings.FUNDING_ARB_MAX_OPEN_HEDGES)

    # хедж занимает ~2 нотионала: спотовая нога без плеча + своп
    assert leg * hedges * 2 <= envelope + 0.05, (
        "все хеджи вместе не должны превышать конверт арбитража"
    )
    # cross-arb пока считает по своей доле — он выключен, конверт ему не нужен
    assert arb_capital.cross_farb_notional(equity=equity) > 0


# ── границы ─────────────────────────────────────────────────────────────────
def test_tiny_equity_clamps_to_exchange_minimum():
    """Эквити ВЫВОДИМ из настроек, а не зашиваем.

    Было `equity=50` при минимуме 20: доля 10.5% давала 5.25 → клэмп срабатывал.
    После снижения минимума до 5 те же 5.25 стали ВЫШЕ него, и тест падал, хотя
    клэмп цел. Берём заведомо малое эквити относительно текущих настроек —
    проверяем механизм, а не совпадение констант.
    """
    from services import capital_envelopes as env

    share = float(env.configured_shares()[env.ARB]) / 100.0 or 0.20
    hedges = max(1, int(settings.FUNDING_ARB_MAX_OPEN_HEDGES))
    floor = float(settings.FUNDING_ARB_MIN_NOTIONAL_USDT)
    # конверт от такого эквити делится на хеджи и ноги → заведомо ниже пола
    tiny_equity = floor * hedges * 2.0 / share / 2.0
    assert arb_capital.funding_arb_notional(equity=tiny_equity) == pytest.approx(floor)


def test_huge_equity_clamps_to_ceiling():
    assert arb_capital.funding_arb_notional(equity=10 ** 6) == pytest.approx(
        float(settings.FUNDING_ARB_MAX_NOTIONAL_USDT)
    )


def test_zero_envelope_still_respects_minimum(monkeypatch):
    """Нулевой конверт не должен давать нулевой лот — только биржевой минимум.

    Крутится теперь КОНВЕРТ: `FUNDING_ARB_NOTIONAL_PCT` удалён как мёртвый
    (нотионал выводится из конверта, отдельной доли больше нет).
    """
    monkeypatch.setattr(settings, "CAPITAL_ENVELOPE_ARB_PCT", 0.0, raising=False)
    assert arb_capital.funding_arb_notional(equity=950.0) == pytest.approx(
        float(settings.FUNDING_ARB_MIN_NOTIONAL_USDT)
    )


def test_negative_envelope_is_treated_as_zero(monkeypatch):
    monkeypatch.setattr(settings, "CAPITAL_ENVELOPE_ARB_PCT", -5.0, raising=False)
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


def test_hedge_builder_sizes_from_capital_envelope(monkeypatch):
    """Размер хеджа масштабируется капиталом и укладывается в конверт."""
    from services.funding_arbitrage import HedgeBuilder
    from services import capital_envelopes as env

    # Тест про долю капитала, а не про кэп нотионала ордера — изолируем от него.
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 10000.0, raising=False)
    monkeypatch.setattr(arb_capital, "available_equity", lambda: 2000.0)

    built = HedgeBuilder().build(_Opportunity())
    envelope = env.envelope_usdt(env.ARB, equity=2000.0, db=None)
    hedges = int(settings.FUNDING_ARB_MAX_OPEN_HEDGES)

    assert built["notional_usdt"] * hedges * 2 <= envelope + 0.05


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
