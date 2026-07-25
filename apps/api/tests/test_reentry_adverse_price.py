"""Price-guard перезахода (#reentry-adverse-price-2026-07-25).

Кулдаун по таймеру не защищает экономику: AVAX #282 закрыт 25.07 в 00:51 по
6.2708, #283 открыт в 01:54 по 6.2751 — тот же символ, та же сторона, цена хуже
на 0.07%. Таймер (60 мин) истёк и пропустил перезаход, round-trip оплачен дважды.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core.config import settings
from services.reentry_cooldown import ReEntryCooldownGuard


class _FakeSignal:
    def __init__(self, *, exit_price, closed_at, side="long", symbol="AVAX/USDT"):
        self.id = 282
        self.symbol = symbol
        self.side = side
        self.status = "closed"
        self.closed_reason = "breakeven_lock"
        self.closed_exit_price = exit_price
        self.closed_at = closed_at
        self.created_at = closed_at
        self.plan_json = {"priority_score": 100.0}


class _FakeQuery:
    def __init__(self, signal):
        self._signal = signal

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._signal


class _FakeDB:
    def __init__(self, signal):
        self._signal = signal

    def query(self, *a, **k):
        return _FakeQuery(self._signal)


def _check(entry_price, *, minutes_ago=63, exit_price=6.2708, side="long"):
    closed_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db = _FakeDB(_FakeSignal(exit_price=exit_price, closed_at=closed_at, side=side))
    return ReEntryCooldownGuard().check(
        db=db,
        bot_id=1,
        symbol="AVAX/USDT",
        side=side,
        current_priority_score=100.0,
        current_setup_score=100.0,
        current_rr_tp2=2.54,
        entry_price=entry_price,
    )


def test_real_avax_churn_case_is_blocked():
    """Боевой случай: таймер истёк (63 мин > 60), но вход дороже выхода."""
    d = _check(6.2751)
    assert d.allowed is False
    assert d.reason == "reentry_adverse_price"
    assert d.payload["adverse_pct"] == pytest.approx(0.0686, abs=1e-3)


def test_reentry_at_better_price_is_allowed():
    """Заходим дешевле, чем вышли — ровно то, ради чего выходили."""
    d = _check(6.2600)
    assert d.allowed is True
    assert d.reason == "cooldown_expired"


def test_large_adverse_move_is_not_churn():
    """Рынок ушёл далеко за пределы чурн-зоны — это уже другой сетап."""
    churn_max = float(settings.REENTRY_ADVERSE_CHURN_MAX_PCT)
    d = _check(6.2708 * (1 + (churn_max + 0.2) / 100))
    assert d.allowed is True


def test_short_side_mirrors_the_rule():
    """Для шорта «хуже» = ниже цены выхода."""
    worse = _check(6.2650, side="short")     # продаём дешевле, чем откупили
    better = _check(6.2800, side="short")
    assert worse.allowed is False
    assert worse.reason == "reentry_adverse_price"
    assert better.allowed is True


def test_guard_is_scoped_to_a_time_window():
    """Спустя окно связь с прошлым выходом теряется — не блокируем вечно."""
    window = float(settings.REENTRY_ADVERSE_WINDOW_MINUTES)
    d = _check(6.2751, minutes_ago=window + 10)
    assert d.allowed is True


def test_guard_can_be_disabled():
    old = settings.REENTRY_ADVERSE_PRICE_GUARD_ENABLED
    try:
        settings.REENTRY_ADVERSE_PRICE_GUARD_ENABLED = False
        assert _check(6.2751).allowed is True
    finally:
        settings.REENTRY_ADVERSE_PRICE_GUARD_ENABLED = old


def test_missing_entry_price_keeps_old_behaviour():
    """Fail-open: без цены гвард молчит, кулдаун работает как раньше."""
    d = _check(None)
    assert d.allowed is True
    assert d.reason == "cooldown_expired"
