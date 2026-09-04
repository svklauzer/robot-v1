"""Оценки приводятся к шкале СТОРОНЫ сделки (#structure-mirror-2026-09-04).

`_score_context` считает по бычьей шкале: trend_up = 75, trend_down = 25.
trend и momentum для шортов зеркалились давно, structure — нет, хотя считается
так же: «хорошо для long, когда цена ближе к support» = 70. То есть шорт,
открытый прямо над поддержкой — в худшем для шорта месте, — получал за это
надбавку и в уверенность, и в setup_score.
"""
from __future__ import annotations

import pytest

from services.confidence_scale import (
    DIRECTIONAL, WEIGHTS, confidence_base, oriented, structure_for_side,
)

# Цена у поддержки: хорошо для лонга, плохо для шорта.
NEAR_SUPPORT = {"trend": 25.0, "momentum": 30.0, "volume": 75.0,
                "structure": 70.0, "volatility": 70.0}
# Цена у сопротивления: наоборот.
NEAR_RESISTANCE = {**NEAR_SUPPORT, "structure": 40.0}


def test_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_long_scale_is_untouched():
    assert oriented(NEAR_SUPPORT, "long") == {k: NEAR_SUPPORT[k] for k in WEIGHTS}


def test_short_mirrors_structure_along_with_trend_and_momentum():
    out = oriented(NEAR_SUPPORT, "short")
    assert out["trend"] == 75.0        # trend_down хорош для шорта
    assert out["momentum"] == 70.0
    assert out["structure"] == 30.0    # у поддержки — плохое место для шорта


def test_volume_and_volatility_are_not_mirrored():
    """Сильный объём и рабочая волатильность одинаково хороши обеим сторонам.
    Зеркалить их значило бы называть хорошее плохим."""
    out = oriented(NEAR_SUPPORT, "short")
    assert out["volume"] == 75.0
    assert out["volatility"] == 70.0
    assert "volume" not in DIRECTIONAL and "volatility" not in DIRECTIONAL


def test_short_prefers_resistance_to_support():
    """Суть правки одной строкой: до неё неравенство было обратным."""
    assert confidence_base(NEAR_RESISTANCE, "short") > confidence_base(NEAR_SUPPORT, "short")
    assert confidence_base(NEAR_SUPPORT, "long") > confidence_base(NEAR_RESISTANCE, "long")


def test_the_error_was_bigger_than_the_gap_between_grades():
    """Размах structure 40..70 при весе 0.20 — 6 пунктов уверенности; порог A
    (62) отстоит от порога B (60) на 2. Перевёрнутый компонент двигал сигнал
    через границу грейда."""
    delta = confidence_base(NEAR_RESISTANCE, "short") - confidence_base(NEAR_SUPPORT, "short")
    assert delta == pytest.approx(6.0)


def test_setup_quality_uses_the_same_scale_as_the_base():
    """В setup_score structure входит с множителем 0.25 — дороже, чем вес 0.20
    в базе, и больше, чем 7 пунктов между порогами setup A (65) и B (58)."""
    assert structure_for_side(NEAR_SUPPORT, "short") == 30.0
    assert structure_for_side(NEAR_SUPPORT, "long") == 70.0
    spread = (structure_for_side(NEAR_RESISTANCE, "short")
              - structure_for_side(NEAR_SUPPORT, "short")) * 0.25
    assert spread == pytest.approx(7.5)


def test_hold_and_unknown_actions_are_left_alone():
    assert oriented(NEAR_SUPPORT, "hold")["structure"] == 70.0
    assert oriented(NEAR_SUPPORT, "")["structure"] == 70.0


def test_missing_scores_fall_back_to_neutral_not_zero():
    """Нуль — это «худшее возможное», а отсутствие данных им не является."""
    assert oriented({}, "long") == {k: 50.0 for k in WEIGHTS}
    assert confidence_base({}, "short") == 50.0


def test_published_signal_and_scan_cannot_disagree():
    """Формула жила в двух копиях — в market_intelligence и в main. Правка
    зеркала в одной из них дала бы одному сигналу две разные уверенности: на
    скане одну, в опубликованном сигнале другую. Обе копии сведены к общей.
    """
    from main import _intelligence_effective_confidence

    class _R:
        confidence_hint = 0.0
        action = "short"
        scores = NEAR_SUPPORT
        setup_quality = {}          # без approve храповик не вмешивается
        setup_decision = ""

    assert _intelligence_effective_confidence(_R()) == confidence_base(NEAR_SUPPORT, "short")
