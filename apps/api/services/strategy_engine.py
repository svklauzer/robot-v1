import pandas as pd

class StrategyEngine:
    def _ema(self, series: pd.Series, span: int):
        return series.ewm(span=span, adjust=False).mean()

    def _atr(self, df: pd.DataFrame, period: int = 14):
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def detect_regime(self, df: pd.DataFrame) -> str:
        ema20 = self._ema(df["close"], 20)
        ema50 = self._ema(df["close"], 50)
        atr = self._atr(df).iloc[-1]
        last = df["close"].iloc[-1]

        if atr < last * 0.003:
            return "flat"
        if ema20.iloc[-1] > ema50.iloc[-1]:
            return "trend_up"
        if ema20.iloc[-1] < ema50.iloc[-1]:
            return "trend_down"
        return "neutral"

    def build_features(self, df: pd.DataFrame) -> dict:
        ema20 = self._ema(df["close"], 20)
        ema50 = self._ema(df["close"], 50)
        atr = self._atr(df).iloc[-1]
        volume_ma = df["volume"].rolling(20).mean().iloc[-1]

        return {
            "last_close": float(df["close"].iloc[-1]),
            "ema20": float(ema20.iloc[-1]),
            "ema50": float(ema50.iloc[-1]),
            "atr": float(atr),
            "volume": float(df["volume"].iloc[-1]),
            "volume_ma": float(volume_ma),
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

        if regime == "trend_up" and last_close > ema20 > ema50 and volume > volume_ma:
            return {
                "action": "long",
                "symbol": symbol,
                "entry_zone": [round(last_close - atr * 0.2, 4), round(last_close + atr * 0.2, 4)],
                "stop_price": round(last_close - atr * 1.2, 4),
                "tp": {
                    "tp1": round(last_close + atr * 1.2, 4),
                    "tp2": round(last_close + atr * 2.0, 4),
                },
                "reason": "trend_volume_breakout",
            }

        if regime == "trend_down" and last_close < ema20 < ema50 and volume > volume_ma:
            return {
                "action": "short",
                "symbol": symbol,
                "entry_zone": [round(last_close - atr * 0.2, 4), round(last_close + atr * 0.2, 4)],
                "stop_price": round(last_close + atr * 1.2, 4),
                "tp": {
                    "tp1": round(last_close - atr * 1.2, 4),
                    "tp2": round(last_close - atr * 2.0, 4),
                },
                "reason": "trend_volume_breakdown",
            }

        return {"action": "hold", "reason": "no_setup"}