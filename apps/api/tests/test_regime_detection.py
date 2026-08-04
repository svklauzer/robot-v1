"""Классификация режима: сильный тренд не должен теряться из-за перегретого
импульса нижних ТФ, но реальный мультитаймфреймовый конфликт остаётся mixed."""
from services.market_intelligence import MarketIntelligenceEngine, TimeframeContext


def _ctx(tf, trend, momentum, volatility="normal"):
    return TimeframeContext(
        timeframe=tf, last_close=1, ema20=1, ema50=1, ema200=1, rsi14=50,
        macd=0, macd_signal=0, macd_hist=0, atr14=1, volume=1, volume_ma20=1,
        volume_ratio=1, support=0, resistance=0, trend=trend, momentum=momentum,
        volatility=volatility, volume_state="normal",
    )


def _engine():
    return MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)


def test_strong_uptrend_all_tf_overheated_is_trend_candidate():
    """TRX-кейс: все ТФ trend_up, но нижние overheated — раньше падало в mixed."""
    tf = {
        "1m": _ctx("1m", "trend_up", "overheated"),
        "5m": _ctx("5m", "trend_up", "overheated"),
        "15m": _ctx("15m", "trend_up", "overheated"),
        "1h": _ctx("1h", "trend_up", "bullish"),
        "4h": _ctx("4h", "trend_up", "bullish"),
    }
    regime = _engine()._detect_multi_timeframe_regime(
        tf, {"trend": 75.0, "total": 56.67, "volume": 48.1}
    )
    assert regime == "trend_up_candidate"


def test_strong_downtrend_oversold_is_trend_candidate():
    tf = {
        "1m": _ctx("1m", "trend_down", "oversold"),
        "5m": _ctx("5m", "trend_down", "oversold"),
        "15m": _ctx("15m", "trend_down", "oversold"),
        "1h": _ctx("1h", "trend_down", "bearish"),
        "4h": _ctx("4h", "trend_down", "bearish"),
    }
    regime = _engine()._detect_multi_timeframe_regime(
        tf, {"trend": 25.0, "total": 56.0, "volume": 48.0}
    )
    assert regime == "trend_down_candidate"


def test_htf_up_but_ltf_conflict_stays_mixed():
    """AVAX-кейс: 1h/4h вверх, но 1m/5m вниз и 15m mixed — не тренд, а откат/шум."""
    tf = {
        "1m": _ctx("1m", "trend_down", "neutral"),
        "5m": _ctx("5m", "trend_down", "oversold"),
        "15m": _ctx("15m", "mixed", "bearish"),
        "1h": _ctx("1h", "trend_up", "bullish"),
        "4h": _ctx("4h", "trend_up", "bullish"),
    }
    regime = _engine()._detect_multi_timeframe_regime(
        tf, {"trend": 57.0, "total": 54.88, "volume": 50.0}
    )
    assert regime == "mixed"


def test_flat_universe_stays_flat():
    tf = {k: _ctx(k, "flat", "neutral") for k in ("1m", "5m", "15m", "1h", "4h")}
    regime = _engine()._detect_multi_timeframe_regime(
        tf, {"trend": 50.0, "total": 45.0, "volume": 50.0}
    )
    assert regime in ("flat", "mixed")
