"""Динамический TP2 для тренда (#trend-tp2-dynamic-2026-08-27).

Множитель TP2 растёт с силой ЖИВОГО тренда (ADX / расширение ATR / наклон
KAMA), источник — ТОЛЬКО индикаторы текущего рынка. Два прошлых похожих
эксперимента (services/setup_reach.py — сужение по историческим MFE-квантилям;
и ранняя версия market_intelligence._fallback_tp1_pct — цель, выведенная из
той же статистики, которой её проверяет tp_reachability.evaluate()) были
измерены на реальных сделках и откачены: первый резал прибыльные хвосты,
второй создавал циклическую блокировку. Тесты здесь фиксируют, что новая
механика структурно не может повторить ни одну из этих ошибок.
"""
from __future__ import annotations

import inspect

import pytest

from core.config import settings
from services.market_intelligence import MarketIntelligenceEngine


def _engine(symbol: str = "BTC/USDT"):
    eng = MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)
    eng._cur_symbol = symbol
    eng._vp_cache = {}
    return eng


def _ctx(**overrides):
    base = {
        "last_close": 100.0,
        "atr14": 1.0,
        "atr14_prev": 1.0,
        "adx14": 23.0,
        "kama": 100.0,
        "kama_prev": 100.0,
    }
    base.update(overrides)
    return base


def test_disabled_by_default_returns_base_r_mult(monkeypatch):
    monkeypatch.setattr(settings, "TREND_TP2_DYNAMIC_ENABLED", False, raising=False)
    eng = _engine()
    r_mult, meta = eng._dynamic_tp2_r_mult(
        {"1h": _ctx(adx14=60.0, atr14=2.0)}, side="long", base_r_mult=3.2)
    assert r_mult == pytest.approx(3.2)
    assert meta["source"] == "disabled"


def test_strong_trend_widens_beyond_base(monkeypatch):
    monkeypatch.setattr(settings, "TREND_TP2_DYNAMIC_ENABLED", True, raising=False)
    eng = _engine()
    strong_ctx = _ctx(adx14=50.0, atr14=1.3, atr14_prev=1.0, kama=101.0, kama_prev=100.0)
    r_mult, meta = eng._dynamic_tp2_r_mult({"1h": strong_ctx}, side="long", base_r_mult=3.2)
    assert r_mult > 3.2
    assert r_mult <= float(settings.TREND_TP2_DYNAMIC_MAX_R_MULT)
    assert meta["strength"] > 0.0


def test_weak_choppy_trend_stays_at_floor(monkeypatch):
    monkeypatch.setattr(settings, "TREND_TP2_DYNAMIC_ENABLED", True, raising=False)
    eng = _engine()
    flat_ctx = _ctx(adx14=23.0, atr14=1.0, atr14_prev=1.0, kama=100.0, kama_prev=100.0)
    r_mult, meta = eng._dynamic_tp2_r_mult({"1h": flat_ctx}, side="long", base_r_mult=3.2)
    assert r_mult == pytest.approx(3.2)
    assert meta["strength"] == pytest.approx(0.0)


def test_never_below_base_r_mult(monkeypatch):
    """Даже при неблагоприятных индикаторах (ADX ниже порога, ATR сжимается,
    KAMA идёт против стороны сделки) множитель не опускается ниже базового —
    компоненты клэмпятся в 0, а не уходят в отрицательную зону."""
    monkeypatch.setattr(settings, "TREND_TP2_DYNAMIC_ENABLED", True, raising=False)
    eng = _engine()
    adverse_ctx = _ctx(adx14=5.0, atr14=0.5, atr14_prev=1.0, kama=99.0, kama_prev=100.0)
    r_mult, _meta = eng._dynamic_tp2_r_mult({"1h": adverse_ctx}, side="long", base_r_mult=3.2)
    assert r_mult == pytest.approx(3.2)


def test_never_exceeds_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "TREND_TP2_DYNAMIC_ENABLED", True, raising=False)
    eng = _engine()
    extreme_ctx = _ctx(adx14=100.0, atr14=10.0, atr14_prev=1.0, kama=130.0, kama_prev=100.0)
    r_mult, meta = eng._dynamic_tp2_r_mult({"1h": extreme_ctx}, side="long", base_r_mult=3.2)
    assert r_mult == pytest.approx(float(settings.TREND_TP2_DYNAMIC_MAX_R_MULT))
    assert meta["strength"] == pytest.approx(1.0)


def test_monotonic_in_adx(monkeypatch):
    monkeypatch.setattr(settings, "TREND_TP2_DYNAMIC_ENABLED", True, raising=False)
    eng = _engine()
    prev_r = None
    for adx in (23.0, 30.0, 40.0, 50.0):
        ctx = _ctx(adx14=adx, atr14=1.0, atr14_prev=1.0, kama=100.0, kama_prev=100.0)
        r_mult, _meta = eng._dynamic_tp2_r_mult({"1h": ctx}, side="long", base_r_mult=3.2)
        if prev_r is not None:
            assert r_mult >= prev_r
        prev_r = r_mult


def test_source_string_is_auditable(monkeypatch):
    monkeypatch.setattr(settings, "TREND_TP2_DYNAMIC_ENABLED", True, raising=False)
    eng = _engine()
    ctx = _ctx(adx14=45.0, atr14=1.2, atr14_prev=1.0, kama=101.0, kama_prev=100.0)
    _r_mult, meta = eng._dynamic_tp2_r_mult({"1h": ctx}, side="long", base_r_mult=3.2)
    for token in ("adx=", "atr=", "kama=", "strength="):
        assert token in meta["source"]


