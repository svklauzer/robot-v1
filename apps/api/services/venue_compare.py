"""Кросс-биржевая телеметрия HTX ↔ Kraken (P1 read-only, #kraken-p1-2026-07-18).

Зачем: решение о межбиржевом funding-арбе (P2) должно приниматься по фактическим
спредам фандинга, а не по предположениям; заодно это второй независимый источник
рыночных данных (задел под data-failover, P3). Модуль НИЧЕГО не торгует и не
трогает торговый контур — чистая математика + два read-only клиента.

Ключевая нормализация: HTX платит фандинг раз в 8ч, Kraken Futures (не-US) — раз
в час. Сырые per-period ставки НЕ сравнимы → приводим к per-hour и годовым.
Цены: перпы HTX в USDT, Kraken — в USD, поэтому price_diff_pct содержит базис
USDT/USD (~0.05–0.1%) — это НЕ арбитражная щель сама по себе.

Kraken-ставка: ccxt для krakenfutures может отдавать в fundingRate абсолютное
значение (USD на контракт, известная странность #25343) — берём
info.relativeFundingRate (относительная ставка к mark) с фолбэком на unified.
"""

import time
from typing import Any

from core.config import settings

HOURS_PER_YEAR = 24 * 365


# ── Чистые функции (тестируются без ccxt) ────────────────────────────────────

def normalize_funding(rate: float, interval_hours: float) -> dict:
    """Per-period ставка → per-hour и годовая, в процентах."""
    interval = float(interval_hours) if interval_hours else 1.0
    per_hour = float(rate) / interval if interval > 0 else float(rate)
    return {
        "per_period_pct": round(float(rate) * 100, 6),
        "interval_hours": interval,
        "per_hour_pct": round(per_hour * 100, 6),
        "annualized_pct": round(per_hour * HOURS_PER_YEAR * 100, 2),
    }


def funding_spread(
    htx_rate: float,
    kraken_rate: float,
    htx_interval_hours: float | None = None,
    kraken_interval_hours: float | None = None,
    round_trip_fee_pct: float | None = None,
) -> dict:
    """Спред фандинга между биржами в сопоставимых единицах + экономика.

    Знак: spread > 0 → на HTX фандинг выше → доходная конструкция
    «шорт перпа HTX + лонг перпа Kraken» (получаем больший фандинг, платим
    меньший). Обе ноги — перпы, дельта-нейтрально, переводов монет нет.
    break_even_days — за сколько дней спред окупает round-trip комиссии
    обеих ног (вход+выход, тейкер обеих бирж).
    """
    h_int = float(htx_interval_hours or getattr(settings, "HTX_FUNDING_INTERVAL_HOURS", 8.0))
    k_int = float(kraken_interval_hours or getattr(settings, "KRAKEN_FUNDING_INTERVAL_HOURS", 1.0))
    h = normalize_funding(htx_rate, h_int)
    k = normalize_funding(kraken_rate, k_int)

    spread_hourly_pct = h["per_hour_pct"] - k["per_hour_pct"]
    spread_daily_pct = spread_hourly_pct * 24
    spread_annualized_pct = spread_hourly_pct * HOURS_PER_YEAR

    if round_trip_fee_pct is None:
        htx_taker = float(getattr(settings, "FUTURES_TAKER_FEE", 0.0005))
        kraken_taker = float(getattr(settings, "KRAKEN_TAKER_FEE", 0.0005))
        # 2 ноги × (вход + выход): (htx + kraken) × 2, в % нотионала.
        round_trip_fee_pct = (htx_taker + kraken_taker) * 2 * 100

    break_even_days = (
        round(round_trip_fee_pct / abs(spread_daily_pct), 1)
        if abs(spread_daily_pct) > 1e-9
        else None
    )

    return {
        "htx": h,
        "kraken": k,
        "spread_hourly_pct": round(spread_hourly_pct, 6),
        "spread_daily_pct": round(spread_daily_pct, 6),
        "spread_annualized_pct": round(spread_annualized_pct, 2),
        "direction": "short_htx_long_kraken" if spread_hourly_pct >= 0 else "short_kraken_long_htx",
        "round_trip_fee_pct": round(float(round_trip_fee_pct), 4),
        "break_even_days": break_even_days,
    }


def kraken_rate_from_entry(entry: dict | None) -> float | None:
    """Относительная ставка из ccxt-ответа krakenfutures (см. докстринг модуля)."""
    if not entry:
        return None
    info = entry.get("info") or {}
    for key in ("relativeFundingRate", "relative_funding_rate"):
        value = info.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    value = entry.get("fundingRate")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ── Сервис ───────────────────────────────────────────────────────────────────

_CACHE: dict[str, tuple[float, dict]] = {}


