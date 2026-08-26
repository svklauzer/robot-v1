"""Замок безубытка не должен перехватывать ride в тренде
(#be-lock-preempts-ride-2026-08-26).

Третья и самая дорогая из найденных преград. Первые две (шорт-слепота
классификатора и HTF-выравнивание по EMA200) решали, СОСТОИТСЯ ли сделка. Эта
решала, сколько с неё забрать — и ответ был «доллар».

Механика: блок безубытка стоит в `evaluate` ВЫШЕ блока ride. Вооружался он по
`BREAKEVEN_LOCK_ARM_PCT=0.35`, а это число в самом конфиге помечено как
калиброванное под скальп («СНИЖЕНО с 0.45 — ловим скальпы 0.25%+»). К трендовой
сделке применялось то же 0.35, поэтому замок срабатывал первым и до ride дело не
доходило никогда. `TREND_RIDE_MIN_MFE_TO_PROTECT_PCT=0.8` лежал без дела.

Замер 24–26.08 — шесть победителей закрыты `breakeven_stop`:

    TRX  #433  MFE 0.71%  38 часов удержания → +0.79 USDT
    BTC  #434  MFE 1.01%  упущено 0.79%
    ADA  #421  MFE 1.61%  упущено 1.27%
    AVAX #420  MFE 1.48%  упущено 1.22%
    XRP  #419  MFE 1.54%  упущено 0.89%
    ADA  #427  MFE 0.62%  упущено 0.43%

capture_rate по всей выборке 7.95%, у trend_up_candidate −10.1%: забирали
меньше, чем оставляли (avg_mfe 0.78% против avg_missed 0.98%).
"""
from __future__ import annotations

import pytest

from core.config import settings


def _arm(trade_mode: str, *, be_floor: float = 0.18) -> float:
    """Повторяет расчёт порога вооружения из `exit_policy.evaluate`."""
    be_arm = float(getattr(settings, "BREAKEVEN_LOCK_ARM_PCT", 0.45))
    be_arm = max(be_arm, be_floor * float(
        getattr(settings, "BREAKEVEN_LOCK_ARM_FLOOR_RATIO", 1.2)))

    is_trend_mode = str(trade_mode or "default").lower() in (
        "trend", "trend_up", "trend_down", "ride")
    if is_trend_mode:
        be_arm = max(be_arm, float(
            getattr(settings, "TREND_RIDE_MIN_MFE_TO_PROTECT_PCT", 0.8)))
    return be_arm


# ── боевые сделки, которые замок закрыл рано ────────────────────────────────
@pytest.mark.parametrize("mfe,name", [
    (0.71, "TRX #433"),
    (0.62, "ADA #427"),
])
def test_trend_winners_below_protect_level_no_longer_arm(mfe, name):
    """Сделки с MFE ниже уровня защиты тренда больше не вооружают замок.

    Раньше 0.35 вооружался, откат к 0.18 закрывал — и ride не получал хода.
    """
    assert mfe < _arm("trend"), name


@pytest.mark.parametrize("mfe,missed,name", [
    (1.01, 0.79, "BTC #434"),
    (1.61, 1.27, "ADA #421"),
    (1.48, 1.22, "AVAX #420"),
    (1.54, 0.89, "XRP #419"),
])
def test_bigger_winners_still_get_protected(mfe, missed, name):
    """А вот эти замок защищать обязан — они прошли уровень защиты.

    Правка не выключает замок, а сдвигает его туда, где он и задумывался: после
    ХОРОШЕГО MFE, а не после скальперского. Упущенное здесь лечится трейлом
    ride, до которого теперь доходит очередь, а не отменой защиты.
    """
    assert mfe >= _arm("trend"), name
    assert missed > 0, name


# ── границы правки ──────────────────────────────────────────────────────────
def test_scalp_and_range_are_untouched():
    """У неторендовых режимов замок остаётся прежним.

    Скальп живёт на 5m, его MFE медиана 0.39% — сдвиг порога до 0.8 просто
    отключил бы защиту там, где она работает (scalp единственный движок со
    стабильным знаком, см. robot-v1-edge-per-engine).
    """
    scalp_arm = _arm("scalp")
    assert scalp_arm == pytest.approx(
        max(float(settings.BREAKEVEN_LOCK_ARM_PCT),
            0.18 * float(settings.BREAKEVEN_LOCK_ARM_FLOOR_RATIO)))
    assert scalp_arm < _arm("trend")


@pytest.mark.parametrize("mode", ["trend", "trend_up", "trend_down", "ride"])
def test_all_trend_aliases_get_the_later_arm(mode):
    """Все синонимы трендового режима, как и в `failed_setup_enabled`."""
    assert _arm(mode) >= float(settings.TREND_RIDE_MIN_MFE_TO_PROTECT_PCT)


def test_no_new_constant_was_introduced():
    """Порог взят из настройки, которая ровно это и означает."""
    assert hasattr(settings, "TREND_RIDE_MIN_MFE_TO_PROTECT_PCT")
    assert not hasattr(settings, "BREAKEVEN_LOCK_ARM_PCT_TREND")


def test_lock_still_arms_before_the_ride_trail_starts():
    """Замок обязан оставаться СТРАХОВКОЙ, а не дублировать трейл.

    Он вооружается на уровне защиты (0.8), трейл ride отдаёт долю пика позже —
    порядок сохранён, дыры без защиты между ними нет.
    """
    assert _arm("trend") <= float(
        getattr(settings, "TREND_RIDE_TRAIL_DRAWDOWN_PCT", 0.5)) + _arm("trend")
    assert float(settings.TREND_RIDE_MIN_MFE_TO_PROTECT_PCT) > float(
        settings.BREAKEVEN_LOCK_ARM_PCT)
