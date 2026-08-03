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
    assert arb_capital.funding_arb_notional(equity=50.0) == pytest.approx(
        float(settings.FUNDING_ARB_MIN_NOTIONAL_USDT)
    )


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
def test_hedge_builder_uses_capital_share(monkeypatch):
    from services.funding_arbitrage import FundingArbOpportunity, HedgeBuilder

    monkeypatch.setattr(arb_capital, "available_equity", lambda: 2000.0)
    opp = FundingArbOpportunity(
        symbol="DOGE/USDT", spot_symbol="DOGE/USDT", swap_symbol="DOGE/USDT:USDT",
        funding_rate=0.0004, funding_rate_pct=0.04, annualized_rate_pct=43.8,
        spot_price=0.07, swap_price=0.07, basis_pct=0.0,
        fee_round_trip_pct=0.5, net_yield_per_period_pct=0.01,
        break_even_periods=12.5, annualized_net_yield_pct=10.0,
        estimated_edge_pct=0.01, status="ok", reject_reason=None,
        next_funding_at=None,
    )
    built = HedgeBuilder().build(opp)
    assert built["notional_usdt"] == pytest.approx(210.0, abs=1.0)


def test_explicit_notional_overrides_share(monkeypatch):
    """Ручное открытие через API задаёт размер явно и долей не перебивается."""
    from services.funding_arbitrage import FundingArbOpportunity, HedgeBuilder

    monkeypatch.setattr(arb_capital, "available_equity", lambda: 2000.0)
    opp = FundingArbOpportunity(
        symbol="DOGE/USDT", spot_symbol="DOGE/USDT", swap_symbol="DOGE/USDT:USDT",
        funding_rate=0.0004, funding_rate_pct=0.04, annualized_rate_pct=43.8,
        spot_price=0.07, swap_price=0.07, basis_pct=0.0,
        fee_round_trip_pct=0.5, net_yield_per_period_pct=0.01,
        break_even_periods=12.5, annualized_net_yield_pct=10.0,
        estimated_edge_pct=0.01, status="ok", reject_reason=None,
        next_funding_at=None,
    )
    assert HedgeBuilder().build(opp, notional_usdt=75.0)["notional_usdt"] == pytest.approx(75.0)
