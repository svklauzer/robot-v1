import pandas as pd


class StrategyEngine:
    """Strategy Engine v2: positive-expectancy ATR targets plus signal filters."""

    def _ema(self, series: pd.Series, span: int):
        return series.ewm(span=span, adjust=False).mean()

    def _atr(self, df: pd.DataFrame, period: int = 14):
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def _rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, float("nan"))
        return 100 - (100 / (1 + rs))

    def _macd(self, series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
        ema12 = self._ema(series, 12)
        ema26 = self._ema(series, 26)
        macd_line = ema12 - ema26
        signal_line = self._ema(macd_line, 9)
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    def detect_regime(self, df: pd.DataFrame) -> str:
        ema20 = self._ema(df["close"], 20)
        ema50 = self._ema(df["close"], 50)
        atr = self._atr(df).iloc[-1]
        last = df["close"].iloc[-1]

        if pd.isna(atr) or atr < last * 0.005:
            return "flat"
        if ema20.iloc[-1] > ema50.iloc[-1]:
            return "trend_up"
        if ema20.iloc[-1] < ema50.iloc[-1]:
            return "trend_down"
        return "neutral"

    def build_features(self, df: pd.DataFrame) -> dict:
        ema20 = self._ema(df["close"], 20)
        ema50 = self._ema(df["close"], 50)
        ema200 = self._ema(df["close"], 200) if len(df) >= 200 else ema50
        atr = self._atr(df).iloc[-1]
        volume_ma = df["volume"].rolling(20).mean().iloc[-1]
        rsi = self._rsi(df["close"]).iloc[-1]
        macd_line, signal_line, histogram = self._macd(df["close"])

        return {
            "last_close": float(df["close"].iloc[-1]),
            "ema20": float(ema20.iloc[-1]),
            "ema50": float(ema50.iloc[-1]),
            "ema200": float(ema200.iloc[-1]),
            "atr": float(atr),
            "volume": float(df["volume"].iloc[-1]),
            "volume_ma": float(volume_ma),
            "rsi": float(rsi) if not pd.isna(rsi) else 50.0,
            "macd": float(macd_line.iloc[-1]),
            "macd_signal": float(signal_line.iloc[-1]),
            "macd_hist": float(histogram.iloc[-1]),
            "macd_hist_prev": float(histogram.iloc[-2]) if len(histogram) >= 2 else 0.0,
        }

    def generate_signal(self, symbol: str, features: dict, regime: str) -> dict:
        if regime == "flat":
            return {"action": "hold", "reason": "flat_market"}

        last_close = features["last_close"]
        ema20 = features["ema20"]
        ema50 = features["ema50"]
        volume = features["volume"]
        volume_ma = features["volume_ma"]
        atr = features["atr"]
        rsi = float(features.get("rsi", 50.0))
        macd_hist = float(features.get("macd_hist", 0.0))
        macd_hist_prev = float(features.get("macd_hist_prev", 0.0))

        atr_pct = atr / last_close * 100 if last_close else 0.0
        if atr_pct < 0.50:
            return {"action": "hold", "reason": "insufficient_volatility"}

        volume_ok = volume > volume_ma * 1.3

        if regime == "trend_up" and last_close > ema20 > ema50 and volume_ok:
            if rsi >= 65:
                return {"action": "hold", "reason": "long_rsi_overbought"}
            if macd_hist <= 0 or macd_hist <= macd_hist_prev:
                return {"action": "hold", "reason": "long_macd_no_confirm"}
            return {
                "action": "long",
                "symbol": symbol,
                "entry_zone": [round(last_close - atr * 0.1, 6), round(last_close + atr * 0.1, 6)],
                "stop_price": round(last_close - atr * 1.2, 6),
                "tp": {"tp1": round(last_close + atr * 2.0, 6), "tp2": round(last_close + atr * 3.5, 6)},
                "reason": "trend_volume_breakout_v2",
            }

        if regime == "trend_down" and last_close < ema20 < ema50 and volume_ok:
            if rsi <= 35:
                return {"action": "hold", "reason": "short_rsi_oversold"}
            if macd_hist >= 0 or macd_hist >= macd_hist_prev:
                return {"action": "hold", "reason": "short_macd_no_confirm"}
            return {
                "action": "short",
                "symbol": symbol,
                "entry_zone": [round(last_close - atr * 0.1, 6), round(last_close + atr * 0.1, 6)],
                "stop_price": round(last_close + atr * 1.2, 6),
                "tp": {"tp1": round(last_close - atr * 2.0, 6), "tp2": round(last_close - atr * 3.5, 6)},
                "reason": "trend_volume_breakdown_v2",
            }

        if not volume_ok:
            return {"action": "hold", "reason": "weak_volume"}
        return {"action": "hold", "reason": "no_setup"}
