"""Этап TP2 не перехватывается, фандинг — по расчётам (12.09.2026).

1. (#tp2-stage-2026-09-12) Правило «TP2 достигнут» закрывало весь остаток на
   92% пути к TP2 и книжило цену TP2, до которой рынок не доходил. Цена
   проходит полосу 92–100% раньше, чем касается TP2, поэтому этап TP2
   (частичная фиксация + трейл хвоста) за 90 дней не сработал ни разу.
2. (#funding-settlements-2026-09-12) Закрытие считало фондирование по плановой
   оценке — 1 час на любую сделку и период по умолчанию. Платит тот, кто
   держит позицию в момент расчёта (HTX, OKX — 00/08/16 UTC, Kraken —
   ежечасно), и для закрытой части число расчётов известно точно.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from core.config import settings
from services import funding_cost
from services.execution_engine import ExecutionEngine
from services.exit_policy import ExitPolicyService


def _utc(h, m=0, day=12):
    return datetime(2026, 9, day, h, m, tzinfo=timezone.utc)


# ── этап TP2 ────────────────────────────────────────────────────────────────

@pytest.fixture
def quiet_post_tp1(monkeypatch):
    """Остальные ветки после TP1 не должны сработать сами по себе."""
    monkeypatch.setattr(settings, "POST_TP1_TRAIL_ENABLED", False, raising=False)


def _decide(current: float):
    # Вход 100, TP2 102 (2%). 101.9 — 95% пути.
    return ExitPolicyService().after_tp1_decision(
        side="long", entry_price=100.0, current_price=current, tp2_price=102.0,
        lifecycle={"mfe_pct": (current - 100.0)}, symbol=None, market_type="swap",
    )


def test_with_the_stage_on_the_rule_leaves_tp2_to_the_stage(monkeypatch, quiet_post_tp1):
    monkeypatch.setattr(settings, "TP2_PROGRESSIVE_ENABLED", True, raising=False)

    decision = _decide(101.9)

    assert decision.reason != "tp2_reached"


def test_without_the_stage_the_close_is_booked_at_the_market(monkeypatch, quiet_post_tp1):
    """Цена TP2 — 102, рынок — 101.9. Книжить 102 значит записать выход по цене,
    которой рынок не видел."""
    monkeypatch.setattr(settings, "TP2_PROGRESSIVE_ENABLED", False, raising=False)

    decision = _decide(101.9)

    assert decision.exit is True and decision.reason == "tp2_reached"
    assert decision.exit_price == pytest.approx(101.9)


def test_the_stage_is_on_in_the_code_default():
    assert settings.TP2_PROGRESSIVE_ENABLED is True


# ── фандинг по расчётам ─────────────────────────────────────────────────────

def test_a_trade_across_one_settlement_pays_one_period():
    assert funding_cost.settlements_crossed(_utc(7, 30), _utc(8, 30), "htx") == 1


def test_a_trade_between_settlements_pays_nothing():
    assert funding_cost.settlements_crossed(_utc(8, 30), _utc(15, 59), "htx") == 0


def test_a_two_day_short_pays_six_on_htx_and_okx():
    for venue in ("htx", "okx"):
        assert funding_cost.settlements_crossed(_utc(1), _utc(1, day=14), venue) == 6


def test_kraken_settles_every_hour():
    assert funding_cost.settlements_crossed(_utc(7, 30), _utc(10, 15), "kraken") == 3


def test_opening_exactly_at_a_settlement_does_not_pay_it():
    """Расчёт в 08:00 уже прошёл для позиции, открытой в 08:00:00."""
    assert funding_cost.settlements_crossed(_utc(8), _utc(9), "htx") == 0


def test_naive_times_are_read_as_utc():
    naive_open = datetime(2026, 9, 12, 7, 30)
    assert funding_cost.settlements_crossed(naive_open, _utc(8, 30), "htx") == 1


def test_the_amount_follows_settlements_and_side():
    """Номинал 1000, ставка 0.01% за период, два расчёта: лонг платит 0.2,
    шорт получает 0.2."""
    kwargs = dict(notional=1000.0, market_type="swap", rate_pct=0.01, venue="htx",
                  opened_at=_utc(7), closed_at=_utc(16, 5))
    assert funding_cost.funding_usdt(side="long", **kwargs) == pytest.approx(0.2)
    assert funding_cost.funding_usdt(side="short", **kwargs) == pytest.approx(-0.2)


def test_spot_never_pays_funding():
    assert funding_cost.funding_usdt(
        notional=1000.0, side="long", market_type="spot", rate_pct=0.01,
        venue="htx", opened_at=_utc(1), closed_at=_utc(1, day=14)) == 0.0


def test_the_plan_estimate_still_amortises_without_times():
    """До входа время выхода неизвестно — там остаётся непрерывная оценка."""
    value = funding_cost.funding_usdt(notional=800.0, side="long", market_type="swap",
                                      rate_pct=0.01, venue="htx", hold_hours=4.0)
    assert value == pytest.approx(800.0 * 0.0001 * 0.5)


def test_both_closes_pass_the_real_holding_window():
    """Ровно эти вызовы брали плановую оценку в 1 час."""
    for method in (ExecutionEngine.partial_close_paper_position,
                   ExecutionEngine.close_paper_position):
        src = inspect.getsource(method)
        assert "opened_at=position.opened_at" in src, method.__name__
        assert "closed_at=datetime.now(timezone.utc)" in src, method.__name__
