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

    # ── Circuit breaker всей биржи (#htx-outage-2026-07-26) ───────────────────
    # Инцидент 26.07: HTX перестал отвечать (RequestTimeout на ПУБЛИЧНОМ
    # /v1/common/timestamp — ключи ни при чём). Каждый вызов уходил в
    # timeout 45s × 5 попыток + 20s блокирующего sleep = 245s. Сканирование
    # (8 символов × 5 ТФ) морозило event loop на десятки минут → /health
    # переставал отвечать → Render убивал инстанс. В логах видно ровно это:
    # последний health в 17:23:49, рестарт в 17:24:55 — 66с тишины.
    #
    # Размыкатель превращает многоминутную заморозку в мгновенное исключение:
    # после N подряд неудач цепь размыкается на M секунд, вызовы падают сразу.
    # Торговый цикл ловит исключение и идёт спать — сервис жив.
    _cb_consecutive_failures: int = 0
    _cb_open_until: float = 0.0
    _cb_host_index: int = 0

    class ExchangeUnavailable(Exception):
        """Цепь разомкнута: биржа признана недоступной, сеть не трогаем."""

    @classmethod
    def _cb_hosts(cls) -> list[str]:
        """Хосты для ротации. HTX действительно держит несколько эндпоинтов
        (api.huobi.pro / api-aws.huobi.pro) и мигрирует на htx.com — если один
        адрес заблокирован или не маршрутизируется из ДЦ, пробуем следующий."""
        raw = str(getattr(settings, "HTX_API_HOSTNAME_FALLBACKS", "") or "")
        hosts = [h.strip() for h in raw.split(",") if h.strip()]
        primary = str(getattr(settings, "HTX_API_HOSTNAME", "") or "").strip()
        if primary:
            hosts = [primary] + [h for h in hosts if h != primary]
        return hosts

    @classmethod
    def circuit_state(cls) -> dict:
        now = time.time()
        hosts = cls._cb_hosts()
        return {
            "open": now < cls._cb_open_until,
            "opens_in_sec": max(0.0, round(cls._cb_open_until - now, 1)),
            "consecutive_failures": cls._cb_consecutive_failures,
            "active_host": hosts[cls._cb_host_index % len(hosts)] if hosts else None,
            "hosts": hosts,
        }

    @classmethod
    def _cb_note_success(cls) -> None:
        cls._cb_consecutive_failures = 0
        cls._cb_open_until = 0.0

    @classmethod
    def _cb_note_failure(cls) -> None:
        cls._cb_consecutive_failures += 1
        threshold = int(getattr(settings, "HTX_CIRCUIT_FAILURE_THRESHOLD", 3))
        if cls._cb_consecutive_failures < threshold:
            return
        cls._cb_open_until = time.time() + float(
            getattr(settings, "HTX_CIRCUIT_OPEN_SECONDS", 120.0)
        )
        # Ротация хоста: следующая проба пойдёт на другой эндпоинт.
        hosts = cls._cb_hosts()
        if len(hosts) > 1:
            cls._cb_host_index = (cls._cb_host_index + 1) % len(hosts)
        log_event(
            logger,
            logging.ERROR,
            "htx_circuit_open",
            consecutive_failures=cls._cb_consecutive_failures,
            open_seconds=float(getattr(settings, "HTX_CIRCUIT_OPEN_SECONDS", 120.0)),
            next_host=hosts[cls._cb_host_index] if hosts else None,
        )

    def __init__(self):
        proxy_url = str(getattr(settings, "HTX_PROXY_URL", "") or "").strip()

        exchange_config: dict = {
            "apiKey": settings.HTX_API_KEY,
            "secret": settings.HTX_API_SECRET,
            "enableRateLimit": True,
            # (#htx-outage-2026-07-26) 45s → 15s. 45 закладывали под Docker-on-Windows
            # + VPN, но в проде это потолок заморозки на КАЖДУЮ попытку: 45×5+20 = 245с
            # на один вызов. Живой HTX отвечает за десятки миллисекунд; 15с — это уже
            # «биржа не отвечает», ждать дольше смысла нет.
            "timeout": int(getattr(settings, "HTX_HTTP_TIMEOUT_MS", 15000)),
            "options": {
                "defaultType": settings.HTX_MARKET_TYPE,
                "adjustForTimeDifference": True,
            },
        }

        # ccxt supports HTTP/SOCKS5 proxies via 'proxies' dict.
        # Set HTX_PROXY_URL=http://user:pass@host:port  or
        #     HTX_PROXY_URL=socks5://user:pass@host:port
        # Переопределение хоста (например api-aws.huobi.pro для AWS-регионов).
        # (#htx-outage-2026-07-26) Хост берём из ротации размыкателя: если основной
        # эндпоинт перестал маршрутизироваться из ДЦ, следующая проба уйдёт на
        # запасной (api-aws.huobi.pro / api.htx.com) без ручного вмешательства.
        _hosts = HTXClient._cb_hosts()
        hostname = _hosts[HTXClient._cb_host_index % len(_hosts)] if _hosts else ""
        if hostname:
            exchange_config["hostname"] = hostname

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
        # (#htx-outage-2026-07-26) Цепь разомкнута — не тратим 245с на заведомо
        # мёртвую биржу и не морозим event loop. Падаем сразу, вызывающий цикл
        # переживёт исключение и попробует на следующей итерации.
        if time.time() < HTXClient._cb_open_until:
            raise HTXClient.ExchangeUnavailable(
                f"htx circuit open for {HTXClient._cb_open_until - time.time():.0f}s "
                f"after {HTXClient._cb_consecutive_failures} consecutive failures"
            )

        # Адаптивные попытки: первый сбой может быть разовым — отрабатываем полный
        # набор. Но если предыдущий вызов уже упал, повторять по 3–5 раз бессмысленно,
        # это лишь удлиняет заморозку. Сокращаем до одной попытки, чтобы размыкатель
        # сработал за секунды, а не за минуты.
        if HTXClient._cb_consecutive_failures >= 1:
            retries = 1

        last_error = None

        for attempt in range(1, retries + 1):
            try:
                result = fn(*args, **kwargs)
                HTXClient._cb_note_success()
                return result
            except Exception as e:
                last_error = e
                # error_type критичен для диагностики: str(e) у ccxt для
                # RequestTimeout — это просто "htx GET <url>" без причины, и по
                # логам 26.07 нельзя было отличить таймаут от 403 (гео-блок),
                # DNS-сбоя или 5xx. Теперь тип виден сразу.
                log_event(
                    logger,
                    logging.WARNING,
                    "htx_retry",
                    attempt=attempt,
                    retries=retries,
                    error_type=type(e).__name__,
                    error=str(e),
                )

                if attempt < retries:
                    base = delay * attempt
                    jitter = base * 0.2 * (random.random() * 2 - 1)  # ±20%
                    time.sleep(max(0.1, base + jitter))

        HTXClient._cb_note_failure()
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

    def fetch_balance(self, params: dict | None = None):
        # params={'type':'spot'} или {'type':'swap'} — у HTX SPOT и USDT-M фьючерсы
        # это РАЗНЫЕ счета со своими свободными остатками.
        return self._retry(self.exchange.fetch_balance, params or {})

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

    # ── Live-safe примитивы (без слепого ретрая create — он не идемпотентен) ───
    def create_order_once(self, symbol: str, type_: str, side: str, amount: float,
                          price: float | None = None, params: dict | None = None):
        """ОДНА попытка отправки ордера (НЕ _retry). Повтор create при таймауте
        может удвоить позицию — идемпотентность обеспечивает вызывающий слой
        (LiveExecutor) через сверку по clientOrderId."""
        return self.exchange.create_order(symbol, type_, side, amount, price, params or {})

    def set_leverage(self, leverage: float, symbol: str, params: dict | None = None):
        """Выставить плечо для символа (swap). best-effort, идемпотентно."""
        if hasattr(self.exchange, "set_leverage"):
            return self._retry(self.exchange.set_leverage, leverage, symbol, params or {})
        return None

    def set_margin_mode(self, margin_mode: str, symbol: str, params: dict | None = None):
        """cross/isolated для символа (swap). best-effort."""
        if hasattr(self.exchange, "set_margin_mode"):
            try:
                return self._retry(self.exchange.set_margin_mode, margin_mode, symbol, params or {})
            except Exception as exc:  # noqa: BLE001 — некоторые аккаунты не дают менять режим с позицией
                log_event(logger, logging.WARNING, "htx_set_margin_mode_skip", symbol=symbol, error=str(exc))
        return None

    def fetch_closed_orders(self, symbol: str | None = None, limit: int = 20):
        if hasattr(self.exchange, "fetch_closed_orders"):
            return self._retry(self.exchange.fetch_closed_orders, symbol, None, limit)
        return []

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

        Приоритет (spot):
        1. Биржа через fetch_trading_fee(symbol)
        2. market metadata из load_markets()
        3. fallback из settings

        Приоритет (swap/futures/perp):
        1. metadata КОНТРАКТНОГО рынка (BTC/USDT:USDT)
        2. fallback из settings (FUTURES_*)

        КРИТИЧНО (#audit-cost-model): инстанс ccxt работает с defaultType из
        HTX_MARKET_TYPE (обычно spot). fetch_trading_fee/spot-metadata для
        swap-запроса возвращали СПОТ-ставку 0.2%, завышая издержки деривативной
        сделки в ~4 раза (факт аудита: total_cost 0.45% round-trip при swap
        taker 0.05%). Для деривативов спот-источники запрещены.
        """
        market_type_value = market_type or settings.MARKET_TYPE
        is_derivative = market_type_value in ["swap", "futures", "perp"]

        maker = None
        taker = None
        source = "fallback_settings"

        if is_derivative:
            # Только метаданные контрактного рынка; спот-ставки не подходят.
            try:
                self.load_markets()
                markets = self.exchange.markets or {}
                contract = None
                for candidate in (f"{symbol}:USDT", symbol):
                    m = markets.get(candidate)
                    if m and (m.get("swap") or m.get("future") or m.get("contract")):
                        contract = m
                        break
                if contract:
                    maker = contract.get("maker")
                    taker = contract.get("taker")
                    if maker is not None or taker is not None:
                        source = "contract_market_metadata"
            except Exception as e:
                log_event(logger, logging.WARNING, "htx_contract_fee_meta_error", symbol=symbol, error=str(e))
        else:
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
