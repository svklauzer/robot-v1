"""Смешивание ML в уверенность подчиняется режиму (#ml-blend-contract-2026-09-07).

Контракт режимов записан в ml_controller и до 07.09 нарушался в обход него:

  off       — ML не участвует в решениях вовсе;
  shadow    — считает и логирует, на сделки НЕ влияет;
  advisory  — рекомендует, решение остаётся за правилами;
  full_auto — гейтит и масштабирует в пределах guardrails.

`robot_loop._intelligence_effective_confidence` подмешивал MLScorer в уверенность
с весом 0.3 при ЛЮБОМ режиме. Уверенность гейтит вход (порог 60) и задаёт грейд,
то есть «не влияет» было неверно для всех четырёх.

Цена вопроса измерена на живых сигналах: у #479 уверенность после этого шага
составила 60.02 при пороге 60.0 — шаг решал, быть сделке или нет. У #476 он
опустил 75.8 до 63.5 и сменил грейд с A на B.
"""
from __future__ import annotations

import pytest

from workers.robot_loop import RobotLoop


class _Result:
    """Минимальный кандидат: функции нужны только оценки и режим."""
    regime = "trend_up_candidate"
    grade = "B"
    confidence_hint = 60.0
    setup_decision = "approve"
    setup_quality = {"final_score": 100.0}
    scores = {"trend": 70.0, "momentum": 60.0, "volume": 60.0,
              "structure": 60.0, "volatility": 60.0}
    action = "long"
    symbol = "ETH/USDT"
    # Блок ML включается только при непустом контексте младшего ТФ.
    timeframes = {"15m": {"last_close": 100.0, "ema20": 99.0, "ema50": 98.0,
                          "volume": 10.0, "volume_ma20": 8.0, "rsi14": 55.0,
                          "macd_hist": 0.1}}


@pytest.fixture
def loop(monkeypatch):
    lp = RobotLoop.__new__(RobotLoop)

    class _Scorer:
        def score(self, *a, **k):
            class _R:
                confidence = 35.0   # пол шкалы [35, 95] — то, что видно на живых
            return _R()

    class _Controller:
        mode = "shadow"

        def effective_mode(self):
            return self.mode

    lp.ml = _Scorer()
    lp.ml_controller = _Controller()
    lp._get_grade_stats = lambda: None
    return lp


def _confidence(loop, result, details):
    return loop._intelligence_effective_confidence(result, details)


# ── влияние на решение ──────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["off", "shadow", "advisory"])
def test_only_full_auto_moves_the_number_that_gates_entry(loop, mode):
    """Ради этого правка и делалась. Ниже full_auto уверенность обязана остаться
    той, что посчитал rule-based контур."""
    loop.ml_controller.mode = mode
    details: dict = {}

    with_ml = _confidence(loop, _Result(), details)

    assert details["ml_blend"]["applied"] is False
    assert with_ml == pytest.approx(details["ml_blend"]["before_ml"])


def test_full_auto_applies_the_blend(loop):
    loop.ml_controller.mode = "full_auto"
    details: dict = {}

    with_ml = _confidence(loop, _Result(), details)

    blend = details["ml_blend"]
    assert blend["applied"] is True
    assert with_ml == pytest.approx(blend["after_ml"])
    assert with_ml < blend["before_ml"], "ML на полу шкалы обязан тянуть вниз"


# ── измерение не теряется ───────────────────────────────────────────────────

def test_shadow_still_records_what_the_blend_would_have_done(loop):
    """Это и есть shadow: «считает и логирует». Если перестать записывать
    результат, сравнить включение с невключением будет не по чему — ровно та
    ошибка, которой избегали в гейте достижимости, оставив `would_block`.
    """
    loop.ml_controller.mode = "shadow"
    details: dict = {}

    _confidence(loop, _Result(), details)
    blend = details["ml_blend"]

    assert blend["after_ml"] < blend["before_ml"], "гипотетический результат не посчитан"
    assert blend["ml_confidence"] == pytest.approx(35.0)
    assert blend["weight"] == pytest.approx(0.30)
    assert blend["ml_mode"] == "shadow"


def test_the_mode_is_the_effective_one_not_the_configured_one(loop, monkeypatch):
    """Контроллер понижает full_auto до shadow при слабом или протухшем AUC.
    Спрашивать `settings.ML_MODE` напрямую значило бы обойти это понижение и
    вернуть дыру с другой стороны — уже под видом полномочий.
    """
    from core.config import settings

    monkeypatch.setattr(settings, "ML_MODE", "full_auto", raising=False)
    loop.ml_controller.mode = "shadow"   # контроллер отозвал полномочия
    details: dict = {}

    _confidence(loop, _Result(), details)

    assert details["ml_blend"]["applied"] is False
    assert details["ml_blend"]["ml_mode"] == "shadow"
