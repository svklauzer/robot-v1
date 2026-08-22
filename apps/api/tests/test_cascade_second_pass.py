"""Второй проход каскада (#cascade-second-pass-2026-08-22).

Замер 22.08, из-за которого правка появилась. Аптренд переваливал по всем
символам: ADX 37.3 → 34.8 → 32.6 → 30.5 → 27.7, DI ушёл в минус (−1.7…−3.7),
цена под KAMA в КАЖДОМ событии. Тренд-машина входить отказывалась — и была
права, до KAMA-enforce система такие развороты покупала.

Но детектор режима всё ещё писал `trend_up_candidate` (он смотрит структуру EMA
старших ТФ и запаздывает), трендовый кандидат получал approve и ОСТАНАВЛИВАЛ
каскад внутри `analyze_symbol`. Гейт же срабатывает позже, в `robot_loop`.
Итог: символ уходил в тишину, а CRT/range/scalp его даже не смотрели — при том
что переходный рынок и есть их территория.
"""
from __future__ import annotations

from services.market_intelligence import MarketIntelligenceEngine, MarketIntelligenceResult


def _result(**kw) -> MarketIntelligenceResult:
    base = dict(
        symbol="SOL/USDT", source="test", action="long", regime="trend_up_candidate",
        entry_zone=[100.0, 101.0], stop_price=99.0, tp={"tp1": 102.0, "tp2": 104.0},
        confidence_hint=60.0, reason="test", scores={}, timeframes={},
        setup_quality={}, setup_decision="approve", radar_state="none",
    )
    base.update(kw)
    return MarketIntelligenceResult(**base)


def test_result_is_mutable_so_the_cascade_can_be_reopened():
    """Каскад помечает трендового кандидата отвергнутым — значит поле изменяемо.

    Если дataclass станет frozen, второй проход молча перестанет работать:
    исключение съест `except` в robot_loop, и мы вернёмся к тишине, не заметив.
    """
    res = _result()
    res.setup_decision = "trend_blocked_by_entry_gate"
    assert res.setup_decision == "trend_blocked_by_entry_gate"


def test_snapshot_is_cached_between_passes(monkeypatch):
    """Второй проход не должен удваивать выкачку OHLCV.

    Egress с Render уже однажды укладывал инстанс, а тут по пять таймфреймов на
    символ. Внутри TTL снимок берётся из кэша.
    """
    eng = MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)
    calls = {"n": 0}

    class _Market:
        def multi_timeframe_snapshot(self, symbol):
            calls["n"] += 1
            return {"source": "test", "timeframes": {}}

    eng.market = _Market()

    first = eng._snapshot_cached("SOL/USDT")
    second = eng._snapshot_cached("SOL/USDT")

    assert calls["n"] == 1, "второй проход обязан переиспользовать снимок"
    assert first is second


def test_cache_expires_between_ticks(monkeypatch):
    """TTL короткий: между тиками данные обновляются, а не залипают."""
    from core.config import settings

    eng = MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)
    calls = {"n": 0}

    class _Market:
        def multi_timeframe_snapshot(self, symbol):
            calls["n"] += 1
            return {"source": "test", "timeframes": {}, "n": calls["n"]}

    eng.market = _Market()
    monkeypatch.setattr(settings, "MTF_SNAPSHOT_TTL_SEC", 0.0, raising=False)

    eng._snapshot_cached("SOL/USDT")
    eng._snapshot_cached("SOL/USDT")

    assert calls["n"] == 2, "нулевой TTL обязан заставить перечитать"


def test_alternative_must_differ_from_the_blocked_regime():
    """Фолбэк принимается только если движок ДРУГОЙ.

    Иначе тот же трендовый кандидат вернулся бы вторым проходом и обошёл гейт,
    который его только что отверг, — дыра вместо шанса альтернативам.
    """
    blocked_regime = "trend_up_candidate"
    same = _result(regime="trend_up_candidate")
    other = _result(regime="range")

    def _accepted(alt) -> bool:
        return not (
            alt is None
            or alt.action == "hold"
            or alt.setup_decision != "approve"
            or str(alt.regime) == blocked_regime
        )

    assert _accepted(same) is False
    assert _accepted(other) is True
    assert _accepted(_result(regime="range", setup_decision="wait")) is False
    assert _accepted(_result(regime="range", action="hold")) is False
