"""Отдачу задаёт ПОЛ, а не порог вооружения — и это не тот замок
(#be-lock-preempts-ride-2026-08-26, гипотеза ОТВЕРГНУТА в тот же день).

Отрицательный результат, записанный с числами.

Гипотеза была такая: шесть победителей 24–26.08 закрыты рано, потому что
безубыток-замок в `exit_policy` стоит выше блока ride и вооружается по
скальперскому `BREAKEVEN_LOCK_ARM_PCT=0.35`. Правка поднимала порог в тренде до
`TREND_RIDE_MIN_MFE_TO_PROTECT_PCT=0.8`. Её отверг существующий
`test_breakeven_lock_covers_the_band_below_trend_capture_arm`.

Две ошибки в одном рассуждении:

1. **Не тот механизм.** Телеметрия пишет `close_reason="breakeven_stop"`, а
   этот блок выдаёт `breakeven_lock`. Разные строки — разный код. `breakeven_stop`
   это перенос `signal.stop_price` в безубыток ПОСЛЕ TP1 в `signal_lifecycle`.
   Я привязался к похожему имени вместо того, чтобы проверить источник.

2. **Даже по своей логике правка ничего не давала.** Сделки с MFE 1.0–1.6%
   вооружали замок и при 0.35, и при 0.8 — а выходили всё равно по ПОЛУ 0.18%.
   Порог вооружения не влияет на размер отдачи. Зато в полосе 0.35–0.8 правка
   снимала единственную защиту, что тест и поймал.

Настоящая причина отдачи: после TP1 остаток охраняется безубытком и НЕ
подтягивается за MFE. Сделка идёт до +1.6%, возвращается к входу — вторая
половина даёт ноль.
"""
from __future__ import annotations

import inspect

import pytest

from core.config import settings


# ── ловушка имён ────────────────────────────────────────────────────────────
def test_breakeven_lock_and_breakeven_stop_are_different_mechanisms():
    """Похожие имена, разный код. Именно на этом я и ошибся."""
    from core import decision_codes
    from services import exit_policy, signal_lifecycle

    assert decision_codes.DECISION_BREAKEVEN_STOP == "breakeven_stop"
    assert 'reason="breakeven_lock"' in inspect.getsource(exit_policy)

    # `breakeven_stop` рождается при переносе стопа после TP1, а не в exit_policy.
    assert "DECISION_BREAKEVEN_STOP" in inspect.getsource(signal_lifecycle)
    assert "DECISION_BREAKEVEN_STOP" not in inspect.getsource(exit_policy)


# ── почему правка была бессмысленной ────────────────────────────────────────
@pytest.mark.parametrize("mfe,name", [
    (1.01, "BTC #434"),
    (1.61, "ADA #421"),
    (1.48, "AVAX #420"),
    (1.54, "XRP #419"),
])
def test_raising_the_arm_would_not_have_changed_these_trades(mfe, name):
    """Порог вооружения не влияет на исход: обе версии вооружают одинаково."""
    old_arm = float(settings.BREAKEVEN_LOCK_ARM_PCT)              # 0.35
    proposed_arm = float(settings.TREND_RIDE_MIN_MFE_TO_PROTECT_PCT)  # 0.8

    assert mfe >= old_arm, name
    assert mfe >= proposed_arm, name
    # Значит выход в обоих случаях идёт по ОДНОМУ И ТОМУ ЖЕ полу.
    assert float(settings.BREAKEVEN_LOCK_FLOOR_PCT) < old_arm


def test_the_giveback_is_set_by_the_floor():
    """Отдача = MFE минус пол. Пол фиксирован, поэтому отдача растёт с MFE.

    ADA #421: пик 1.61%, пол 0.18% — рынку вернули 1.43 п.п., а телеметрия
    записала «упущено 1.27%». Ни один порог вооружения этого не меняет.
    """
    floor = float(settings.BREAKEVEN_LOCK_FLOOR_PCT)
    peak = 1.61

    assert peak - floor == pytest.approx(1.43, abs=0.01)
    assert floor == pytest.approx(0.18)


def test_the_band_below_the_ride_arm_keeps_its_only_guard():
    """Полоса между замком и ride обязана остаться под защитой замка.

    Ради этого и написан `test_breakeven_lock_covers_the_band_below_trend_capture_arm`
    в test_exit_policy.py. Поднятие порога оставляло её пустой.
    """
    band_arm_effective = float(settings.MIN_PROTECTIVE_EXIT_PCT) / (
        1.0 - float(settings.TREND_CAPTURE_GIVEBACK_SHARE))
    arm = float(settings.BREAKEVEN_LOCK_ARM_PCT)

    assert arm < band_arm_effective, "полоса существует"
    assert arm < float(settings.TREND_RIDE_MIN_MFE_TO_PROTECT_PCT), \
        "ride в этой полосе вооружиться не может — замок единственный"


def test_arm_was_not_raised_in_trend():
    """Страховка от повторного захода: порог в тренде НЕ поднимаем."""
    src = inspect.getsource(__import__(
        "services.exit_policy", fromlist=["ExitPolicyService"]))

    assert "ОТМЕНЕНО В ТОТ ЖЕ ДЕНЬ" in src, \
        "запись об отвергнутой гипотезе исчезла — её повторят"
    assert 'be_arm = max(be_arm, float(\n                getattr(settings, "TREND_RIDE_MIN_MFE_TO_PROTECT_PCT"' not in src
