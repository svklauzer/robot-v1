"""KAMA — адаптивная скользящая Кауфмана. (#tz-trend-engine-2026-08-03)

Зачем она вместо EMA
--------------------
EMA сглаживает одинаково независимо от того, движется рынок или стоит. KAMA
меняет скорость по «эффективности» хода: путь по прямой / сумма шагов. При
направленном движении она почти догоняет цену (период ~2), во флэте почти
замирает (период ~30).

Для нас это не косметика. Замер по 342 закрытым: у trend_up edge_ratio 0.943 —
ход ПРОТИВ сделки больше хода за неё. Так выглядит вход в случайный момент
внутри тренда, а он случайный и есть: условие `h4.trend == up AND h1.trend == up`
истинно сутками, а зона входа задана как `last × 0.997…1.003`, то есть «цена в
момент, когда до символа дошёл сканер».

KAMA даёт то, чего в движке нет: ЛИНИЮ, относительно которой вход имеет смысл.
Цена выше KAMA — тренд жив; возврат к KAMA — откат, в который можно входить;
закрытие ниже KAMA — тренд сломан и позицию пора закрывать. Одна линия
одновременно задаёт фильтр, точку входа и стоп.

Формула
-------
    ER    = |close[i] − close[i−n]| / Σ|close[j] − close[j−1]|   (n последних)
    SC    = (ER × (2/(fast+1) − 2/(slow+1)) + 2/(slow+1))²
    KAMA  = KAMA[i−1] + SC × (close[i] − KAMA[i−1])

Настройки ТЗ: n=10, fast=2, slow=30.

Чистые функции над списком цен — тестируется без pandas и рынка.
"""
from __future__ import annotations

ER_PERIOD = 10
FAST = 2
SLOW = 30


def kama_series(
    closes: list[float],
    *,
    er_period: int = ER_PERIOD,
    fast: int = FAST,
    slow: int = SLOW,
) -> list[float | None]:
    """KAMA по всей серии. None там, где данных ещё не хватает.

    Первое значение — простая цена закрытия на позиции er_period: линии нужна
    точка отсчёта, и брать её как SMA было бы лишним допущением.
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= er_period or er_period < 1:
        return out

    fast_sc = 2.0 / (float(fast) + 1.0)
    slow_sc = 2.0 / (float(slow) + 1.0)

    prev = float(closes[er_period])
    out[er_period] = prev

    for i in range(er_period + 1, n):
        change = abs(float(closes[i]) - float(closes[i - er_period]))
        volatility = 0.0
        for j in range(i - er_period + 1, i + 1):
            volatility += abs(float(closes[j]) - float(closes[j - 1]))

        # Нулевая волатильность = цена не двигалась. ER не определён;
        # честнее считать эффективность нулевой (рынок стоит), чем единичной.
        er = (change / volatility) if volatility > 0 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        prev = prev + sc * (float(closes[i]) - prev)
        out[i] = prev

    return out


def kama_last(closes: list[float], **kwargs) -> float | None:
    series = kama_series(closes, **kwargs)
    for value in reversed(series):
        if value is not None:
            return value
    return None


def efficiency_ratio(closes: list[float], er_period: int = ER_PERIOD) -> float | None:
    """Насколько ход направленный: 1 — прямая линия, 0 — топтание.

    Полезно отдельно от KAMA: это прямая мера «есть ли вообще движение»,
    и она не требует калибровки порога так остро, как ADX.
    """
    if len(closes) <= er_period:
        return None
    change = abs(float(closes[-1]) - float(closes[-1 - er_period]))
    volatility = sum(
        abs(float(closes[i]) - float(closes[i - 1]))
        for i in range(len(closes) - er_period, len(closes))
    )
    if volatility <= 0:
        return 0.0
    return change / volatility
