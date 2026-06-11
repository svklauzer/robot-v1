import logging
import random
import socket
import time

import ccxt
import urllib3.util.connection as _urllib3_connection
from core.config import settings
from core.logging import get_logger, log_event

logger = get_logger(__name__)


# ── Force IPv4 for ccxt/requests traffic ──────────────────────────────────────
# ccxt uses requests/urllib3 synchronously. On dual-stack hosts an IPv6 attempt
# can stall on connect (Happy Eyeballs) before falling back to IPv4, adding
# seconds of latency (and triggering our retry/backoff). The Telegram httpx
# client already forces IPv4 for the same reason; this does it for ccxt by
# making urllib3 resolve only A (IPv4) records.
def _allowed_gai_family_ipv4_only():
    return socket.AF_INET


_urllib3_connection.allowed_gai_family = _allowed_gai_family_ipv4_only


class HTXClient:
    # ── Class-level market cache ──────────────────────────────────────────────
    # Shared across ALL instances so that a new HTXClient() reuses the markets
    # loaded by a previous one instead of hitting load_markets() again.
    _markets_loaded: bool = False
    _cached_markets: dict = {}

    # Fee API circuit-breaker: if the endpoint fails (e.g. insufficient API
    # permissions), stop retrying for _FEE_BACKOFF_SECONDS to avoid log spam.
    _fee_api_backoff_until: float = 0.0
    _FEE_BACKOFF_SECONDS: float = 4 * 3600  # 4 hours

    def __init__(self):
        proxy_url = str(getattr(settings, "HTX_PROXY_URL", "") or "").strip()

        exchange_config: dict = {
            "apiKey": settings.HTX_API_KEY,
            "secret": settings.HTX_API_SECRET,
            "enableRateLimit": True,
            "timeout": 45000,   # raised 20s→30s→45s; Docker-on-Windows + VPN adds RTT
            "options": {
                "defaultType": settings.HTX_MARKET_TYPE,
                "adjustForTimeDifference": True,
            },
        }

        # ccxt supports HTTP/SOCKS5 proxies via 'proxies' dict.
        # Set HTX_PROXY_URL=http://user:pass@host:port  or
        #     HTX_PROXY_URL=socks5://user:pass@host:port
        if proxy_url:
            exchange_config["proxies"] = {
                "http": proxy_url,
                "https": proxy_url,
            }
            log_event(
                logger,
                logging.INFO,
                "htx_using_proxy",
                proxy=proxy_url[:30] + "..." if len(proxy_url) > 30 else proxy_url,
            )

        self.exchange = ccxt.htx(exchange_config)

        # Disable fetchCurrencies — ccxt/HTX calls v2/reference/currencies during
        # load_markets(). That endpoint times out under poor network conditions
        # (Docker on Windows, VPN) and the data isn't needed for our use case.
        self.exchange.has["fetchCurrencies"] = False

        # Inject class-level market cache into the fresh exchange object so this
        # instance can use already-loaded precision/limit data without a round-trip.
        if HTXClient._cached_markets:
            self.exchange.markets = HTXClient._cached_markets

    def _retry(self, fn, *args, retries: int = 3, delay: float = 2.0, **kwargs):
        """Retry wrapper with exponential backoff + jitter.

        Jitter (±20%) prevents thundering-herd when multiple symbols retry in sync.
        Delays: ~2s, ~4s before attempts 2 and 3.
        """
        last_error = None

        for attempt in range(1, retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_error = e
                log_event(logger, logging.WARNING, "htx_retry", attempt=attempt, retries=retries, error=str(e))

                if attempt < retries:
                    base = delay * attempt
                    jitter = base * 0.2 * (random.random() * 2 - 1)  # ±20%
                    time.sleep(max(0.1, base + jitter))

        raise last_error

    def load_markets(self):
        """
        Load and cache exchange markets — with cross-instance sharing.

        Priority:
        1. Class-level cache populated by a previous instance  → return immediately
        2. Exchange object already has markets (rare) → promote to class cache
        3. Fetch from API → write to class cache so future instances don't repeat

        fetchCurrencies is disabled at __init__ so v2/reference/currencies is
        never called; only the markets endpoint is used.
        """
        # Fast path: class-level cache populated
        if HTXClient._cached_markets:
            # Ensure this instance's exchange object also has the markets
            if not self.exchange.markets:
                self.exchange.markets = HTXClient._cached_markets
            return HTXClient._cached_markets

        # Exchange object already populated (edge case: markets set externally)
        if self.exchange.markets:
            HTXClient._markets_loaded = True
            HTXClient._cached_markets = self.exchange.markets
            return self.exchange.markets

        try:
            result = self._retry(self.exchange.load_markets, retries=5, delay=3.0)
            HTXClient._markets_loaded = True
            HTXClient._cached_markets = result
            log_event(logger, logging.INFO, "htx_markets_loaded", count=len(result))
            return result
        except Exception as e:
            # If we can't load markets, log and return empty dict.
            # Callers handle missing market data gracefully (fallback to defaults).
            log_event(
                logger, logging.ERROR, "htx_load_markets_failed",
                error=str(e),
                note="precision and limits will use fallback values",
            )
            return {}

    def fetch_balance(self):
        return self._retry(self.exchange.fetch_balance)

    def fetch_ticker(self, symbol: str):
        # Прогреваем кросс-инстансный кэш рынков, иначе ccxt дергает полный
        # load_markets() внутри каждого fetch_ticker (сотни символов = ~3-4с).
        # После первого раза это no-op (фаст-путь по _cached_markets).
        self.load_markets()
        return self._retry(self.exchange.fetch_ticker, symbol)

    def fetch_ohlcv(self, symbol: str, timeframe="5m", limit=200):
        self.load_markets()
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

    def fetch_funding_rate(self, symbol: str):
        if hasattr(self.exchange, "fetch_funding_rate"):
            return self._retry(self.exchange.fetch_funding_rate, symbol)
        raise RuntimeError("HTX/CCXT fetch_funding_rate is not available")

    def fetch_mark_price(self, symbol: str) -> float:
        ticker = self.fetch_ticker(symbol)
        return float(ticker.get("mark") or ticker.get("last") or ticker.get("close") or ticker.get("bid") or ticker.get("ask"))

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
            price = float(price)
        except Exception:
            price = 0.0

        # Avoid noisy exchange precision errors on zero/negative/invalid prices.
        # Callers may temporarily pass placeholder values during intermediate calculations.
        if price <= 0:
            return price

        try:
            self.load_markets()
            return float(self.exchange.price_to_precision(symbol, price))
        except Exception as e:
            log_event(logger, logging.WARNING, "htx_price_precision_fallback", symbol=symbol, error=str(e))
            return float(price)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        try:
            amount = float(amount)
        except Exception:
            amount = 0.0

        if amount <= 0:
            return amount

        try:
            self.load_markets()
            return float(self.exchange.amount_to_precision(symbol, amount))
        except Exception as e:
            log_event(logger, logging.WARNING, "htx_amount_precision_fallback", symbol=symbol, error=str(e))
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
            log_event(logger, logging.WARNING, "htx_market_meta_error", symbol=symbol, error=str(e))
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
        Fetch live trading fees for a symbol.

        Circuit-breaker: if the fee endpoint fails (e.g. API key lacks fee-query
        permissions), we back off for _FEE_BACKOFF_SECONDS before retrying.
        This prevents a WARNING flood every 30s in the logs.
        """
        now = time.time()
        if now < HTXClient._fee_api_backoff_until:
            return {}  # still in back-off window — skip silently

        try:
            self.exchange.load_markets()

            if hasattr(self.exchange, "fetch_trading_fee"):
                fee = self.exchange.fetch_trading_fee(symbol)
                return fee or {}

        except Exception as e:
            # Trip the circuit-breaker so we stop retrying for a long while.
            HTXClient._fee_api_backoff_until = now + HTXClient._FEE_BACKOFF_SECONDS
            log_event(
                logger, logging.WARNING, "htx_fee_error",
                symbol=symbol, error=str(e),
                note=f"fee API disabled for {HTXClient._FEE_BACKOFF_SECONDS/3600:.0f}h, using settings fallback",
            )

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
            log_event(logger, logging.WARNING, "htx_market_fee_meta_error", symbol=symbol, error=str(e))

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
