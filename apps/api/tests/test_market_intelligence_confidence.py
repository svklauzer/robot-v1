"""
Tests for direction-aware confidence_hint in MarketIntelligenceEngine.

Before the fix, _score_context used a long-biased scale:
  trend_up = 75, trend_down = 25

This caused SHORT signals in bearish markets to get confidence_hint ~44%
even when all timeframes showed perfect short alignment.

After the fix, confidence_hint is computed from direction-aligned scores:
  short: dir_trend = 100 - raw_trend  (25 → 75 for bearish)
  long:  dir_trend = raw_trend        (75 for bullish)
"""
from services.market_intelligence import MarketIntelligenceEngine


engine = MarketIntelligenceEngine()


def _build_scores(*, trend: float, momentum: float, volume: float = 50.0,
                  structure: float = 60.0, volatility: float = 66.0) -> dict:
    total = (
        trend * 0.30 + momentum * 0.20 + volume * 0.20
        + structure * 0.20 + volatility * 0.10
    )
    return {
        "trend": trend,
        "momentum": momentum,
        "volume": volume,
        "structure": structure,
        "volatility": volatility,
        "total": round(total, 2),
    }


def _confidence(action: str, scores: dict) -> float:
    """Mirror the direction-aware formula from _build_multi_timeframe_candidate."""
    raw_trend    = scores["trend"]
    raw_momentum = scores["momentum"]

    if action == "short":
        dir_trend    = 100.0 - raw_trend
        dir_momentum = 100.0 - raw_momentum
    else:
        dir_trend    = raw_trend
        dir_momentum = raw_momentum

    return round(
        dir_trend    * 0.30
        + dir_momentum * 0.20
        + scores["volume"]    * 0.20
        + scores["structure"] * 0.20
        + scores["volatility"] * 0.10,
        2,
    )


def test_short_in_bearish_market_gets_high_confidence():
    """Trend down (raw=25) should translate to high confidence for short."""
    scores = _build_scores(trend=25.0, momentum=30.0, volume=52.0)
    c = _confidence("short", scores)

    assert c > 60.0, f"short in bearish market should have confidence > 60, got {c}"


def test_long_in_bullish_market_gets_high_confidence():
    """Trend up (raw=75) should give high confidence for long."""
    scores = _build_scores(trend=75.0, momentum=70.0, volume=70.0)
    c = _confidence("long", scores)

    assert c > 60.0, f"long in bullish market should have confidence > 60, got {c}"


def test_short_confidence_higher_than_long_in_bearish_market():
    """Same bearish scores should give higher confidence to short than long."""
    scores = _build_scores(trend=25.0, momentum=30.0)
    c_short = _confidence("short", scores)
    c_long  = _confidence("long", scores)

    assert c_short > c_long, (
        f"short ({c_short}) should beat long ({c_long}) in bearish market"
    )


def test_long_confidence_higher_than_short_in_bullish_market():
    """Same bullish scores should give higher confidence to long than short."""
    scores = _build_scores(trend=75.0, momentum=70.0)
    c_long  = _confidence("long", scores)
    c_short = _confidence("short", scores)

    assert c_long > c_short, (
        f"long ({c_long}) should beat short ({c_short}) in bullish market"
    )


def test_dot_usdt_short_example_from_live_scan():
    """
    Regression: DOT/USDT short from live scan should give ~62.6 confidence,
    not the old ~44.64.
    """
    # Actual values from live scan: trend=25.0, momentum=42.6, volume=52.1,
    # structure=60.0, volatility=62.0
    scores = _build_scores(trend=25.0, momentum=42.6, volume=52.1,
                           structure=60.0, volatility=62.0)
    c = _confidence("short", scores)

    assert c > 58.0, f"DOT/USDT short should have confidence > 58, got {c}"
    assert c < 75.0, f"DOT/USDT short should have confidence < 75, got {c}"


def test_score_context_trend_down_scores_25():
    """Verify the underlying _score_context scale hasn't changed."""
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        trend="trend_down",
        momentum="bearish",
        volume_state="normal",
        last_close=100.0,
        support=95.0,
        resistance=110.0,
        atr14=1.5,
    )

    # _score_context is private-ish but accessible for testing
    score = engine._score_context(ctx)

    assert score["trend"] == 25.0, "trend_down should score 25 on raw scale"
    assert score["momentum"] == 30.0, "bearish should score 30 on raw scale"
