import logging
import math
import random
import time
import uuid

import ccxt
from core.config import settings
from core.logging import get_logger, log_event

logger = get_logger(__name__)

# IPv4-форс urllib3 уже применён глобально при импорте htx_client (см. его
# докстринг/комментарий там) — тот же паттерн, что уже использует kraken_client.
# Повторно патчить здесь не нужно: это process-global monkeypatch.


class OKXClient:
    """(#okx-satellite-2026-09-02) Второй биржевой клиент — OKX как ручной
    сателлит HTX. Переключение биржи — ТОЛЬКО через ACTIVE_EXCHANGE (Render env
    + редеплой), автоматического failover нет. Обе биржи никогда не торгуют
    одновременно: RobotLoop/LiveExecutor/TradePlanBuilder остаются однобиржевыми,
    просто конструируют либо HTXClient, либо OKXClient (см. exchange_factory.py).

    Структура — ручная параллельная реализация HTXClient (тот же паттерн, что
    уже использует KrakenClient относительно HTXClient), а не общий базовый
    класс: контракт формализован отдельно в exchange_client.py (typing.Protocol,
    без наследования и рантайм-эффекта).

    (#okx-precision-verified-2026-09-02) amount_to_precision/contract_size ниже
    повторяют HTX-логику "конвертировать в контракты, округлить вниз, вернуть
    обратно" — сверено с официальной документацией OKX (docs-v5, /public/
    instruments): у деривативов (SWAP/FUTURES) `sz` ордера ДЕЙСТВИТЕЛЬНО в
    контрактах, не в монетах ("lotSz/minSz: If it is a derivatives contract,
    the value is the number of contracts"), а `ctVal` (contract value) —
    ровно то поле, из которого ccxt строит унифицированный `contractSize`.
    Модель конвертации не HTX-специфика, а общий контракт деривативов OKX —
    порт логики корректен.
    (#okx-lot-step-2026-09-16) Прежняя версия округляла до ЦЕЛОГО контракта и
    при объёме меньше одного контракта поднимала его до одного. Утверждение
    «никогда не завышает объём» было неверным: у OKX шаг лота 0.01 контракта
    (BTC/ETH/SOL/XRP/DOGE), а 1 контракт BTC = 0.01 BTC ≈ 760 USDT. План с кэпом
    250 USDT превращался в позицию 760: в бумаге BTC стоил втрое больше других
    сделок, в live каждый такой ордер отклонял бы кэп. Теперь шаг и минимум
    берутся из рынка (lotSz/minSz), округление только вниз, меньше минимума — 0.
    """

    _markets_loaded: bool = False
    _cached_markets: dict = {}

    _fee_api_backoff_until: float = 0.0
    _FEE_BACKOFF_SECONDS: float = 4 * 3600  # 4 hours

    _cb_consecutive_failures: int = 0
    _cb_open_until: float = 0.0
    _cb_host_index: int = 0

    class ExchangeUnavailable(Exception):
        """Цепь разомкнута: биржа признана недоступной, сеть не трогаем."""

    @classmethod
    def _cb_hosts(cls) -> list[str]:
        raw = str(getattr(settings, "OKX_API_HOSTNAME_FALLBACKS", "") or "")
        hosts = [h.strip() for h in raw.split(",") if h.strip()]
        primary = str(getattr(settings, "OKX_API_HOSTNAME", "") or "").strip()
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
        threshold = int(getattr(settings, "OKX_CIRCUIT_FAILURE_THRESHOLD", 2))
        if cls._cb_consecutive_failures < threshold:
            return
        cls._cb_open_until = time.time() + float(
            getattr(settings, "OKX_CIRCUIT_OPEN_SECONDS", 120.0)
        )
        hosts = cls._cb_hosts()
        if len(hosts) > 1:
            cls._cb_host_index = (cls._cb_host_index + 1) % len(hosts)
        log_event(
            logger,
            logging.ERROR,
            "okx_circuit_open",
            consecutive_failures=cls._cb_consecutive_failures,
            open_seconds=float(getattr(settings, "OKX_CIRCUIT_OPEN_SECONDS", 120.0)),
            next_host=hosts[cls._cb_host_index] if hosts else None,
        )

    def __init__(self):
        proxy_url = str(getattr(settings, "OKX_PROXY_URL", "") or "").strip()

        exchange_config: dict = {
            "apiKey": settings.OKX_API_KEY,
            "secret": settings.OKX_API_SECRET,
            # OKX требует третий креденшл — passphrase, заданный при создании
            # API-ключа на бирже. У HTX/Kraken этого поля нет вовсе.
            "password": settings.OKX_API_PASSPHRASE,
            "enableRateLimit": True,
            "timeout": int(getattr(settings, "OKX_HTTP_TIMEOUT_MS", 15000)),
            "options": {
                "defaultType": settings.OKX_MARKET_TYPE,
                "adjustForTimeDifference": True,
                # (#memory-probe-2026-09-16) Только спот и свопы. По умолчанию
                # ccxt грузит ещё срочные фьючерсы и опционы: 4443 рынка и
                # +55 МБ против 1905 и +28 МБ (замер 16.09) — при лимите
                # контейнера 512 МБ. Торговля идёт спотом (OKX_MARKET_TYPE),
                # комиссии и стакан — по свопам; срочные и опционы не нужны нигде.
                "fetchMarkets": {"types": ["spot", "swap"]},
            },
        }

        _hosts = OKXClient._cb_hosts()
        hostname = _hosts[OKXClient._cb_host_index % len(_hosts)] if _hosts else ""
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
                "okx_using_proxy",
                proxy=proxy_url[:30] + "..." if len(proxy_url) > 30 else proxy_url,
            )

        self.exchange = ccxt.okx(exchange_config)

        # Отключаем fetchCurrencies по той же причине, что и в HTXClient: этот
        # эндпоинт не нужен для нашего сценария, а его отказ/таймаут не должен
        # мешать load_markets().
        self.exchange.has["fetchCurrencies"] = False

        if OKXClient._cached_markets:
            self.exchange.markets = OKXClient._cached_markets

    def _retry(self, fn, *args, retries: int = 3, delay: float = 2.0, **kwargs):
        """Тот же контракт, что и HTXClient._retry: размыкатель → DNS-preflight
        → экспоненциальный backoff+jitter → fast-trip к 1 попытке после
        предыдущего сбоя. См. HTXClient._retry для полного обоснования."""
        if time.time() < OKXClient._cb_open_until:
            raise OKXClient.ExchangeUnavailable(
                f"okx circuit open for {OKXClient._cb_open_until - time.time():.0f}s "
                f"after {OKXClient._cb_consecutive_failures} consecutive failures"
            )

        from services.net_guard import resolve_ok

        _hosts = OKXClient._cb_hosts()
        _host = _hosts[OKXClient._cb_host_index % len(_hosts)] if _hosts else "www.okx.com"
        if not resolve_ok(_host):
            OKXClient._cb_note_failure()
            raise OKXClient.ExchangeUnavailable(
                f"okx host {_host} does not resolve within DNS guard timeout — "
                f"outbound network/DNS problem, not the exchange"
            )

        if OKXClient._cb_consecutive_failures >= 1:
            retries = 1

        last_error = None

        for attempt in range(1, retries + 1):
            try:
                result = fn(*args, **kwargs)
                OKXClient._cb_note_success()
                return result
            except Exception as e:
                last_error = e
                log_event(
                    logger,
                    logging.WARNING,
                    "okx_retry",
                    attempt=attempt,
                    retries=retries,
                    error_type=type(e).__name__,
                    error=str(e),
                )

                if attempt < retries:
                    base = delay * attempt
                    jitter = base * 0.2 * (random.random() * 2 - 1)
                    time.sleep(max(0.1, base + jitter))

        OKXClient._cb_note_failure()
        raise last_error

    def load_markets(self):
        if OKXClient._cached_markets:
            if not self.exchange.markets:
                self.exchange.markets = OKXClient._cached_markets
            return OKXClient._cached_markets

        if self.exchange.markets:
            OKXClient._markets_loaded = True
            OKXClient._cached_markets = self.exchange.markets
            return self.exchange.markets

        try:
            result = self._retry(self.exchange.load_markets, retries=5, delay=3.0)
            OKXClient._markets_loaded = True
            OKXClient._cached_markets = result
            log_event(logger, logging.INFO, "okx_markets_loaded", count=len(result))
            return result
        except Exception as e:
            log_event(
                logger, logging.ERROR, "okx_load_markets_failed",
                error=str(e),
                note="precision and limits will use fallback values",
            )
            return {}

    def fetch_balance(self, params: dict | None = None):
        return self._retry(self.exchange.fetch_balance, params or {})

    def fetch_ticker(self, symbol: str):
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
        raise RuntimeError("OKX/CCXT fetch_funding_rate is not available")

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

    def create_order_once(self, symbol: str, type_: str, side: str, amount: float,
                          price: float | None = None, params: dict | None = None):
        """ОДНА попытка отправки ордера (НЕ _retry) — тот же контракт, что и
        HTXClient.create_order_once: идемпотентность обеспечивает вызывающий
        слой (LiveExecutor) через сверку по clientOrderId."""
        return self.exchange.create_order(symbol, type_, side, amount, price, params or {})

    def set_leverage(self, leverage: float, symbol: str, params: dict | None = None):
        if hasattr(self.exchange, "set_leverage"):
            return self._retry(self.exchange.set_leverage, leverage, symbol, params or {})
        return None

    def set_swap_leverage(self, symbol: str, leverage: float, margin_mode: str,
                          position_side: str | None = None):
        """Плечо свопа для режима маржи сделки. (#live-margin-posmode-2026-09-16)

        У OKX режим маржи задаётся не на инструменте, а в каждом ордере (tdMode);
        плечо хранится отдельно на паре инструмент + режим маржи (+ сторона в
        Long/Short mode). Поэтому достаточно одного вызова set-leverage с
        mgnMode — ccxt `set_margin_mode` бьёт в тот же эндпоинт и без `lever`
        падает. Без marginMode ccxt ставит плечо для cross, а ордер isolated
        открылся бы при том плече, что стоит на бирже.
        Ошибку не глушим: без подтверждённого плеча позицию не открываем.
        """
        params: dict = {"marginMode": margin_mode}
        if position_side:
            params["posSide"] = position_side
        return self._retry(self.exchange.set_leverage, leverage, symbol, params)

    def fetch_derivatives_account(self) -> dict:
        """Режим счёта для свопов: {'hedged', 'blocker', 'info'}.

        Один запрос account/config: posMode — режим позиций (long_short_mode →
        нужна сторона позиции в ордере), acctLv — режим счёта. В режиме
        «Spot mode» (acctLv=1) свопы не торгуются вовсе.
        """
        res = self._retry(self.exchange.fetch_position_mode) or {}
        info = res.get("info") or {}
        acct_lv = str(info.get("acctLv") or "")
        blocker = None
        if acct_lv == "1":
            blocker = ("okx_account_mode_spot_only: счёт OKX в режиме Spot mode, свопы не "
                       "торгуются — переключить Trading mode на Spot and futures")
        return {"hedged": bool(res.get("hedged")), "blocker": blocker,
                "info": {"posMode": info.get("posMode"), "acctLv": acct_lv or None}}

    # ccxt okx разбирает сторону позиции из этого ключа (posSide в запросе).
    POSITION_SIDE_PARAM = "positionSide"

    # ── стоп на бирже (#exchange-stop-2026-09-16) ─────────────────────────────
    def create_stop_loss_order(self, symbol: str, side: str, amount: float,
                               trigger_price: float, params: dict | None = None):
        """Условный стоп-лосс по рынку: ordType=conditional, slOrdPx=-1.

        ОДНА попытка, как create_order_once: повтор при таймауте поставил бы
        второй стоп; расхождение ловит сверка открытых стопов. Алгоритмические
        ордера OKX не резервируют ни маржу, ни позицию — рыночному закрытию
        стоп не мешает.
        """
        query = dict(params or {})
        query["stopLossPrice"] = trigger_price
        return self.exchange.create_order(symbol, "market", side, amount, None, query)

    def fetch_open_stop_orders(self, symbol: str):
        return self._retry(self.exchange.fetch_open_orders, symbol, None, None,
                           {"trigger": True, "ordType": "conditional"})

    def cancel_stop_order(self, order_id: str, symbol: str):
        return self._retry(self.exchange.cancel_order, order_id, symbol, {"trigger": True})

    @staticmethod
    def make_client_order_id(purpose: str) -> str:
        """clOrdId OKX: буквы и цифры, до 32 символов, с префиксом робота —
        по нему ордера робота отличаются от ручных (services/robot_orders.py)."""
        from services.robot_orders import alnum_client_id

        return alnum_client_id(purpose)

    def set_margin_mode(self, margin_mode: str, symbol: str, params: dict | None = None):
        if hasattr(self.exchange, "set_margin_mode"):
            try:
                return self._retry(self.exchange.set_margin_mode, margin_mode, symbol, params or {})
            except Exception as exc:  # noqa: BLE001 — некоторые аккаунты не дают менять режим с позицией
                log_event(logger, logging.WARNING, "okx_set_margin_mode_skip", symbol=symbol, error=str(exc))
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

        if price <= 0:
            return price

        try:
            self.load_markets()
            return float(self.exchange.price_to_precision(symbol, price))
        except Exception as e:
            log_event(logger, logging.WARNING, "okx_price_precision_fallback", symbol=symbol, error=str(e))
            return float(price)

    @staticmethod
    def _contract_lot(market: dict) -> tuple[float, float]:
        """(шаг, минимум) объёма ордера в КОНТРАКТАХ. (#okx-lot-step-2026-09-16)

        Источник — сырые `lotSz`/`minSz` OKX из `market["info"]`: однозначны при
        любом режиме точности ccxt. Запасной — унифицированные
        `precision.amount` (у OKX режим TICK_SIZE, это и есть шаг) и
        `limits.amount.min`. Нет ни того ни другого — шаг в 1 контракт, как было.
        """
        def positive(value):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None

        info = market.get("info") or {}
        step = (positive(info.get("lotSz"))
                or positive((market.get("precision") or {}).get("amount"))
                or 1.0)
        minimum = (positive(info.get("minSz"))
                   or positive(((market.get("limits") or {}).get("amount") or {}).get("min"))
                   or step)
        return step, minimum

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Объём В МОНЕТАХ, который биржа примет, — округлённый вниз.

        Для контрактного рынка: монеты → контракты → вниз до шага лота → монеты.
        Меньше минимального лота — 0: сделку открывать нечем, а поднимать объём
        до лота значит выйти за риск и кэп нотионала (#okx-lot-step-2026-09-16).
        Результат никогда не больше `amount`.
        """
        try:
            amount = float(amount)
        except Exception:
            amount = 0.0

        if amount <= 0:
            return amount

        try:
            self.load_markets()
            market = self.exchange.markets.get(symbol)

            if market and market.get('contract') and market.get('contractSize'):
                contract_size = float(market['contractSize'])
                step, minimum = self._contract_lot(market)
                contracts = amount / contract_size
                # Относительный допуск: 0.3/0.1 в float — 2.9999999, а не 3.
                lots = math.floor(contracts / step * (1 + 1e-9))
                contracts_q = lots * step
                if contracts_q < minimum * (1 - 1e-9):
                    return 0.0
                base = round(contracts_q * contract_size, 12)
                return float(min(base, amount))

            return float(self.exchange.amount_to_precision(symbol, amount))
        except Exception as e:
            log_event(logger, logging.WARNING, "okx_amount_precision_fallback",
                      symbol=symbol, amount=float(amount),
                      markets_loaded=bool(OKXClient._cached_markets),
                      error_type=type(e).__name__, error=str(e))
            return float(math.floor(amount))

    def contract_size(self, symbol: str) -> float | None:
        try:
            markets = self.load_markets()
            market = markets.get(symbol) or self.exchange.market(symbol)
            if not market or not market.get("contract"):
                return None
            size = market.get("contractSize")
            return float(size) if size else None
        except Exception as e:  # noqa: BLE001
            log_event(logger, logging.WARNING, "okx_contract_size_unavailable",
                      symbol=symbol, error=str(e))
            return None

    def market_limits(self, symbol: str) -> dict:
        try:
            markets = self.load_markets()
            market = markets.get(symbol) or self.exchange.market(symbol)
            limits = market.get("limits") or {}

            amount_limits = limits.get("amount") or {}
            cost_limits = limits.get("cost") or {}
            price_limits = limits.get("price") or {}

            # (#okx-lot-step-2026-09-16) У деривативов OKX лимиты объёма — в
            # КОНТРАКТАХ (minSz), а план сравнивает их с объёмом в МОНЕТАХ. Пока
            # объём поднимался до целого контракта, BTC (минимум 0.01 контракта)
            # проходил случайно; с честным округлением 0.0032 BTC < «0.01» —
            # и каждая BTC-сделка отклонялась бы. Отдаём в монетах.
            min_amount = amount_limits.get("min")
            max_amount = amount_limits.get("max")
            if market.get("contract") and market.get("contractSize"):
                size = float(market["contractSize"])
                min_amount = float(min_amount) * size if min_amount is not None else None
                max_amount = float(max_amount) * size if max_amount is not None else None

            return {
                "amount_unit": "base",
                "min_amount": min_amount,
                "max_amount": max_amount,
                "min_cost": cost_limits.get("min"),
                "max_cost": cost_limits.get("max"),
                "min_price": price_limits.get("min"),
                "max_price": price_limits.get("max"),
            }

        except Exception as e:
            log_event(logger, logging.WARNING, "okx_market_meta_error", symbol=symbol,
                      error_type=type(e).__name__, error=str(e),
                      note="лимиты биржи недоступны — проверка min_amount/min_cost "
                           "для этого символа НЕ выполняется")
            return {
                "limits_available": False,
                "min_amount": None,
                "max_amount": None,
                "min_cost": None,
                "max_cost": None,
                "min_price": None,
                "max_price": None,
            }

    def fetch_trading_fee(self, symbol: str) -> dict:
        now = time.time()
        if now < OKXClient._fee_api_backoff_until:
            return {}

        try:
            self.exchange.load_markets()

            if hasattr(self.exchange, "fetch_trading_fee"):
                fee = self.exchange.fetch_trading_fee(symbol)
                return fee or {}

        except Exception as e:
            OKXClient._fee_api_backoff_until = now + OKXClient._FEE_BACKOFF_SECONDS
            log_event(
                logger, logging.WARNING, "okx_fee_error",
                symbol=symbol, error=str(e),
                note=f"fee API disabled for {OKXClient._FEE_BACKOFF_SECONDS/3600:.0f}h, using settings fallback",
            )

        return {}

    def trading_fee_rates(self, symbol: str, market_type: str | None = None) -> dict:
        """Тот же приоритет и то же ограничение на спот-источники для
        деривативов, что и HTXClient.trading_fee_rates — см. его докстринг."""
        market_type_value = market_type or settings.MARKET_TYPE
        is_derivative = market_type_value in ["swap", "futures", "perp"]

        maker = None
        taker = None
        source = "fallback_settings"

        if is_derivative:
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
                log_event(logger, logging.WARNING, "okx_contract_fee_meta_error", symbol=symbol, error=str(e))
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
                log_event(logger, logging.WARNING, "okx_market_fee_meta_error", symbol=symbol, error=str(e))

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
