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


# ── храповик (#confidence-ratchet-2026-09-04) ───────────────────────────────

from core.config import settings
from services.confidence_scale import calibrate


def test_disagreement_no_longer_raises_confidence():
    """Суть правки. Рынок говорит 45, чек-лист 76. Храповик брал большую ногу и
    выдавал 68.4 — выше порога грейда A (62). Среднее даёт 56.7, ниже порога.
    """
    assert calibrate(45.0, 76.0, "approve").effective == pytest.approx(56.7)


def test_a_weak_checklist_can_now_lower_a_strong_base():
    """Раньше не могло вовсе: max() не умеет опускать. Именно поэтому ведро A
    оказалось БОЛЬШЕ ведра B (53 против 44) при более строгом гейте."""
    assert calibrate(80.0, 72.0, "approve").effective < 80.0


def test_a_strong_checklist_still_lifts_a_weak_base():
    """Механизм вводился ради этого, и это сохранено: правка убирает
    односторонность, а не сам подъём."""
    assert calibrate(50.0, 88.0, "approve").effective > 50.0


def test_agreeing_legs_are_left_where_they_are():
    """Когда обе ноги говорят одно, среднее не двигает результат — иначе
    правка меняла бы и те входы, к которым претензий нет."""
    assert calibrate(72.0, 80.0, "approve").effective == pytest.approx(72.0)


def test_caps_still_hold():
    assert calibrate(100.0, 100.0, "approve").effective == 88.0
    assert calibrate(100.0, 60.0, "wait").effective == 72.0


def test_below_the_branch_thresholds_only_the_base_survives():
    out = calibrate(61.0, 40.0, "approve")
    assert out.branch == "base_only"
    assert out.effective == 61.0
    assert out.setup_leg is None


def test_leg_gap_is_recorded_for_the_next_measurement():
    """Расхождение ног — ровно та величина, которую храповик игнорировал.
    Пишется в план отдельно, чтобы разбор стопов проверил, предсказывает ли она
    исход сама по себе."""
    payload = calibrate(45.0, 76.0, "approve").as_dict()
    assert payload["leg_gap"] == pytest.approx(23.4)
    assert payload["base"] == 45.0 and payload["setup_leg"] == pytest.approx(68.4)


def test_rollback_switch_restores_the_old_one_sided_form(monkeypatch):
    """Правка меняет выборку входов и на старых данных не проверяется. Если
    поток схлопнется, прежнее поведение обязано возвращаться без деплоя."""
    monkeypatch.setattr(settings, "CONFIDENCE_SYMMETRIC_BLEND", False, raising=False)
    assert calibrate(45.0, 76.0, "approve").effective == pytest.approx(68.4)


def test_the_live_path_and_the_scan_cannot_diverge():
    """Формула жила в ТРЁХ копиях и две из них расходились: у боевой не было
    ветки approve≥62 и множитель был 0.90, у скана — 0.92 и потолок 80. Скан
    показывал владельцу не то число, по которому робот торгует.
    """
    from main import _intelligence_effective_confidence
    from services.confidence_scale import confidence_calibration

    class _R:
        confidence_hint = 45.0
        action = "long"
        scores = {}                     # без scores база берётся как есть
        setup_quality = {"final_score": 76.0, "decision": "approve"}
        setup_decision = "approve"

    assert _intelligence_effective_confidence(_R()) == confidence_calibration(_R()).effective