class VenueCompareService:
    """Сравнение HTX ↔ Kraken по нашей вселенной символов. Fail-open по символу:
    ошибка одной пары не валит весь ответ."""

    def __init__(self, htx_client=None, kraken_client=None):
        if htx_client is None:
            from services.htx_client import HTXClient
            htx_client = HTXClient()
        if kraken_client is None:
            from services.kraken_client import KrakenClient
            kraken_client = KrakenClient()
        self.htx = htx_client
        self.kraken = kraken_client

    def compare(self, symbols: list[str] | None = None, use_cache: bool = True) -> dict:
        from services.funding_arbitrage import FundingSymbolMapper
        from services.kraken_client import map_to_kraken_symbol

        symbols = symbols or settings.symbols
        cache_key = ",".join(symbols)
        ttl = float(getattr(settings, "KRAKEN_COMPARE_CACHE_SEC", 60))
        now = time.time()
        if use_cache and cache_key in _CACHE:
            ts, payload = _CACHE[cache_key]
            if now - ts < ttl:
                return {**payload, "cached": True, "cache_age_sec": round(now - ts, 1)}

        kraken_symbols = [map_to_kraken_symbol(s) for s in symbols]
        kraken_bulk = self.kraken.fetch_funding_rates(kraken_symbols)

        items: list[dict] = []
        for symbol in symbols:
            item: dict[str, Any] = {"symbol": symbol}
            k_symbol = map_to_kraken_symbol(symbol)
            item["kraken_symbol"] = k_symbol
            try:
                swap_symbol = FundingSymbolMapper.swap_symbol(symbol)
                htx_funding = self.htx.fetch_funding_rate(swap_symbol) or {}
                htx_rate = float(htx_funding.get("fundingRate") or htx_funding.get("rate") or 0.0)
                htx_mark = float(self.htx.fetch_mark_price(swap_symbol))

                k_entry = kraken_bulk.get(k_symbol)
                if k_entry is None:
                    # Bulk не покрыл символ (или упал) → пер-символьный фолбэк.
                    k_entry = self.kraken.fetch_funding_rate(k_symbol)
                k_rate = kraken_rate_from_entry(k_entry)
                if k_rate is None:
                    raise RuntimeError("kraken_funding_rate_unavailable")
                k_mark = float(
                    (k_entry.get("markPrice") if isinstance(k_entry, dict) else None)
                    or self.kraken.fetch_mark_price(k_symbol)
                )

                item["htx_mark"] = htx_mark
                item["kraken_mark"] = k_mark
                # Содержит базис USDT/USD — не арбитражная щель сама по себе.
                item["price_diff_pct"] = (
                    round((htx_mark - k_mark) / k_mark * 100, 4) if k_mark else None
                )
                item["spread"] = funding_spread(htx_rate, k_rate)
                item["kraken_next_funding_prediction_pct"] = _prediction_pct(k_entry)
            except Exception as e:  # noqa: BLE001
                item["error"] = str(e)
            items.append(item)

        ok_items = [i for i in items if "error" not in i]
        best = max(
            ok_items,
            key=lambda i: abs(i["spread"]["spread_annualized_pct"]),
            default=None,
        )
        payload = {
            "status": "ok" if ok_items else "error",
            "venues": {
                "htx": {"funding_interval_hours": float(getattr(settings, "HTX_FUNDING_INTERVAL_HOURS", 8.0)), "quote": "USDT"},
                "kraken": {"funding_interval_hours": float(getattr(settings, "KRAKEN_FUNDING_INTERVAL_HOURS", 1.0)), "quote": str(getattr(settings, "KRAKEN_QUOTE", "USD"))},
            },
            "items": items,
            "errors": len(items) - len(ok_items),
            "best_spread": (
                {
                    "symbol": best["symbol"],
                    "spread_annualized_pct": best["spread"]["spread_annualized_pct"],
                    "direction": best["spread"]["direction"],
                    "break_even_days": best["spread"]["break_even_days"],
                }
                if best
                else None
            ),
            "note": "Read-only телеметрия (P1). Спред>0 → шорт HTX-перпа + лонг Kraken-перпа. price_diff содержит базис USDT/USD.",
        }
        _CACHE[cache_key] = (now, payload)
        return {**payload, "cached": False}

    def health(self) -> dict:
        """Латентность и доступность обеих площадок (для будущего failover)."""
        started = time.monotonic()
        try:
            htx_mark = float(self.htx.fetch_mark_price("BTC/USDT:USDT"))
            htx = {"ok": htx_mark > 0, "latency_ms": round((time.monotonic() - started) * 1000, 1), "btc_mark": htx_mark}
        except Exception as e:  # noqa: BLE001
            htx = {"ok": False, "latency_ms": round((time.monotonic() - started) * 1000, 1), "error": str(e)}
        kraken = self.kraken.ping()
        return {"status": "ok", "htx": htx, "kraken": kraken}


def _prediction_pct(entry: dict | None) -> float | None:
    """funding_rate_prediction Kraken (следующий фандинг) — есть только у них."""
    if not isinstance(entry, dict):
        return None
    info = entry.get("info") or {}
    for key in ("relativeFundingRatePrediction", "relative_funding_rate_prediction"):
        value = info.get(key)
        if value is not None:
            try:
                return round(float(value) * 100, 6)
            except (TypeError, ValueError):
                return None
    return None
