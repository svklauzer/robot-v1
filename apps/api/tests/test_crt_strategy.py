"""CRT: динамический TP2 (#crt-tp2-dynamic-2026-08-27) и ревert строгости
CRT_LTF_CONFIRM (#audit-2026-08-27).

CRT_LTF_CONFIRM был тихо ужесточён с "either" на "both" в commit dd05813
(06.08) без обоснования, при том что более поздняя правка (render.yaml)
явно старалась "снизить планку" для CRT — но этот параметр не тронула.
CRT почти не торговал с 06.08. Возвращён на "either" в config.py.

Динамический TP2 зеркалит market_intelligence._dynamic_tp2_r_mult: источник
ТОЛЬКО живые индикаторы (ADX/ATR-expansion на HTF), НЕ исторический MFE.
Эффект есть только при CRT_TARGETS_MODE="extended" — в "range" (инструкция)
TP2 структурный (CRH/CRL), rr не участвует.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services.crt_strategy import CRTStrategyService, _dynamic_tp2_rr


def test_crt_ltf_confirm_default_is_either():
    """Регрессионная защита: значение не должно снова тихо стать "both"."""
    assert str(settings.CRT_LTF_CONFIRM).lower() == "either"


# ── _dynamic_tp2_rr: юнит-тесты ──────────────────────────────────────────

def test_dynamic_rr_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "CRT_TP2_DYNAMIC_ENABLED", False, raising=False)
    rr, meta = _dynamic_tp2_rr(2.0, adx=60.0, atr_ratio=2.0)
    assert rr == pytest.approx(2.0)
    assert meta["source"] == "disabled"


def test_dynamic_rr_widens_with_adx_and_atr(monkeypatch):
    monkeypatch.setattr(settings, "CRT_TP2_DYNAMIC_ENABLED", True, raising=False)
    rr, meta = _dynamic_tp2_rr(2.0, adx=50.0, atr_ratio=1.3)
    assert rr > 2.0
    assert rr <= float(settings.CRT_TP2_DYNAMIC_MAX_RR)
    assert meta["source"].startswith("dynamic(")


def test_dynamic_rr_never_below_base(monkeypatch):
    monkeypatch.setattr(settings, "CRT_TP2_DYNAMIC_ENABLED", True, raising=False)
    rr, _meta = _dynamic_tp2_rr(2.0, adx=0.0, atr_ratio=0.5)
    assert rr == pytest.approx(2.0)


def test_dynamic_rr_never_exceeds_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "CRT_TP2_DYNAMIC_ENABLED", True, raising=False)
    rr, _meta = _dynamic_tp2_rr(2.0, adx=100.0, atr_ratio=10.0)
    assert rr == pytest.approx(float(settings.CRT_TP2_DYNAMIC_MAX_RR))


def test_dynamic_rr_monotonic_in_adx(monkeypatch):
    monkeypatch.setattr(settings, "CRT_TP2_DYNAMIC_ENABLED", True, raising=False)
    prev = None
    for adx in (23.0, 30.0, 40.0, 50.0):
        rr, _meta = _dynamic_tp2_rr(2.0, adx=adx, atr_ratio=1.0)
        if prev is not None:
            assert rr >= prev
        prev = rr


# ── evaluate(): сквозные тесты ───────────────────────────────────────────

def _bullish_crt_fixture():
    c1 = {"open": 105.0, "high": 110.0, "low": 100.0, "close": 108.0}
    c2 = {"open": 101.0, "high": 103.0, "low": 98.0, "close": 102.0}
    htf = [c1, c2]
    ltf = [
        {"open": 99.5, "high": 101.0, "low": 99.0, "close": 100.0},
        {"open": 99.5, "high": 101.0, "low": 99.0, "close": 100.0},
        {"open": 99.5, "high": 101.0, "low": 99.0, "close": 100.0},
        {"open": 100.0, "high": 101.5, "low": 99.5, "close": 100.5},
        {"open": 100.5, "high": 103.0, "low": 100.0, "close": 102.0},
    ]
    return htf, ltf


def _base_settings(monkeypatch):
    monkeypatch.setattr(settings, "CRT_STOP_BUFFER_PCT", 0.05, raising=False)
    monkeypatch.setattr(settings, "CRT_MIN_RR_TP1", 1.0, raising=False)
    monkeypatch.setattr(settings, "CRT_TP2_RR", 2.0, raising=False)
    monkeypatch.setattr(settings, "CRT_MIN_TP1_NET_PCT", 0.0, raising=False)
    monkeypatch.setattr(settings, "CRT_MIN_SETUP_SCORE", 0.0, raising=False)
    monkeypatch.setattr(settings, "CRT_LTF_CONFIRM", "either", raising=False)
    monkeypatch.setattr(settings, "CRT_MIN_RANGE_PCT", 1.5, raising=False)


def test_targets_mode_extended_widens_tp2_with_strong_trend(monkeypatch):
    _base_settings(monkeypatch)
    monkeypatch.setattr(settings, "CRT_TARGETS_MODE", "extended", raising=False)
    htf, ltf = _bullish_crt_fixture()
    kwargs = dict(
        current_price=101.0, htf_trend="mixed", mtf_trend="mixed",
        htf_momentum="neutral", mtf_momentum="neutral",
    )

    monkeypatch.setattr(settings, "CRT_TP2_DYNAMIC_ENABLED", False, raising=False)
    base_sig = CRTStrategyService().evaluate(htf, ltf, **kwargs)
    assert base_sig is not None
    assert base_sig.action == "long"

    monkeypatch.setattr(settings, "CRT_TP2_DYNAMIC_ENABLED", True, raising=False)
    dyn_sig = CRTStrategyService().evaluate(
        htf, ltf, **kwargs, htf_adx=50.0, htf_atr_ratio=1.3)
    assert dyn_sig is not None

    assert dyn_sig.tp["tp2"] > base_sig.tp["tp2"]
    assert dyn_sig.tp["tp2"] > dyn_sig.tp["tp1"]


def test_targets_mode_range_ignores_dynamic_rr(monkeypatch):
    """С default-инструкцией CRT_TARGETS_MODE="range" TP2 структурный
    (CRH/CRL) — динамический RR не должен на него влиять вообще."""
    _base_settings(monkeypatch)
    monkeypatch.setattr(settings, "CRT_TARGETS_MODE", "range", raising=False)
    htf, ltf = _bullish_crt_fixture()
    kwargs = dict(
        current_price=101.0, htf_trend="mixed", mtf_trend="mixed",
        htf_momentum="neutral", mtf_momentum="neutral",
    )

    monkeypatch.setattr(settings, "CRT_TP2_DYNAMIC_ENABLED", False, raising=False)
    base_sig = CRTStrategyService().evaluate(htf, ltf, **kwargs)
    assert base_sig is not None

    monkeypatch.setattr(settings, "CRT_TP2_DYNAMIC_ENABLED", True, raising=False)
    dyn_sig = CRTStrategyService().evaluate(
        htf, ltf, **kwargs, htf_adx=90.0, htf_atr_ratio=5.0)
    assert dyn_sig is not None

    assert dyn_sig.tp["tp2"] == pytest.approx(base_sig.tp["tp2"])
    assert dyn_sig.tp["tp2"] == pytest.approx(110.0)  # crh, структурная цель
