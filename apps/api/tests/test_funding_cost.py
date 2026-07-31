"""Контракт расчёта фондирования (#funding-cost-2026-07-30).

Проверяется то, что было посчитано неверно: период, знак и ставка.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import funding_cost


def _f(**kw):
    base = dict(notional=1000.0, side="long", market_type="swap",
                hold_hours=8.0, rate_pct=0.08, venue="htx")
    base.update(kw)
    return funding_cost.funding_usdt(**base)


# ── период ──────────────────────────────────────────────────────────────────
def test_spot_never_pays_funding():
    assert _f(market_type="spot") == 0.0


def test_full_period_charges_full_rate():
    # 1000 USDT × 0.08% × 1 период = 0.8
    assert _f(hold_hours=8.0) == pytest.approx(0.8)


def test_short_hold_charges_proportionally_not_a_full_period():
    """Главная ошибка прежней модели: полный период за сделку на 40 минут."""
    assert _f(hold_hours=40 / 60) == pytest.approx(0.8 * (40 / 60) / 8)


def test_kraken_settles_hourly_not_every_eight_hours():
    htx = _f(hold_hours=8.0, venue="htx")
    kraken = _f(hold_hours=8.0, venue="kraken")
    assert kraken == pytest.approx(htx * 8)


def test_zero_and_negative_hold_cost_nothing():
    assert _f(hold_hours=0.0) == 0.0
    assert _f(hold_hours=-5.0) == 0.0


# ── знак ────────────────────────────────────────────────────────────────────
def test_positive_rate_long_pays_short_receives():
    """При ставке > 0 лонги платят шортам. Шорту это доход, а не расход."""
    assert _f(side="long", rate_pct=0.08) > 0
    assert _f(side="short", rate_pct=0.08) < 0
    assert _f(side="long") == pytest.approx(-_f(side="short"))


def test_negative_rate_flips_who_pays():
    assert _f(side="long", rate_pct=-0.08) < 0
    assert _f(side="short", rate_pct=-0.08) > 0


# ── ставка ──────────────────────────────────────────────────────────────────
def test_explicit_rate_wins_over_fallback(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_FALLBACK_RATE_PCT", 0.01, raising=False)
    assert _f(rate_pct=0.08) == pytest.approx(0.8)


def test_fallback_used_when_no_observations(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_FALLBACK_RATE_PCT", 0.01, raising=False)
    monkeypatch.setattr(funding_cost, "observed_rate_pct", lambda _s: None)
    value = funding_cost.funding_usdt(notional=1000.0, side="long", market_type="swap",
                                      hold_hours=8.0, venue="htx", symbol="BTC/USDT")
    assert value == pytest.approx(0.1)


def test_observed_rate_is_preferred_over_fallback(monkeypatch):
    monkeypatch.setattr(funding_cost, "observed_rate_pct", lambda _s: 0.08)
    value = funding_cost.funding_usdt(notional=1000.0, side="long", market_type="swap",
                                      hold_hours=8.0, venue="htx", symbol="BTC/USDT")
    assert value == pytest.approx(0.8)


def test_broken_journal_does_not_raise(monkeypatch):
    def _boom(_symbol):
        raise RuntimeError("journal unreadable")

    monkeypatch.setattr("services.funding_rate_history.stability", _boom)
    assert funding_cost.observed_rate_pct("BTC/USDT") is None


# ── интеграция с CostEngine ─────────────────────────────────────────────────
def test_cost_engine_short_gets_funding_as_income():
    """Регресс на исходную ошибку: шорт получал фондирование расходом.

    При ставке > 0 фондирование уменьшает издержки шорта, а не увеличивает их.
    """
    from services.cost_engine import CostEngine

    engine = CostEngine()
    short = engine.estimate(symbol="BTC/USDT", market_type="swap", side="short",
                            entry_price=100.0, exit_price=99.0, qty=10.0,
                            hold_hours=8.0, funding_rate_pct=0.08, venue="htx")
    long = engine.estimate(symbol="BTC/USDT", market_type="swap", side="long",
                           entry_price=100.0, exit_price=101.0, qty=10.0,
                           hold_hours=8.0, funding_rate_pct=0.08, venue="htx")
    assert short.funding_buffer < 0
    assert long.funding_buffer > 0


def test_cost_engine_charges_slippage_on_both_legs():
    """Выход рыночным ордером проскальзывает так же, как вход."""
    from services.cost_engine import CostEngine

    preview = CostEngine().estimate(symbol="BTC/USDT", market_type="spot", side="long",
                                    entry_price=100.0, exit_price=100.0, qty=10.0)
    expected = 2 * 1000.0 * settings.SLIPPAGE_BUFFER_PCT
    assert preview.slippage_buffer == pytest.approx(expected)
