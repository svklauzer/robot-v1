import random
import pandas as pd
from services.exchange_factory import get_exchange_client, resolve_exchange_name

from core.config import settings
from core.logging import get_logger, log_event

logger = get_logger(__name__)
_SWAP_MISSING: set[str] = set()   # символы без свопа: предупреждаем один раз
_SOURCE_LOGGED = False


class MarketDataService:
    def __init__(self, exchange: str | None = None):
        # (#okx-satellite-exchange-routing-2026-09-02) см. CostEngine.__init__.
        self.client = get_exchange_client(exchange)
        # (#market-source-label-2026-09-02) Раньше "source" в snapshot/
        # ticker_snapshot был захардкожен строкой "htx" — Health-страница
        # показывала неверный источник данных при активной OKX.
        self.exchange_name = resolve_exchange_name(exchange)

    # (#market-data-swap-2026-10-25) Рынок данных. ccxt: BTC/USDT — спот,
    # BTC/USDT:USDT — своп. Торгуем и стоп-контролируем на свопе, значит и
    # свечи/цена должны быть оттуда. Только OKX; прочее — спот как раньше.
    def _data_market(self) -> str:
        if self.exchange_name != "okx":
            return "spot"
        value = str(getattr(settings, "MARKET_DATA_TYPE", "spot") or "spot").lower().strip()
        return "swap" if value == "swap" else "spot"

    def _data_symbol(self, symbol: str) -> str:
        if self._data_market() != "swap" or ":" in symbol:
            return symbol
        from services.market_routing import _swap_symbol

        return _swap_symbol(symbol)

    def _call(self, method: str, symbol: str, *args, **kwargs):
        global _SOURCE_LOGGED
        if not _SOURCE_LOGGED:
            _SOURCE_LOGGED = True
            log_event(logger, 20, "market_data_source", exchange=self.exchange_name,
                      data_market=self._data_market())
        fn = getattr(self.client, method)
        data_symbol = self._data_symbol(symbol)
        if data_symbol == symbol:
            return fn(symbol, *args, **kwargs)
        try:
            return fn(data_symbol, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            # Откат на спот ТОЛЬКО если у символа нет свопа. Сетевые и прочие
            # ошибки не маскируем спотом: это снова смешало бы два инструмента.
            if type(exc).__name__ != "BadSymbol" and "does not have market symbol" not in str(exc):
                raise
            if symbol not in _SWAP_MISSING:
                _SWAP_MISSING.add(symbol)
                log_event(logger, 30, "market_data_swap_missing", symbol=symbol,
                          data_symbol=data_symbol, fallback="spot")
            return fn(symbol, *args, **kwargs)

    def snapshot(self, symbol: str) -> dict:
        try:
            ticker = self._call("fetch_ticker", symbol)
            ohlcv = self._call("fetch_ohlcv", symbol, "5m", 200)

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
                "source": self.exchange_name,
                "data_market": self._data_market(),
            }

        except Exception as e:
            print(f"[MARKET DATA ERROR] {symbol}: {e}")
            raise

    def ticker_snapshot(self, symbol: str) -> dict:
        """Лёгкая проба коннекта: только тикер (без 200 свечей OHLCV).

        Используется health/connectivity-проверками, где нужны лишь bid/ask/last.
        Один HTTP-вызов вместо двух → корректный замер реальной задержки.
        """
        ticker = self._call("fetch_ticker", symbol)
        return {
            "symbol": symbol,
            "last": ticker.get("last"),
            "bid": ticker.get("bid"),
            "ask": ticker.get("ask"),
            "source": self.exchange_name,
            "data_market": self._data_market(),
        }

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
            data = self._call("fetch_ohlcv", symbol, timeframe=timeframe, limit=limit)

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

