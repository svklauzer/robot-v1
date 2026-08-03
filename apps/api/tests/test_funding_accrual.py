"""Начисление funding по факту и связка горизонта удержания.

(#funding-periodic-accrual-2026-08-03, #funding-hold-horizon-2026-08-03)

Обе правки — про одно: движок считал деньги по предположениям, которые не
совпадали с его собственным поведением.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.config import settings
from services import funding_accrual as fa
from services.funding_arbitrage import FundingArbEngine, _assumed_hold_periods

HOUR = 3600.0


# ── пер-периодное начисление ────────────────────────────────────────────────
def test_accrual_uses_current_rate_not_entry_rate():
    """Ставка берётся на момент начисления: биржа платит по действующей.

    Прежняя формула брала ставку входа за весь срок, а funding mean-reverts —
    отсюда систематическое завышение в одну сторону.
    """
    ledger = fa.empty_ledger(now_ts=0.0, rate=0.0006)
    # Первые 8 часов ставка упала вдвое — начисляем по НОВОЙ.
    ledger = fa.accrue(ledger, notional=100.0, current_rate=0.0003, now_ts=8 * HOUR)
    assert ledger["accrued_usdt"] == pytest.approx(100.0 * 0.0003 * 1.0)


def test_accrual_is_prorata_not_stepwise():
    """Полшага времени — половина периода, а не ноль и не целый."""
    ledger = fa.empty_ledger(now_ts=0.0, rate=0.0004)
    ledger = fa.accrue(ledger, notional=100.0, current_rate=0.0004, now_ts=4 * HOUR)
    assert ledger["periods"] == pytest.approx(0.5)
    assert ledger["accrued_usdt"] == pytest.approx(100.0 * 0.0004 * 0.5)


def test_accrual_accumulates_across_steps():
    ledger = fa.empty_ledger(now_ts=0.0, rate=0.0004)
    ledger = fa.accrue(ledger, notional=100.0, current_rate=0.0004, now_ts=8 * HOUR)
    ledger = fa.accrue(ledger, notional=100.0, current_rate=0.0002, now_ts=16 * HOUR)
    assert ledger["accrued_usdt"] == pytest.approx(100.0 * (0.0004 + 0.0002))
    assert ledger["periods"] == pytest.approx(2.0)


def test_time_does_not_run_backwards():
    ledger = fa.empty_ledger(now_ts=100 * HOUR, rate=0.0004)
    ledger = fa.accrue(ledger, notional=100.0, current_rate=0.0004, now_ts=50 * HOUR)
    assert ledger["accrued_usdt"] == 0.0


def test_negative_rate_is_a_cost_not_income():
    """При отрицательной ставке шорт-нога платит — это расход, а не ноль."""
    ledger = fa.empty_ledger(now_ts=0.0, rate=-0.0002)
    ledger = fa.accrue(ledger, notional=100.0, current_rate=-0.0002, now_ts=8 * HOUR)
    assert ledger["accrued_usdt"] < 0


# ── что берётся на закрытии ─────────────────────────────────────────────────
def test_ledger_wins_over_estimates():
    raw = {"accrual": fa.accrue(fa.empty_ledger(0.0, 0.0004), notional=100.0,
                                current_rate=0.0002, now_ts=24 * HOUR)}
    value, method = fa.collected_usdt(raw, notional=100.0, entry_rate=0.0006,
                                      exit_rate=0.0001, periods=3)
    assert method == "per_period"
    assert value == pytest.approx(100.0 * 0.0002 * 3)


def test_legacy_position_falls_back_and_is_labelled():
    """Позиции до правки ledger'а не имеют — считаем прежним способом,
    но помечаем, иначе старые и новые сделки смешаются в статистике."""
    value, method = fa.collected_usdt(None, notional=100.0, entry_rate=0.0006,
                                      exit_rate=0.0002, periods=30)
    assert method == "trapezoid_legacy"
    assert value == pytest.approx(100.0 * 0.0004 * 30)


def test_oldest_positions_use_entry_rate_when_no_exit_known():
    value, method = fa.collected_usdt(None, notional=100.0, entry_rate=0.000624,
                                      exit_rate=None, periods=30)
    assert method == "entry_rate_legacy"
    # Ровно те +1.8716, что записаны у сделки #1 — сверка со старой формулой.
    assert value == pytest.approx(1.8716, abs=1e-3)


def test_entry_rate_overstates_when_rate_decays():
    """Причина всей правки: ставка падает, а старая формула этого не видит."""
    ledger = fa.empty_ledger(0.0, 0.0006)
    for step in range(1, 31):
        decayed = 0.0006 * (1 - step / 40)
        ledger = fa.accrue(ledger, notional=100.0, current_rate=decayed,
                           now_ts=step * 8 * HOUR)
    honest, _ = fa.collected_usdt({"accrual": ledger}, notional=100.0,
                                  entry_rate=0.0006, exit_rate=None, periods=30)
    legacy, _ = fa.collected_usdt(None, notional=100.0, entry_rate=0.0006,
                                  exit_rate=None, periods=30)
    assert honest < legacy
    assert legacy - honest > 0.25  # порядок завышения из конфига


# ── горизонт удержания ──────────────────────────────────────────────────────
def test_horizon_follows_max_hold(monkeypatch):
    """Амортизация комиссии считается из фактического потолка удержания."""
    monkeypatch.setattr(settings, "FUNDING_ARB_MAX_HOLD_HOURS", 240, raising=False)
    monkeypatch.setattr(settings, "FUNDING_ARB_ASSUMED_HOLD_PERIODS_OVERRIDE", 0, raising=False)
    assert _assumed_hold_periods() == 30


def test_horizon_cannot_drift_from_max_hold(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_ARB_ASSUMED_HOLD_PERIODS_OVERRIDE", 0, raising=False)
    monkeypatch.setattr(settings, "FUNDING_ARB_MAX_HOLD_HOURS", 80, raising=False)
    assert _assumed_hold_periods() == 10
    monkeypatch.setattr(settings, "FUNDING_ARB_MAX_HOLD_HOURS", 400, raising=False)
    assert _assumed_hold_periods() == 50


def test_override_is_available_for_emergencies(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_ARB_MAX_HOLD_HOURS", 240, raising=False)
    monkeypatch.setattr(settings, "FUNDING_ARB_ASSUMED_HOLD_PERIODS_OVERRIDE", 5, raising=False)
    assert _assumed_hold_periods() == 5


def test_horizon_is_never_zero(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_ARB_ASSUMED_HOLD_PERIODS_OVERRIDE", 0, raising=False)
    monkeypatch.setattr(settings, "FUNDING_ARB_MAX_HOLD_HOURS", 1, raising=False)
    assert _assumed_hold_periods() >= 1


# ── врезка в цикл сопровождения ─────────────────────────────────────────────
# Ledger сам по себе ничего не считает: он наполняется только там, где движок
# ходит за ставкой. Ниже — что этот вызов действительно происходит и что
# результат доживает до закрытия.

def _position(*, opened_hours_ago: float = 0.0, notional: float = 100.0,
              rate: float = 0.0006, with_ledger: bool = True, now=None):
    """Позиция-заглушка. `now` фиксируется явно: иначе opened_at и опорная
    точка теста берутся из двух разных вызовов now() и расходятся на
    микросекунды, чего хватает, чтобы точные сравнения плавали."""
    now = now or datetime.now(timezone.utc)
    opened_at = now - timedelta(hours=opened_hours_ago)
    raw = {"hedge": {}}
    if with_ledger:
        raw["accrual"] = fa.empty_ledger(now_ts=opened_at.timestamp(), rate=rate)
    return SimpleNamespace(
        id=1, notional_usdt=notional, entry_funding_rate=rate,
        opened_at=opened_at, raw_json=raw,
    )


def test_management_pass_fills_the_ledger():
    """Проход цикла начисляет по фактически прошедшему времени."""
    engine = FundingArbEngine.__new__(FundingArbEngine)
    now = datetime.now(timezone.utc)
    position = _position(opened_hours_ago=8.0, now=now)

    out = engine._accrue_position(position, current_rate=0.0004, now_ts=now.timestamp())

    assert out["periods"] == pytest.approx(1.0)
    assert out["accrued_usdt"] == pytest.approx(100.0 * 0.0004)
    assert position.raw_json["accrual"]["accrued_usdt"] == out["accrued_usdt"]


def test_repeated_passes_do_not_double_count():
    """Два прохода подряд — второй начисляет только за свой интервал."""
    engine = FundingArbEngine.__new__(FundingArbEngine)
    now = datetime.now(timezone.utc)
    position = _position(opened_hours_ago=0.0, now=now)
    base = now.timestamp()

    engine._accrue_position(position, current_rate=0.0004, now_ts=base + 8 * HOUR)
    second = engine._accrue_position(position, current_rate=0.0004, now_ts=base + 8 * HOUR)

    assert second["periods"] == pytest.approx(1.0)


def test_rate_change_between_passes_is_respected():
    """Ставка упала между проходами — второй интервал считается по новой."""
    engine = FundingArbEngine.__new__(FundingArbEngine)
    now = datetime.now(timezone.utc)
    position = _position(opened_hours_ago=0.0, now=now)
    base = now.timestamp()

    engine._accrue_position(position, current_rate=0.0006, now_ts=base + 8 * HOUR)
    out = engine._accrue_position(position, current_rate=0.0001, now_ts=base + 16 * HOUR)

    assert out["accrued_usdt"] == pytest.approx(100.0 * (0.0006 + 0.0001))
    # Ставка входа за 2 периода дала бы вдвое больше — вот цена правки.
    assert out["accrued_usdt"] < 100.0 * 0.0006 * 2


def test_position_opened_before_the_change_is_marked_not_backfilled():
    """У старой позиции ledger'а нет. Достраивать историю нечем — помечаем."""
    engine = FundingArbEngine.__new__(FundingArbEngine)
    now = datetime.now(timezone.utc)
    position = _position(opened_hours_ago=100.0, with_ledger=False, now=now)

    engine._accrue_position(position, current_rate=0.0004, now_ts=now.timestamp())
    ledger = position.raw_json["accrual"]

    assert ledger["accrued_usdt"] == 0.0
    assert ledger["started_mid_position"] is True
    assert ledger["unmeasured_hours"] == pytest.approx(100.0, abs=0.1)


def test_ledger_survives_to_close():
    """Круг: открыли → начислили → закрыли. На закрытии берётся факт."""
    engine = FundingArbEngine.__new__(FundingArbEngine)
    now = datetime.now(timezone.utc)
    position = _position(opened_hours_ago=0.0, now=now)
    base = now.timestamp()

    for step in range(1, 31):
        engine._accrue_position(position, current_rate=0.0006 * (1 - step / 40),
                                now_ts=base + step * 8 * HOUR)

    value, method = fa.collected_usdt(
        position.raw_json, notional=100.0, entry_rate=0.0006,
        exit_rate=0.0001, periods=30,
    )
    assert method == "per_period"
    assert value == pytest.approx(position.raw_json["accrual"]["accrued_usdt"], abs=1e-6)
    # Прежняя формула по ставке входа завысила бы на заметную величину.
    assert 100.0 * 0.0006 * 30 - value > 0.25
