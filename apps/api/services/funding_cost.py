"""Фондирование: расчёт по факту, а не константой.

Было в CostEngine:

    funding_buffer = notional * FUNDING_BUFFER_PCT * holding_funding_periods
    # 0.03%, ровно 1 период, всегда расход, для обеих сторон

Три ошибки:

1. Период. HTX списывает фондирование раз в 8 ч, Kraken — раз в час. Сделка
   на 40 минут пересекает расчёт HTX с вероятностью ~1/12, а модель брала
   полный период всегда.
2. Знак. При ставке > 0 лонг платит, шорт получает. Шорту начислялся расход.
3. Ставка. 0.03% константой при фактически наблюдаемых значениях (BTC/USDT
   в storage/ml/funding_rates.jsonl: 0.08%/8ч).

Правильная арифметика уже была в репозитории — funding_arbitrage считает
`held_periods = held_hours / 8` и знает, что longs pay shorts; cross_funding_arb
приводит обе площадки к часу. Направленные движки ей не пользовались.

Единицы: ставка в ПРОЦЕНТАХ за один период площадки (как в funding_rates.jsonl).
Результат в USDT, знак «плюс = расход».
"""
from __future__ import annotations

from core.config import settings

# Каденция расчёта по площадкам.
PERIOD_HOURS = {
    "htx": 8.0,
    "huobi": 8.0,
    "kraken": 1.0,
}

_LONG = ("long", "buy")


def period_hours(venue: str | None = None) -> float:
    key = str(venue or "").lower().strip()
    if key in PERIOD_HOURS:
        return PERIOD_HOURS[key]
    return float(getattr(settings, "FUNDING_PERIOD_HOURS_DEFAULT", 8.0))


def is_derivative(market_type: str | None) -> bool:
    return str(market_type or "").lower() in ("swap", "futures", "perp")


def observed_rate_pct(symbol: str) -> float | None:
    """Наблюдаемая ставка из журнала. None — наблюдений нет."""
    try:
        from services import funding_rate_history

        stats = funding_rate_history.stability(symbol)
        if int(stats.get("observations") or 0) <= 0:
            return None
        rate = stats.get("mean_rate_pct")
        return float(rate) if rate is not None else None
    except Exception:  # noqa: BLE001 — журнал не на крит-пути
        return None


def periods_elapsed(hold_hours: float | None, venue: str | None = None) -> float:
    """Сколько расчётных периодов приходится на удержание.

    Непрерывная амортизация, как в cross_funding_arb: списание дискретно, но
    на большом числе сделок несмещённо, а для одной сделки заранее неизвестно,
    попадёт ли она на расчёт. Отрицательное/пустое время → 0.
    """
    if hold_hours is None:
        hold_hours = float(getattr(settings, "FUNDING_EXPECTED_HOLD_HOURS", 1.0))
    return max(0.0, float(hold_hours)) / period_hours(venue)


def funding_usdt(
    *,
    notional: float,
    side: str,
    market_type: str | None,
    hold_hours: float | None = None,
    rate_pct: float | None = None,
    venue: str | None = None,
    symbol: str | None = None,
) -> float:
    """Фондирование за удержание. Плюс — расход, минус — доход.

    Спот фондирования не платит и не получает — там всегда 0.
    """
    if not is_derivative(market_type):
        return 0.0

    if rate_pct is None and symbol:
        rate_pct = observed_rate_pct(symbol)
    if rate_pct is None:
        rate_pct = float(getattr(settings, "FUNDING_FALLBACK_RATE_PCT", 0.01))

    amount = float(notional) * (float(rate_pct) / 100.0) * periods_elapsed(hold_hours, venue)

    # Ставка > 0: лонг платит, шорт получает. При отрицательной — наоборот,
    # знак переворачивается сам.
    if str(side or "").lower() in _LONG:
        return round(amount, 8)
    return round(-amount, 8)
