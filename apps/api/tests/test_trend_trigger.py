"""Контракт триггера трендового входа (#trend-trigger-2026-07-30).

Правило добавляет во вход недостающее измерение — растянутость цены от опоры.
Тесты описывают то, из-за чего движок терял деньги (вход в уже состоявшийся
импульс), и то, что правило не имеет права сломать: остальные движки, работу
цикла при отсутствии индикаторов, откаты против направления сделки.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import trend_trigger


def _ctx(last: float, ema20: float, atr: float = 1.0) -> dict:
    return {"last_close": last, "ema20": ema20, "atr14": atr}


def _tfs(last: float, ema20: float, atr: float = 1.0) -> dict:
    return {"15m": _ctx(last, ema20, atr), "5m": _ctx(last, ema20, atr)}


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    # Дефолт — режим shadow: порог не откалиброван, вход не блокируется.
    # Тесты правила проверяют enforce; отдельный тест ниже — что дефолт
    # действительно только измеряет.
    monkeypatch.setattr(settings, "TREND_TRIGGER_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "TREND_MAX_EXTENSION_ATR", 1.5, raising=False)
    monkeypatch.setattr(settings, "TREND_TRIGGER_MODE", "enforce", raising=False)


# ── измерение растянутости ──────────────────────────────────────────────────
def test_extension_is_measured_in_atr_not_percent():
    """ATR, а не проценты: 1% растяжения на BTC и на ADA — разные события."""
    assert trend_trigger.extension_in_atr(_ctx(102.0, 100.0, 2.0), "long") == pytest.approx(1.0)
    assert trend_trigger.extension_in_atr(_ctx(110.0, 100.0, 2.0), "long") == pytest.approx(5.0)


def test_extension_is_directional_for_short():
    """Для шорта растянутость — уход цены ВНИЗ от опоры."""
    assert trend_trigger.extension_in_atr(_ctx(98.0, 100.0, 2.0), "short") == pytest.approx(1.0)
    assert trend_trigger.extension_in_atr(_ctx(102.0, 100.0, 2.0), "short") == pytest.approx(-1.0)


def test_zero_atr_is_not_a_division_by_zero():
    assert trend_trigger.extension_in_atr(_ctx(102.0, 100.0, 0.0), "long") is None


# ── собственно правило ──────────────────────────────────────────────────────
def test_entry_near_support_is_allowed():
    decision = trend_trigger.evaluate(_tfs(100.5, 100.0, 1.0),
                                      regime="trend_up_candidate", side="long")
    assert decision.allowed
    assert decision.reason == "within_pullback_band"


def test_chasing_a_finished_impulse_is_blocked():
    """Это и есть исходная поломка: покупка в 3 ATR от опоры.

    Условие `h4 trend_up AND h1 trend_up` истинно сутками, зона входа —
    `last ±0.3%`, поэтому вход случался на любом удалении от EMA20.
    """
    decision = trend_trigger.evaluate(_tfs(103.0, 100.0, 1.0),
                                      regime="trend_up_candidate", side="long")
    assert not decision.allowed
    assert decision.reason == "extended_from_ema20"
    assert decision.extension_atr == pytest.approx(3.0)


def test_short_chasing_is_blocked_symmetrically():
    decision = trend_trigger.evaluate(_tfs(97.0, 100.0, 1.0),
                                      regime="trend_down_candidate", side="short")
    assert not decision.allowed
    assert decision.extension_atr == pytest.approx(3.0)


def test_pullback_against_the_trade_is_not_extension():
    """Цена ушла ПРОТИВ направления сделки — это не «догоняем импульс».

    Отрицательная растянутость не должна блокировать вход: правило запрещает
    гнаться за движением, а не входить на откате.
    """
    decision = trend_trigger.evaluate(_tfs(96.0, 100.0, 1.0),
                                      regime="trend_up_candidate", side="long")
    assert decision.allowed
    assert decision.extension_atr < 0


def test_threshold_boundary_is_inclusive():
    at_limit = trend_trigger.evaluate(_tfs(101.5, 100.0, 1.0),
                                      regime="trend_up_candidate", side="long")
    just_over = trend_trigger.evaluate(_tfs(101.51, 100.0, 1.0),
                                       regime="trend_up_candidate", side="long")
    assert at_limit.allowed
    assert not just_over.allowed


# ── границы применимости ────────────────────────────────────────────────────
@pytest.mark.parametrize("regime", ["crt", "scalp", "range", "reversal_long_candidate"])
def test_other_engines_are_untouched(regime):
    """CRT, скальп, range и разворот строят вход от структуры — свипа, микро-края,
    границы коридора, опоры. Растянутость там либо уже учтена, либо не имеет
    смысла, и reversal_long — единственный сетап с доказанным edge."""
    decision = trend_trigger.evaluate(_tfs(110.0, 100.0, 1.0), regime=regime, side="long")
    assert decision.allowed
    assert decision.reason == "not_a_trend_regime"


def test_disabled_flag_is_passthrough(monkeypatch):
    monkeypatch.setattr(settings, "TREND_TRIGGER_ENABLED", False, raising=False)
    decision = trend_trigger.evaluate(_tfs(110.0, 100.0, 1.0),
                                      regime="trend_up_candidate", side="long")
    assert decision.allowed
    assert decision.reason == "disabled"


@pytest.mark.parametrize("contexts", [None, {}, {"15m": {}}, {"15m": {"last_close": 100.0}}])
def test_missing_indicators_fail_open(contexts):
    """Гейт входа не имеет права остановить цикл из-за отсутствующего индикатора."""
    decision = trend_trigger.evaluate(contexts, regime="trend_up_candidate", side="long")
    assert decision.allowed


def test_shadow_mode_measures_without_blocking(monkeypatch):
    """Дефолтный режим: величина считается, вход проходит.

    Порог 1.5 ATR не откалиброван — распределение extension_atr на живых
    сканах неизвестно, потому что до этой правки оно не записывалось.
    Блокировать по неизвестному распределению нельзя.
    """
    monkeypatch.setattr(settings, "TREND_TRIGGER_MODE", "shadow", raising=False)
    decision = trend_trigger.evaluate(_tfs(103.0, 100.0, 1.0),
                                      regime="trend_up_candidate", side="long")
    assert decision.allowed
    assert decision.reason == "extended_from_ema20_shadow"
    assert decision.extension_atr == pytest.approx(3.0)


def test_decision_is_serialisable_for_the_plan():
    """Решение уходит в plan_json: правило нельзя проверить на старых данных,
    оно меняет саму выборку входов. Сравнение возможно только постфактум."""
    payload = trend_trigger.evaluate(_tfs(103.0, 100.0, 1.0),
                                     regime="trend_up_candidate", side="long").as_dict()
    assert payload["extension_atr"] == pytest.approx(3.0)
    assert payload["max_extension_atr"] == pytest.approx(1.5)
    assert set(payload) == {"allowed", "reason", "extension_atr",
                            "max_extension_atr", "regime", "side"}
