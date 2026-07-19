"""Read-only клиент Kraken Futures (P1 интеграции, #kraken-p1-2026-07-18).

ТОЛЬКО публичные данные: тикеры, фандинг, стакан. Ни ключей, ни ордеров —
торговый контур HTX не затрагивается. Паттерны повторяют HTXClient:
класс-левел кэш рынков (share между инстансами), retry с джиттером, fail-open.

Kraken Futures matching — AWS eu-west-1 (Дублин); latency некритична для
наших задач P1 (телеметрия funding-спреда, второй источник данных).
IPv4-форс urllib3 уже применён глобально при импорте htx_client.
"""

import logging
import random
import time

import ccxt
from core.config import settings
from core.logging import get_logger, log_event

logger = get_logger(__name__)


def map_to_kraken_symbol(symbol: str, quote: str | None = None) -> str:
    """Наш формат 'BTC/USDT' → unified-символ линейного перпа Kraken 'BTC/USD:USD'.

    Kraken Futures котирует и маржирует перпы в USD (multi-collateral PF_*).
    Сравнение цен HTX(USDT) ↔ Kraken(USD) несёт базис USDT/USD — это учитывает
    venue_compare, здесь только маппинг.
    """
    q = (quote or getattr(settings, "KRAKEN_QUOTE", "USD") or "USD").upper()
    base = symbol.split("/", 1)[0].strip().upper()
    return f"{base}/{q}:{q}"


class KrakenClient:
    # Кросс-инстансный кэш рынков — как в HTXClient: повторный KrakenClient()
    # не дёргает load_markets() заново.
    _markets_loaded: bool = False
    _cached_markets: dict = {}

    def __init__(self):
        exchange_config: dict = {
            "enableRateLimit": True,
            "timeout": int(getattr(settings, "KRAKEN_TIMEOUT_MS", 20000)),
        }

        proxy_url = str(getattr(settings, "KRAKEN_PROXY_URL", "") or "").strip()
        if proxy_url:
            exchange_config["proxies"] = {"http": proxy_url, "https": proxy_url}
            log_event(
                logger,
                logging.INFO,
                "kraken_using_proxy",
                proxy=proxy_url[:30] + "..." if len(proxy_url) > 30 else proxy_url,
            )

        self.exchange = ccxt.krakenfutures(exchange_config)

        if KrakenClient._cached_markets:
            self._inject_markets(KrakenClient._cached_markets)

    def _inject_markets(self, markets: dict) -> None:
        """(#kraken-markets-by-id-2026-07-19) Кэш рынков инжектим через
        set_markets(): сырое присваивание exchange.markets оставляло внутренний
        индекс ccxt markets_by_id = None, и safe_market() внутри
        fetch_funding_rates падал «argument of type 'NoneType' is not iterable»
        у каждого нового инстанса, получившего кэш (первый после старта работал,
        последующие — нет)."""
        try:
            self.exchange.set_markets(markets)
        except Exception:  # noqa: BLE001 — хуже сырого присваивания не будет
            self.exchange.markets = markets

    def _retry(self, fn, *args, retries: int = 3, delay: float = 1.5, **kwargs):
        """Retry с экспоненциальным бэкоффом + джиттер ±20% (паттерн HTXClient)."""
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                last_error = e
                log_event(logger, logging.WARNING, "kraken_retry", attempt=attempt, retries=retries, error=str(e))
                if attempt < retries:
                    base = delay * attempt
                    jitter = base * 0.2 * (random.random() * 2 - 1)
                    time.sleep(max(0.1, base + jitter))
        raise last_error

    def load_markets(self) -> dict:
        if KrakenClient._cached_markets:
            if not self.exchange.markets or getattr(self.exchange, "markets_by_id", None) is None:
                self._inject_markets(KrakenClient._cached_markets)
            return KrakenClient._cached_markets
        if self.exchange.markets:
            KrakenClient._markets_loaded = True
            KrakenClient._cached_markets = self.exchange.markets
            return self.exchange.markets
        try:
            result = self._retry(self.exchange.load_markets, retries=3, delay=2.0)
            KrakenClient._markets_loaded = True
            KrakenClient._cached_markets = result
            log_event(logger, logging.INFO, "kraken_markets_loaded", count=len(result))
            return result
        except Exception as e:  # noqa: BLE001
            log_event(logger, logging.ERROR, "kraken_load_markets_failed", error=str(e))
            return {}

    def has_market(self, symbol: str) -> bool:
        markets = self.load_markets()
        return bool(markets) and symbol in markets

    def fetch_ticker(self, symbol: str) -> dict:
        self.load_markets()
        return self._retry(self.exchange.fetch_ticker, symbol)

    def fetch_mark_price(self, symbol: str) -> float:
        ticker = self.fetch_ticker(symbol) or {}
        return float(
            ticker.get("mark")
            or ticker.get("markPrice")
            or ticker.get("last")
            or ticker.get("close")
            or ticker.get("bid")
            or ticker.get("ask")
            or 0.0
        )

    def fetch_funding_rates(self, symbols: list[str] | None = None) -> dict:
        """Bulk-фандинг перпов одним вызовом (ccxt fetchFundingRates).

        (#kraken-tickers-fix-2026-07-19) Вызывать ccxt БЕЗ списка символов нельзя:
        /tickers Kraken содержит и истёкшие датированные фьючерсы (напр.
        FI_XRPUSD_250131), которых нет в карте рынков — парсер ccxt падает
        («does not have market symbol»). Со списком ccxt отфильтровывает тикеры
        ДО парсинга. Поэтому: переданные символы режем до существующих на бирже;
        без аргумента — все перпы из карты рынков. Возвращает
        {unified_symbol: entry}; fail-open: при сбое {} (вызывающий код падает
        на пер-символьный фолбэк или помечает ошибку).
        """
        markets = self.load_markets()
        if not markets:
            return {}
        if symbols:
            wanted = [s for s in symbols if s in markets]
        else:
            wanted = [s for s, m in markets.items() if (m or {}).get("swap")]
        if not wanted:
            return {}
        try:
            result = self._retry(self.exchange.fetch_funding_rates, wanted, retries=2, delay=1.5)
            return result if isinstance(result, dict) else {}
        except Exception as e:  # noqa: BLE001
            log_event(logger, logging.WARNING, "kraken_funding_rates_failed", error=str(e))
            return {}

    def fetch_funding_rate(self, symbol: str) -> dict:
        self.load_markets()
        return self._retry(self.exchange.fetch_funding_rate, symbol, retries=2, delay=1.5)

    def fetch_order_book(self, symbol: str, limit: int = 25) -> dict:
        self.load_markets()
        return self._retry(self.exchange.fetch_order_book, symbol, limit)

    def ping(self) -> dict:
        """Health-проба: время ответа тикера BTC. Для /venues/health."""
        started = time.monotonic()
        try:
            price = self.fetch_mark_price(map_to_kraken_symbol("BTC/USDT"))
            return {
                "ok": price > 0,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "btc_mark": price,
            }
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "error": str(e),
            }
