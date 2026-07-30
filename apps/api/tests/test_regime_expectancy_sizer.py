"""Контракт оси «размер от ожидания режима» (#regime-sizing-2026-07-30).

Проверяется поведение, на которое опирается решение о деньгах, а не форма
чисел: короткая история не режет, отрицательное ожидание режет с шринкажем,
пол не пробивается, положительный режим не наказывается, а сбой расчёта не
роняет торговый цикл.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services.regime_expectancy_sizer import RegimeExpectancySizer


class _Signal:
    def __init__(self, regime: str, net: float, risk: float):
        self.plan_json = {"regime": regime}
        self.closed_net_pnl = net
        self.net_pnl_stop = -abs(risk)


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_a, **_k):
        return self

    def order_by(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def all(self):
        return self._rows


class _DB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_a, **_k):
        return _Query(self._rows)


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    # Ось включена по умолчанию (см. REGIME_EXP_SIZING_ENABLED в config.py) как
    # страховочный контур против режимов с отрицательным rolling expectancy.
    monkeypatch.setattr(settings, "REGIME_EXP_SIZING_ENABLED", True, raising=False)
    RegimeExpectancySizer.invalidate()
    yield
    RegimeExpectancySizer.invalidate()


def _sizer():
    # now=... фиксирует часы, иначе TTL-кэш переиспользует чужую таблицу.
    return RegimeExpectancySizer(now=0.0)


def test_short_history_does_not_cut_size():
    """Пока сделок меньше REGIME_EXP_MIN_HISTORY, режим идёт полным размером.

    Иначе случайная серия из трёх стопов на новом сетапе навсегда ужимает его
    до наблюдательного размера ещё до того, как о нём хоть что-то известно.
    """
    rows = [_Signal("crt", -2.0, 1.0) for _ in range(5)]
    decision = _sizer().evaluate(_DB(rows), "crt")
    assert decision.multiplier == 1.0
    assert decision.reason == "insufficient_history"


def test_positive_expectancy_keeps_full_size():
    rows = [_Signal("crt", 0.5, 1.0) for _ in range(40)]
    decision = _sizer().evaluate(_DB(rows), "crt")
    assert decision.multiplier == 1.0
    assert decision.expectancy_r == pytest.approx(0.5)


def test_negative_expectancy_approaches_floor_from_above():
    """Режим, теряющий много больше REGIME_EXP_FLOOR_AT_R, стремится к полу.

    Именно СТРЕМИТСЯ, а не достигает: шринкаж n/(n+prior) асимптотически
    приближается к единице, но не равен ей ни на какой конечной выборке.
    Это не погрешность, а смысл: полная уверенность в оценке не наступает
    никогда, и последний процент размера режим сохраняет всегда.
    """
    rows = [_Signal("trend_up_candidate", -0.5, 1.0) for _ in range(200)]
    decision = _sizer().evaluate(_DB(rows), "trend_up_candidate")
    floor = float(settings.REGIME_EXP_MIN_MULT)
    assert floor < decision.multiplier < floor + 0.10
    assert decision.reason == "expectancy_negative"


def test_floor_is_never_zero():
    """Наблюдательный размер обязан быть > 0.

    При нуле режим перестаёт давать закрытия, скользящее окно пустеет,
    ожидание замерзает — и вернуться к полному размеру он уже не сможет.
    """
    assert float(settings.REGIME_EXP_MIN_MULT) > 0


def test_shrinkage_short_sample_cuts_less_than_long_sample():
    """При одинаковом ожидании короткая выборка режет мягче длинной."""
    short = _sizer().evaluate(_DB([_Signal("scalp", -0.2, 1.0) for _ in range(20)]), "scalp")
    RegimeExpectancySizer.invalidate()
    long = _sizer().evaluate(_DB([_Signal("scalp", -0.2, 1.0) for _ in range(400)]), "scalp")
    assert short.multiplier > long.multiplier


def test_mild_negative_cuts_less_than_deep_negative():
    mild = _sizer().evaluate(_DB([_Signal("range", -0.02, 1.0) for _ in range(60)]), "range")
    RegimeExpectancySizer.invalidate()
    deep = _sizer().evaluate(_DB([_Signal("range", -0.30, 1.0) for _ in range(60)]), "range")
    assert mild.multiplier > deep.multiplier


def test_trades_without_planned_risk_are_excluded_not_zeroed():
    """Сделка без планового риска не попадает НИ в числитель, НИ в знаменатель.

    Если считать её риск нулевым, знаменатель занижается и ожидание в R
    раздувается — режим получит множитель, не соответствующий его результату.
    """
    rows = [_Signal("crt", 1.0, 1.0) for _ in range(30)]
    for row in rows[:10]:
        row.net_pnl_stop = 0.0
    decision = _sizer().evaluate(_DB(rows), "crt")
    assert decision.sample == 20
    assert decision.risk_usdt == pytest.approx(20.0)


def test_unknown_regime_is_neutral():
    assert _sizer().evaluate(_DB([]), None).multiplier == 1.0
    assert _sizer().evaluate(_DB([]), "").multiplier == 1.0


def test_disabled_flag_returns_neutral(monkeypatch):
    monkeypatch.setattr(settings, "REGIME_EXP_SIZING_ENABLED", False, raising=False)
    rows = [_Signal("trend_up_candidate", -0.5, 1.0) for _ in range(200)]
    decision = _sizer().evaluate(_DB(rows), "trend_up_candidate")
    assert decision.multiplier == 1.0
    assert decision.reason == "disabled"


def test_db_failure_is_fail_open():
    """Сбой запроса не имеет права остановить торговый цикл."""
    class _Broken:
        def query(self, *_a, **_k):
            raise RuntimeError("db down")

    decision = _sizer().evaluate(_Broken(), "crt")
    assert decision.multiplier == 1.0


def test_multiplier_never_exceeds_one():
    """Ось умеет только уменьшать. Ожидание +2R не даёт права увеличить ставку —
    размер назначает риск-модель, а не бэктест прошлого."""
    rows = [_Signal("reversal_long_candidate", 5.0, 1.0) for _ in range(80)]
    assert _sizer().evaluate(_DB(rows), "reversal_long_candidate").multiplier == 1.0
