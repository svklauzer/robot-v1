import ccxt
import random
import pandas as pd
from services.htx_client import HTXClient

from core.config import settings


class MarketDataService:
    def __init__(self):
        self.client = HTXClient()
        self.exchange = None

    def snapshot(self, symbol: str) -> dict:
        try:
            ticker = self.client.fetch_ticker(symbol)
            ohlcv = self.client.fetch_ohlcv(symbol, "5m", 200)

            df = pd.DataFrame(
                ohlcv,
                columns=["ts", "open", "high", "low", "close", "volume"]
            )

            return {
                "symbol": symbol,
                "last": ticker["last"],
                "bid": ticker.get("bid"),
                "ask": ticker.get("ask"),
                "ohlcv": df,
                "source": "htx",
            }

        except Exception as e:
            print(f"[MARKET DATA ERROR] {symbol}: {e}")
            raise

    def mock_snapshot(self, symbol: str) -> dict:
        base_price = {
            "BTC/USDT": 64000,
            "ETH/USDT": 3200,
            "SOL/USDT": 140,
        }.get(symbol, 100)

        rows = []
        price = base_price

        for i in range(200):
            drift = random.uniform(-0.002, 0.003)
            price = price * (1 + drift)

            high = price * (1 + random.uniform(0.0005, 0.003))
            low = price * (1 - random.uniform(0.0005, 0.003))
            open_price = price * (1 + random.uniform(-0.001, 0.001))
            close = price
            volume = random.uniform(100, 1000)

            rows.append([
                i,
                open_price,
                high,
                low,
                close,
                volume,
            ])

        df = pd.DataFrame(
            rows,
            columns=["ts", "open", "high", "low", "close", "volume"]
        )

        last = float(df["close"].iloc[-1])

        return {
            "symbol": symbol,
            "last": last,
            "bid": last * 0.999,
            "ask": last * 1.001,
            "ohlcv": df,
            "source": "mock",
        }

    def _mock_allowed(self) -> bool:
        return bool(getattr(settings, "ALLOW_MARKET_MOCK", False))

    def ohlcv(self, symbol: str, timeframe: str = "5m", limit: int = 200):
        try:
            exchange = self._get_exchange()

            data = exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                limit=limit,
            )

            df = pd.DataFrame(
                data,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )

            if df.empty or len(df) < 60:
                raise ValueError(f"not_enough_ohlcv: {symbol} {timeframe} len={len(df)}")

            return df

        except Exception as e:
            print(f"[MARKET OHLCV ERROR] {symbol} {timeframe}: {e}")
            raise

    def multi_timeframe_snapshot(self, symbol: str):
        """
        Возвращает OHLCV по нескольким таймфреймам.
        """

        timeframes = ["1m", "5m", "15m", "1h", "4h"]

        result = {}

        for tf in timeframes:
            result[tf] = self.ohlcv(symbol, timeframe=tf, limit=250)

        last_snap = self.snapshot(symbol)

        return {
            "symbol": symbol,
            "source": last_snap.get("source", "unknown"),
            "last": last_snap.get("last"),
            "timeframes": result,
        }   


    def _get_exchange(self):
        if self.exchange is not None:
            return self.exchange

        self.exchange = ccxt.htx({
            "apiKey": settings.HTX_API_KEY,
            "secret": settings.HTX_API_SECRET,
            "enableRateLimit": True,
            "options": {
                "defaultType": settings.HTX_MARKET_TYPE,
            },
        })

        return self.exchange     