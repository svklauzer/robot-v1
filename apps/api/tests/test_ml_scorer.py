"""Tests for MLScorer v2 — multi-factor scorer."""
from services.ml_scorer import MLScorer


def _features(**overrides):
    base = {
        "last_close": 100.0,
        "ema20": 99.0,
        "ema50": 98.0,
        "volume": 1400.0,
        "volume_ma": 1000.0,
        "rsi": 55.0,
        "macd_hist": 0.30,
        "macd_hist_prev": 0.20,
    }
    base.update(overrides)
    return base


def test_ml_scorer_returns_mlscore_with_valid_ranges():
    result = MLScorer().score(_features(), regime="trend_up")

    assert 0.35 <= result.probability <= 0.95
    assert result.confidence == round(result.probability * 100, 2)
    assert result.multiplier in (1.0, 1.25, 1.50)


def test_ml_scorer_trend_up_strong_alignment_gives_high_score():
    """All bullish signals aligned — should score well above base floor."""
    result = MLScorer().score(
        _features(last_close=101.0, ema20=100.0, ema50=99.0, rsi=58.0,
                  macd_hist=0.4, macd_hist_prev=0.2, volume=1500.0, volume_ma=1000.0),
        regime="trend_up",
        grade="A",
    )
    assert result.probability >= 0.70, f"expected ≥0.70, got {result.probability}"


def test_ml_scorer_trend_up_overbought_rsi_penalty():
    """RSI > 68 in trend_up should reduce score vs neutral RSI."""
    normal = MLScorer().score(_features(rsi=55.0), regime="trend_up")
    overbought = MLScorer().score(_features(rsi=72.0), regime="trend_up")

    assert overbought.probability < normal.probability


def test_ml_scorer_trend_down_strong_alignment_gives_high_score():
    features = _features(
        last_close=97.0, ema20=99.0, ema50=100.0,
        rsi=45.0, macd_hist=-0.4, macd_hist_prev=-0.2,
        volume=1500.0, volume_ma=1000.0,
    )
    result = MLScorer().score(features, regime="trend_down", grade="A")
    assert result.probability >= 0.70


def test_ml_scorer_weak_volume_reduces_score():
    strong_vol = MLScorer().score(_features(volume=1500.0, volume_ma=1000.0), regime="trend_up")
    weak_vol   = MLScorer().score(_features(volume=700.0,  volume_ma=1000.0), regime="trend_up")

    assert weak_vol.probability < strong_vol.probability


def test_ml_scorer_grade_c_penalty_lowers_confidence():
    base   = MLScorer().score(_features(), regime="trend_up", grade="A")
    grade_c = MLScorer().score(_features(), regime="trend_up", grade="C")

    assert grade_c.probability < base.probability


def test_ml_scorer_grade_a_plus_boost_raises_confidence():
    grade_a    = MLScorer().score(_features(), regime="trend_up", grade="A")
    grade_a_plus = MLScorer().score(_features(), regime="trend_up", grade="A+")

    assert grade_a_plus.probability >= grade_a.probability


def test_ml_scorer_floor_prevents_zero_probability():
    """Even the worst possible inputs must not go below BASE_FLOOR."""
    worst = _features(
        last_close=95.0, ema20=99.0, ema50=100.0,  # price below EMAs for trend_up
        rsi=75.0,                                    # overbought
        macd_hist=-0.5, macd_hist_prev=-0.2,        # MACD against direction
        volume=500.0, volume_ma=1000.0,              # weak volume
    )
    result = MLScorer().score(worst, regime="trend_up", grade="C")

    assert result.probability >= MLScorer.BASE_FLOOR


def test_ml_scorer_multiplier_tiers():
    """Multiplier should increase with probability tier."""
    # Force a high probability via ideal conditions
    ideal = _features(
        last_close=101.0, ema20=100.0, ema50=99.0,
        rsi=58.0, macd_hist=0.5, macd_hist_prev=0.3,
        volume=2000.0, volume_ma=1000.0,
    )
    high = MLScorer().score(ideal, regime="trend_up", grade="A+")
    low  = MLScorer().score(_features(rsi=72.0, macd_hist=-0.1, volume=700.0, volume_ma=1000.0),
                            regime="trend_up", grade="C")

    # High score should have higher or equal multiplier
    assert high.multiplier >= low.multiplier