def test_never_reads_tp_reachability_or_ml_trade_logger():
    """Регрессионная защита от циклической ошибки: _dynamic_tp2_r_mult не
    имеет права читать исторический MFE/outcomes журнал, которым
    tp_reachability.evaluate() уже проверяет достижимость цели — иначе
    получится «цель проверяется тем же, из чего выведена» (см. докстринг
    метода и tests/test_tp1_from_measured_move.py про прошлый провал).

    Проверяем ТЕЛО функции (без докстринга — в нём это предостережение как
    раз упоминается словами), на реальные признаки использования: импорт
    модуля, обращение к его атрибутам/функциям."""
    fn = MarketIntelligenceEngine._dynamic_tp2_r_mult
    body_src = inspect.getsource(fn).split('"""', 2)[-1]  # после закрывающих """ докстринга
    forbidden = (
        "tp_reachability", "ml_trade_logger", "MLTradeLogger",
        "mfe_pct", "trade_outcomes",
    )
    for token in forbidden:
        assert token not in body_src, f"_dynamic_tp2_r_mult must not reference {token!r}"


def test_build_long_levels_tp2_widens_with_strong_trend(monkeypatch):
    """Сквозной тест через _build_long_levels: включённая динамика в сильном
    тренде даёт TP2 дальше, чем при выключенной, и инвариант tp2 > tp1
    сохраняется."""
    eng = _engine()
    contexts = {
        "5m": {"last_close": 100.0, "atr14": 1.0, "support": 95.0, "resistance": None},
        "15m": {"atr14": 1.0, "support": 95.0, "resistance": None},
        "1h": {
            "last_close": 100.0, "atr14": 1.0, "atr14_prev": 1.0,
            "adx14": 50.0, "kama": 101.0, "kama_prev": 100.0,
            "support": 95.0, "resistance": None,
        },
    }

    monkeypatch.setattr(settings, "TREND_TP2_DYNAMIC_ENABLED", False, raising=False)
    baseline = eng._build_long_levels(contexts)

    monkeypatch.setattr(settings, "TREND_TP2_DYNAMIC_ENABLED", True, raising=False)
    dynamic = eng._build_long_levels(contexts)

    assert dynamic["tp"]["tp2"] > baseline["tp"]["tp2"]
    assert dynamic["tp"]["tp2"] > dynamic["tp"]["tp1"]
    assert dynamic["tp2_dynamic"]["r_mult"] > baseline["tp2_dynamic"]["r_mult"]


def test_multi_timeframe_candidate_surfaces_tp2_dynamic_in_setup_quality(monkeypatch):
    """(#tp2-dynamic-audit-2026-08-31) _build_long/short_levels() считает
    tp2_dynamic, но в проде setup_quality строится ОТДЕЛЬНО через
    _score_setup_quality() — levels["tp2_dynamic"] раньше нигде не оседал,
    и ни телеметрия (`tp2_reached_too_rarely`), ни /signals не могли
    показать, раздула ли динамика TP2 сверх исторически достижимого, или
    цель и так была недостижима. Проверяем именно боевой путь
    (_build_multi_timeframe_candidate), а не _build_long_levels напрямую —
    предыдущий тест уже проверил последний и не поймал бы эту потерю."""
    eng = _engine()
    monkeypatch.setattr(
        eng, "_detect_radar_state", lambda **kw: "none", raising=False)
    monkeypatch.setattr(
        eng, "_build_long_levels",
        lambda contexts: {
            "entry_zone": [99.0, 100.0],
            "stop_price": 95.0,
            "tp": {"tp1": 102.0, "tp2": 108.0},
            "tp2_dynamic": {"r_mult": 4.5, "source": "adx=50.0 strength=0.9"},
        },
        raising=False,
    )
    monkeypatch.setattr(
        eng, "_score_setup_quality",
        lambda **kw: {"decision": "approve", "final_score": 80.0},
        raising=False,
    )

    result = eng._build_multi_timeframe_candidate(
        symbol="BTC/USDT",
        source="mtf",
        contexts={"15m": _ctx()},
        scores={"trend": 75.0, "momentum": 70.0, "volume": 50.0,
                "structure": 50.0, "volatility": 50.0, "total": 60.0},
        regime="trend_up_candidate",
    )

    assert result.setup_quality is not None
    assert "tp2_dynamic" in result.setup_quality, (
        "tp2_dynamic посчитан в levels, но потерян по пути в setup_quality — "
        "именно это скрывало причину блокировок tp2_reached_too_rarely")
    assert result.setup_quality["tp2_dynamic"]["r_mult"] == 4.5


def test_hold_candidate_does_not_crash_without_levels(monkeypatch):
    """regime вне trend_up/down_candidate (hold/mixed/flat) никогда не строит
    levels — merge не должен падать на NameError, когда его нет."""
    eng = _engine()
    monkeypatch.setattr(
        eng, "_detect_radar_state", lambda **kw: "none", raising=False)
    monkeypatch.setattr(
        eng, "_score_setup_quality",
        lambda **kw: {"decision": "hold", "final_score": 40.0},
        raising=False,
    )

    result = eng._build_multi_timeframe_candidate(
        symbol="BTC/USDT",
        source="mtf",
        contexts={"15m": _ctx()},
        scores={"trend": 50.0, "momentum": 50.0, "volume": 50.0,
                "structure": 50.0, "volatility": 50.0, "total": 50.0},
        regime="mixed",
    )

    assert result.action == "hold"
    assert "tp2_dynamic" not in result.setup_quality
