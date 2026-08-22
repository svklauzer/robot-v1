"""Конверты капитала (#capital-envelopes-2026-08-21).

Замер, из-за которого это появилось: три контура претендовали на один депозит
независимо — 70% направленные + ~42% арбитраж (2 хеджа × 10.5% × 2 ноги) + 5%
сетка ≈ 117% при капитале 950. `used_margin()` видит только Signal и не знает
ни о `FundingArbPosition`, ни о корзинах сетки.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import capital_envelopes as env


class _EmptyDB:
    """БД без открытых арб-позиций."""

    def query(self, *_a, **_kw):
        return self

    def filter(self, *_a, **_kw):
        return self

    def count(self):
        return 0


class _BusyDB(_EmptyDB):
    def count(self):
        return 1


@pytest.fixture(autouse=True)
def _no_grid_cycles(monkeypatch):
    """По умолчанию сетка пуста — иначе тесты зависят от файла состояния."""
    monkeypatch.setattr(env, "_grid_holds", lambda: False)


def test_configured_shares_never_exceed_capital():
    """Главный инвариант: обещать больше 100% депозита нельзя."""
    shares = env.configured_shares()
    assert sum(shares.values()) <= 100.0, (
        f"сумма конвертов {sum(shares.values())}% > 100% — контуры обещают "
        "больше, чем есть на счёте"
    )


def test_disabled_and_empty_contours_release_share_to_directional(monkeypatch):
    """GRID_ENABLED=false и ENABLE_FUNDING_ARB=false → всё направленным."""
    monkeypatch.setattr(settings, "ENABLE_FUNDING_ARB", False, raising=False)
    monkeypatch.setattr(settings, "GRID_ENABLED", False, raising=False)

    shares = env.effective_shares(db=_EmptyDB())

    assert shares[env.ARB] == 0.0
    assert shares[env.GRID] == 0.0
    assert shares[env.DIRECTIONAL] == pytest.approx(
        env.configured_shares()[env.DIRECTIONAL]
        + env.configured_shares()[env.ARB]
        + env.configured_shares()[env.GRID]
    )


def test_disabled_but_holding_keeps_its_share(monkeypatch):
    """Выключен ≠ пуст.

    Стоп-кран сетки штатно оставляет живые корзины: новые не открываются, но
    существующие обслуживаются, чтобы маржа не заперлась. Отдать её долю в этот
    момент — дважды пообещать одни деньги.
    """
    monkeypatch.setattr(settings, "ENABLE_FUNDING_ARB", False, raising=False)
    monkeypatch.setattr(settings, "GRID_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "GRID_KILL_SWITCH_ENABLED", True, raising=False)
    monkeypatch.setattr(env, "_grid_holds", lambda: True)

    shares = env.effective_shares(db=_BusyDB())

    assert shares[env.ARB] == env.configured_shares()[env.ARB]
    assert shares[env.GRID] == env.configured_shares()[env.GRID]
    assert shares[env.DIRECTIONAL] == env.configured_shares()[env.DIRECTIONAL]


def test_kill_switch_alone_stops_new_grid_cycles(monkeypatch):
    monkeypatch.setattr(settings, "GRID_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "GRID_KILL_SWITCH_ENABLED", True, raising=False)
    assert env.grid_enabled() is False


def test_arb_notional_derives_from_envelope_and_fits_it(monkeypatch):
    """Два хеджа должны умещаться в конверт, а не превышать его вдвое."""
    monkeypatch.setattr(settings, "ENABLE_FUNDING_ARB", True, raising=False)
    monkeypatch.setattr(settings, "GRID_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "GRID_KILL_SWITCH_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "FUNDING_ARB_MAX_OPEN_HEDGES", 2, raising=False)

    equity = 950.0
    leg = env.arb_leg_notional(equity=equity, db=_BusyDB())
    envelope = env.envelope_usdt(env.ARB, equity=equity, db=_BusyDB())

    # хедж занимает ~2 нотионала (спот без плеча + своп)
    assert leg * 2 * 2 == pytest.approx(envelope, abs=0.05)
    assert leg * 2 * 2 <= envelope + 0.05


def test_no_db_is_treated_as_holding(monkeypatch):
    """Без БД неизвестно, пуст ли арбитраж → считаем занятым.

    Консервативно: лучше недодать направленным, чем пообещать дважды.
    """
    monkeypatch.setattr(settings, "ENABLE_FUNDING_ARB", False, raising=False)
    shares = env.effective_shares(db=None)
    assert shares[env.ARB] == env.configured_shares()[env.ARB]
