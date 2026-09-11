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

Закрытая сделка — по расчётам, а не по часам (#funding-settlements-2026-09-12)
------------------------------------------------------------------------------
Фондирование списывается дискретно: платит тот, кто держит позицию в момент
расчёта (HTX и OKX — 00:00, 08:00, 16:00 UTC; Kraken — каждый час). Для
закрытой части позиции время известно, и считается число расчётов, которые
она пересекла: сделка 07:30–08:30 платит один полный период, 08:30–15:59 — ни
одного. Непрерывная амортизация (часы / период) остаётся только для оценки на
этапе плана, когда время выхода неизвестно. До этой правки закрытие брало
плановую оценку — 1 час на любую сделку, — и шорт на двое суток недосчитывал
шесть расчётов.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.config import settings

# Каденция расчёта по площадкам.
PERIOD_HOURS = {
    "htx": 8.0,
    "huobi": 8.0,
    "okx": 8.0,
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


def _utc_hours(moment: datetime) -> float:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp() / 3600.0


def settlements_crossed(opened_at: datetime, closed_at: datetime,
                        venue: str | None = None) -> int:
    """Сколько расчётов фондирования позиция пересекла: моменты t = k·период
    от 00:00 UTC, открытие < t ≤ закрытие. Пустое или обратное окно — 0."""
    period = period_hours(venue)
    start, end = _utc_hours(opened_at), _utc_hours(closed_at)
    if end <= start or period <= 0:
        return 0
    # Сравнение в целых секундах: 08:00:00 не должно стать 07:59:59.999.
    period_s = int(round(period * 3600))
    start_s, end_s = int(round(start * 3600)), int(round(end * 3600))
    return max(0, end_s // period_s - start_s // period_s)


def funding_usdt(
    *,
    notional: float,
    side: str,
    market_type: str | None,
    hold_hours: float | None = None,
    rate_pct: float | None = None,
    venue: str | None = None,
    symbol: str | None = None,
    opened_at: datetime | None = None,
    closed_at: datetime | None = None,
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

    if opened_at is not None and closed_at is not None:
        periods = float(settlements_crossed(opened_at, closed_at, venue))
    else:
        periods = periods_elapsed(hold_hours, venue)
    amount = float(notional) * (float(rate_pct) / 100.0) * periods

    # Ставка > 0: лонг платит, шорт получает. При отрицательной — наоборот,
    # знак переворачивается сам.
    if str(side or "").lower() in _LONG:
        return round(amount, 8)
    return round(-amount, 8)
