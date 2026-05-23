import time
import ccxt
from core.config import settings


class HTXClient:
    _markets_loaded = False

    def __init__(self):
        self.exchange = ccxt.htx({
            "apiKey": settings.HTX_API_KEY,
            "secret": settings.HTX_API_SECRET,
            "enableRateLimit": True,
            "timeout": 20000,
            "options": {
                "defaultType": settings.HTX_MARKET_TYPE,
                "adjustForTimeDifference": True,
            }
        })

    def _retry(self, fn, *args, retries: int = 3, delay: float = 1.0, **kwargs):
        last_error = None

        for attempt in range(1, retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_error = e
                print(f"[HTX RETRY] attempt={attempt}/{retries} error={e}")

                if attempt < retries:
                    time.sleep(delay * attempt)

        raise last_error

    def load_markets(self):
        if not HTXClient._markets_loaded:
            result = self._retry(self.exchange.load_markets)
            HTXClient._markets_loaded = True
            return result

        return self.exchange.markets or self.exchange.load_markets()

    def fetch_balance(self):
        return self._retry(self.exchange.fetch_balance)

    def fetch_ticker(self, symbol: str):
        return self._retry(self.exchange.fetch_ticker, symbol)

    def fetch_ohlcv(self, symbol: str, timeframe="5m", limit=200):
        return self._retry(
            self.exchange.fetch_ohlcv,
            symbol,
            timeframe=timeframe,
            limit=limit,
        )

    def fetch_open_orders(self, symbol: str | None = None):
        return self._retry(self.exchange.fetch_open_orders, symbol)

    def fetch_positions(self):
        if hasattr(self.exchange, "fetch_positions"):
            return self._retry(self.exchange.fetch_positions)
        return []

    def create_market_order(self, symbol: str, side: str, amount: float, params: dict | None = None):
        return self._retry(
            self.exchange.create_order,
            symbol,
            "market",
            side,
            amount,
            None,
            params or {},
        )

    def cancel_order(self, order_id: str, symbol: str):
        return self._retry(self.exchange.cancel_order, order_id, symbol)

    def fetch_order(self, order_id: str, symbol: str):
        return self._retry(self.exchange.fetch_order, order_id, symbol)

    def price_to_precision(self, symbol: str, price: float) -> float:
        try:
            self.load_markets()
            return float(self.exchange.price_to_precision(symbol, price))
        except Exception as e:
            print(f"[HTX PRICE PRECISION FALLBACK] {symbol}: {e}")
            return float(price)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        try:
            self.load_markets()
            return float(self.exchange.amount_to_precision(symbol, amount))
        except Exception as e:
            print(f"[HTX AMOUNT PRECISION FALLBACK] {symbol}: {e}")
            return float(amount)

    def market_limits(self, symbol: str) -> dict:
        try:
            markets = self.load_markets()
            market = markets.get(symbol) or self.exchange.market(symbol)
            limits = market.get("limits") or {}

            amount_limits = limits.get("amount") or {}
            cost_limits = limits.get("cost") or {}
            price_limits = limits.get("price") or {}

            return {
                "min_amount": amount_limits.get("min"),
                "max_amount": amount_limits.get("max"),
                "min_cost": cost_limits.get("min"),
                "max_cost": cost_limits.get("max"),
                "min_price": price_limits.get("min"),
                "max_price": price_limits.get("max"),
            }

        except Exception as e:
            print(f"[HTX MARKET META ERROR] {symbol}: {e}")
            return {
                "min_amount": None,
                "max_amount": None,
                "min_cost": None,
                "max_cost": None,
                "min_price": None,
                "max_price": None,
            }

    def fetch_trading_fee(self, symbol: str) -> dict:
        """
        Пытаемся получить актуальные комиссии по конкретному символу через HTX/CCXT.
        Если биржа/API временно не отдали комиссии — возвращаем пустой dict.
        """
        try:
            self.exchange.load_markets()

            if hasattr(self.exchange, "fetch_trading_fee"):
                fee = self.exchange.fetch_trading_fee(symbol)
                return fee or {}

        except Exception as e:
            print(f"[HTX FEE ERROR] {symbol}: {e}")

        return {}

    def trading_fee_rates(self, symbol: str, market_type: str | None = None) -> dict:
        """
        Нормализованные maker/taker комиссии.

        Приоритет:
        1. Биржа через fetch_trading_fee(symbol)
        2. market metadata из load_markets()
        3. fallback из settings
        """
        market_type_value = market_type or settings.MARKET_TYPE

        maker = None
        taker = None
        source = "fallback_settings"

        fee = self.fetch_trading_fee(symbol)

        if fee:
            maker = fee.get("maker")
            taker = fee.get("taker")

            if maker is not None or taker is not None:
                source = "exchange_api"

        try:
            self.exchange.load_markets()
            market = self.exchange.market(symbol)

            if maker is None:
                maker = market.get("maker")

            if taker is None:
                taker = market.get("taker")

            if source == "fallback_settings" and (maker is not None or taker is not None):
                source = "market_metadata"

        except Exception as e:
            print(f"[HTX MARKET FEE META ERROR] {symbol}: {e}")

        if maker is None:
            maker = (
                settings.FUTURES_MAKER_FEE
                if market_type_value in ["swap", "futures", "perp"]
                else settings.SPOT_MAKER_FEE
            )

        if taker is None:
            taker = (
                settings.FUTURES_TAKER_FEE
                if market_type_value in ["swap", "futures", "perp"]
                else settings.SPOT_TAKER_FEE
            )

        return {
            "symbol": symbol,
            "market_type": market_type_value,
            "maker": float(maker),
            "taker": float(taker),
            "source": source,
        }