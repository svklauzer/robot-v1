"""Сайзинг по грейду не должен включаться, пока ось измерена в минус
(#grade-axis-2026-09-04).

Замер по 97 закрытым сделкам: confidence, по которой ставится грейд,
предсказывает СТОП (AUC 0.6605, ДИ [0.549; 0.772]), а грейд A значимо убыточен
(−0.4257R) при B, неотличимом от нуля. Обе оси сайзинга по грейду дают A
БОЛЬШЕ, чем B, и обе сейчас плоские — именно поэтому их легко включить, не
вспомнив об этом замере.
"""
from __future__ import annotations

import pytest

from core.config import Settings


def _blockers(**overrides) -> list[str]:
    return Settings(**overrides).production_blockers()


def _grade_blocker(blockers: list[str]) -> str | None:
    return next((b for b in blockers if "grade axis" in b), None)


def test_current_production_values_are_not_blocked():
    """Обе оси плоские — блокеру не за что цепляться. Иначе защита ронялась бы
    на исправном конфиге и её бы отключили, а не починили."""
    assert _grade_blocker(_blockers()) is None


def test_leverage_by_grade_is_blocked():
    blocker = _grade_blocker(_blockers(ENABLE_SMART_LEVERAGE=True))
    assert blocker is not None
    assert "ENABLE_SMART_LEVERAGE" in blocker


def test_capping_b_below_a_is_blocked():
    blocker = _grade_blocker(_blockers(DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE=0.5))
    assert blocker is not None
    assert "DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE" in blocker


def test_both_axes_are_named_in_one_blocker():
    """Одно сообщение с обеими причинами: починив одну, владелец должен сразу
    видеть, что вторая тоже открыта, а не получать блокер второй раз."""
    blocker = _grade_blocker(
        _blockers(ENABLE_SMART_LEVERAGE=True, DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE=0.5)
    )
    assert "ENABLE_SMART_LEVERAGE" in blocker
    assert "DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE" in blocker


def test_blocker_carries_the_measurement_not_just_a_verdict():
    """Блокер должен объяснять ЧЕМ он обоснован: иначе через месяц он выглядит
    как чужая перестраховка и снимается не глядя."""
    blocker = _grade_blocker(_blockers(ENABLE_SMART_LEVERAGE=True))
    assert "0.66" in blocker and "-0.4257R" in blocker


def test_deliberate_flag_lifts_the_guard():
    """Снимается флагом, а не правкой порогов — тот же порядок, что у
    UNIFIED_MARGIN_ACCOUNTING: ось перемерена, решение принято осознанно."""
    assert _grade_blocker(
        _blockers(ENABLE_SMART_LEVERAGE=True, GRADE_AXIS_VALIDATED=True)
    ) is None


def test_guard_does_not_depend_on_live_orders():
    """Бумага тоже страдает: искажённый сайзинг портит ту самую выборку, по
    которой ось и перемеряется."""
    assert _grade_blocker(
        _blockers(ENABLE_SMART_LEVERAGE=True, ENABLE_LIVE_ORDERS=False)
    ) is not None
