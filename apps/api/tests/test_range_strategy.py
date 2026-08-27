"""Range: динамический буфер TP2 (#range-tp2-dynamic-2026-08-27).

Источник ТОЛЬКО живые индикаторы (ADX/ATR-expansion на 1h), НЕ исторический
MFE. Буфер (доля ширины диапазона, на которую TP2 не доходит до дальней
границы) ТОЛЬКО сужается при сильном локальном тренде — TP2 приближается к
границе, но никогда не отдаляется от неё дальше исходного
RANGE_TP2_RESISTANCE_BUFFER.
"""
from __future__ import annotations

import pytest

import core.strategy_profiles as strategy_profiles
from core.config import settings
from services.range_strategy import RangeStrategyService, _dynamic_tp2_buffer_mult


# ── _dynamic_tp2_buffer_mult: юнит-тесты ─────────────────────────────────

def test_dynamic_buffer_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "RANGE_TP2_DYNAMIC_ENABLED", False, raising=False)
    buf, meta = _dynamic_tp2_buffer_mult(0.10, adx=40.0, atr_ratio=2.0)
    assert buf == pytest.approx(0.10)
    assert meta["source"] == "disabled"


def test_dynamic_buffer_shrinks_with_strong_adx_and_atr_expansion(monkeypatch):
    monkeypatch.setattr(settings, "RANGE_TP2_DYNAMIC_ENABLED", True, raising=False)
    buf, meta = _dynamic_tp2_buffer_mult(0.10, adx=30.0, atr_ratio=1.5)
    assert buf < 0.10
    assert buf >= float(settings.RANGE_TP2_DYNAMIC_MIN_BUFFER)
    assert meta["source"].startswith("dynamic(")


def test_dynamic_buffer_never_exceeds_base_buffer(monkeypatch):
    monkeypatch.setattr(settings, "RANGE_TP2_DYNAMIC_ENABLED", True, raising=False)
    buf, _meta = _dynamic_tp2_buffer_mult(0.10, adx=0.0, atr_ratio=0.5)
    assert buf == pytest.approx(0.10)


def test_dynamic_buffer_never_undershoots_min_buffer(monkeypatch):
    monkeypatch.setattr(settings, "RANGE_TP2_DYNAMIC_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "RANGE_TP2_DYNAMIC_MIN_BUFFER", 0.0, raising=False)
    buf, _meta = _dynamic_tp2_buffer_mult(0.10, adx=100.0, atr_ratio=10.0)
    assert buf == pytest.approx(0.0)


# ── evaluate(): сквозной тест ─────────────────────────────────────────────

def _configure_range(monkeypatch, *, dynamic_enabled: bool):
    monkeypatch.setattr(settings, "ENABLE_RANGE_STRATEGY", True, raising=False)
    monkeypatch.setattr(settings, "RANGE_MIN_WIDTH_PCT", 1.8, raising=False)
    monkeypatch.setattr(settings, "RANGE_SUPPORT_ZONE", 0.30, raising=False)
    monkeypatch.setattr(settings, "RANGE_ENTRY_RSI_MIN", 25.0, raising=False)
    monkeypatch.setattr(settings, "RANGE_ENTRY_RSI_MAX", 52.0, raising=False)
    monkeypatch.setattr(settings, "RANGE_MIN_TP1_NET_PCT", 0.0, raising=False)
    monkeypatch.setattr(settings, "RANGE_TP2_RESISTANCE_BUFFER", 0.10, raising=False)
    monkeypatch.setattr(settings, "RANGE_STOP_ATR_MULT", 2.5, raising=False)
    monkeypatch.setattr(settings, "RANGE_MIN_SETUP_SCORE", 0.0, raising=False)
    monkeypatch.setattr(settings, "RANGE_ALLOW_SHORT", True, raising=False)
    monkeypatch.setattr(settings, "RANGE_CONFIRMED_ONLY", True, raising=False)
    monkeypatch.setattr(settings, "RANGE_TP2_DYNAMIC_ENABLED", dynamic_enabled, raising=False)
    monkeypatch.setattr(settings, "RANGE_TP2_DYNAMIC_MIN_BUFFER", 0.0, raising=False)
    monkeypatch.setattr(settings, "RANGE_TP2_DYNAMIC_ADX_BASE", 15.0, raising=False)
    monkeypatch.setattr(settings, "RANGE_TP2_DYNAMIC_ADX_SPAN", 15.0, raising=False)
    # RangeEngine.load() кэшируется в core.strategy_profiles._cache — сбрасываем,
    # иначе monkeypatch выше не долетит до RangeStrategyService.evaluate().
    monkeypatch.setattr(strategy_profiles, "_cache", None, raising=False)


def _range_contexts(*, adx14: float, atr_ratio: float):
    return {
        "4h": {"trend": "mixed"},
        "15m": {"trend": "mixed", "rsi14": 40.0},
        "1h": {
            "support": 95.0, "resistance": 105.0, "last_close": 96.0,
            "atr14": 1.3, "atr14_prev": 1.3 / atr_ratio if atr_ratio else 1.3,
            "adx14": adx14,
        },
    }


def test_range_evaluate_tp2_moves_closer_to_edge_in_strong_local_trend(monkeypatch):
    _configure_range(monkeypatch, dynamic_enabled=False)
    baseline = RangeStrategyService().evaluate(_range_contexts(adx14=30.0, atr_ratio=1.5))
    assert baseline is not None
    assert baseline.action == "long"

    _configure_range(monkeypatch, dynamic_enabled=True)
    dynamic = RangeStrategyService().evaluate(_range_contexts(adx14=30.0, atr_ratio=1.5))
    assert dynamic is not None

    # Лонг: TP2 ближе к верхней границе диапазона => дальше от входа => больше цифра.
    assert dynamic.tp["tp2"] > baseline.tp["tp2"]
    assert dynamic.tp["tp2"] <= 105.0  # никогда не дальше самой границы диапазона
