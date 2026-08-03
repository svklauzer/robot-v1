"""Пер-периодное начисление funding (#funding-periodic-accrual-2026-08-03).

Зачем
-----
Внутрибиржевой арбитраж считал доход по ставке ВХОДА за весь срок:

    funding = notional × entry_rate × periods

Правка 27.07 заменила это трапецией `(вход + выход) / 2`, что снимает основное
смещение, но остаётся приближением: ставка меняется каждые 8 часов, и среднее
двух крайних точек равно интегралу только на линейном участке.

Цена вопроса измерима. Все пять закрытых позиций держались ровно 30 периодов —
они упирались в `FUNDING_ARB_MAX_HOLD_HOURS = 240`, — и весь их результат
(+4.02 USDT) посчитан по ставке входа: сверка сходится точно,
100 × 0.000624 × 30 = 1.8716 при записанных 1.871629. По оценке в самом
конфиге завышение составляет 0.25–0.60 USDT на сделку, то есть от четверти до
трёх четвертей заявленного результата движка.

Межбиржевый арбитраж при этом начисляет carry pro-rata по ТЕКУЩЕЙ ставке и
показывает минус. Один принцип, разный знак, разница в методике учёта — это
и есть причина привести оба к одному способу.

Как считается
-------------
Накопление по фактической ставке за фактически прошедшее время:

    доход_за_шаг = нотионал × ставка_сейчас × (часы_с_прошлого_шага / 8)

Ledger живёт в `raw_json.accrual` — отдельной колонки не требуется, миграция не
нужна. Позиции, открытые до этой правки, ledger'а не имеют: для них расчёт
падает обратно на трапецию, и это помечено в результате явно, чтобы старые и
новые сделки не смешивались в отчётах.

Чистые функции над словарём — тестируется без БД и без биржи.
"""
from __future__ import annotations

PERIOD_HOURS = 8.0
LEDGER_KEY = "accrual"


def empty_ledger(now_ts: float, rate: float) -> dict:
    """Ledger новой позиции. Начисление стартует с момента открытия."""
    return {
        "accrued_usdt": 0.0,
        "periods": 0.0,
        "last_ts": float(now_ts),
        "last_rate": float(rate),
        "steps": 0,
        "method": "per_period",
    }


def read_ledger(raw_json: dict | None) -> dict | None:
    if not isinstance(raw_json, dict):
        return None
    ledger = raw_json.get(LEDGER_KEY)
    return ledger if isinstance(ledger, dict) else None


def accrue(
    ledger: dict,
    *,
    notional: float,
    current_rate: float,
    now_ts: float,
    period_hours: float = PERIOD_HOURS,
) -> dict:
    """Начислить carry с прошлого шага по ТЕКУЩЕЙ ставке.

    Ставка берётся на момент начисления, а не на момент входа: именно так
    ставка и работает — биржа платит по той, что действует в расчётный час.
    Время назад не идёт: отрицательный интервал начисления не даёт.
    """
    # Именно `is None`, а не `or`: last_ts == 0.0 — валидная отметка времени,
    # и `or` подменил бы её текущей, обнулив интервал начисления.
    raw_last = ledger.get("last_ts")
    last_ts = float(raw_last) if raw_last is not None else float(now_ts)
    elapsed_hours = max(0.0, (float(now_ts) - last_ts) / 3600.0)
    periods = elapsed_hours / float(period_hours)

    gain = float(notional) * float(current_rate) * periods
    return {
        "accrued_usdt": round(float(ledger.get("accrued_usdt") or 0.0) + gain, 8),
        "periods": round(float(ledger.get("periods") or 0.0) + periods, 6),
        "last_ts": float(now_ts),
        "last_rate": float(current_rate),
        "steps": int(ledger.get("steps") or 0) + (1 if periods > 0 else 0),
        "method": "per_period",
    }


def collected_usdt(
    raw_json: dict | None,
    *,
    notional: float,
    entry_rate: float,
    exit_rate: float | None,
    periods: int,
    honest_trapezoid: bool = True,
) -> tuple[float, str]:
    """Сколько собрано на закрытии. Возвращает (сумма, способ расчёта).

    Приоритет — фактический ledger. Его отсутствие означает позицию, открытую
    до этой правки; для неё считаем прежним способом и помечаем результат,
    чтобы в отчётах было видно, какие сделки чем измерены.
    """
    ledger = read_ledger(raw_json)
    if ledger and float(ledger.get("periods") or 0.0) > 0:
        return round(float(ledger.get("accrued_usdt") or 0.0), 6), "per_period"

    if honest_trapezoid and exit_rate is not None:
        rate = (float(entry_rate) + float(exit_rate)) / 2.0
        return round(float(notional) * rate * int(periods), 6), "trapezoid_legacy"

    return round(float(notional) * float(entry_rate) * int(periods), 6), "entry_rate_legacy"
