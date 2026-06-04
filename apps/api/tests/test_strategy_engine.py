from services.strategy_engine import StrategyEngine


def _features(**overrides):
    payload = {
        "last_close": 100.0,
        "ema20": 99.0,
        "ema50": 98.0,
        "atr": 1.0,
        "volume": 1301.0,
        "volume_ma": 1000.0,
        "rsi": 55.0,
        "macd_hist": 0.25,
        "macd_hist_prev": 0.10,
    }
    payload.update(overrides)
    return payload


def test_strategy_engine_v2_builds_positive_rr_long_levels():
    signal = StrategyEngine().generate_signal("BTC/USDT", _features(), "trend_up")

    assert signal["action"] == "long"
    assert signal["reason"] == "trend_volume_breakout_v2"
    assert signal["entry_zone"] == [99.9, 100.1]
    assert signal["stop_price"] == 98.8
    assert signal["tp"] == {"tp1": 102.0, "tp2": 103.5}


def test_strategy_engine_v2_filters_overbought_and_weak_volume():
    overbought = StrategyEngine().generate_signal("BTC/USDT", _features(rsi=70.0), "trend_up")
    weak_volume = StrategyEngine().generate_signal("BTC/USDT", _features(volume=1200.0), "trend_up")

    assert overbought == {"action": "hold", "reason": "long_rsi_overbought"}
    assert weak_volume == {"action": "hold", "reason": "weak_volume"}


def test_strategy_engine_v2_filters_missing_macd_confirmation():
    signal = StrategyEngine().generate_signal("BTC/USDT", _features(macd_hist=0.05, macd_hist_prev=0.10), "trend_up")

    assert signal == {"action": "hold", "reason": "long_macd_no_confirm"}


def test_strategy_engine_v2_builds_positive_rr_short_levels():
    features = _features(last_close=100.0, ema20=101.0, ema50=102.0, rsi=45.0, macd_hist=-0.25, macd_hist_prev=-0.10)
    signal = StrategyEngine().generate_signal("BTC/USDT", features, "trend_down")

    assert signal["action"] == "short"
    assert signal["reason"] == "trend_volume_breakdown_v2"
    assert signal["stop_price"] == 101.2
    assert signal["tp"] == {"tp1": 98.0, "tp2": 96.5}
