"""Триггер трендового входа: состояние → событие (#trend-trigger-2026-07-30).

Почему движок терял деньги
--------------------------
Условие входа `trend_up_candidate` — чистая проверка СОСТОЯНИЯ:

    h4.trend == "trend_up" AND h1.trend == "trend_up"
    AND m15.momentum in ("bullish", "neutral")

где `trend_up` это `price > ema20 > ema50`. На 4h это EMA20 ≈ 3.3 суток и
EMA50 ≈ 8 суток — условие держится ДНЯМИ и не говорит ничего о том, где мы
внутри движения: в начале импульса или на его вершине.

Зона входа при этом строится так (`_build_long_levels`):

    entry_from = last * 0.997
    entry_to   = last * 1.003

то есть «текущая цена ±0.3%». Ни отката, ни пробоя, ни опоры. Сложив одно с
другим, получаем: пока многодневное условие истинно, движок покупает по той
цене, на которой его застал очередной скан. Вход — случайная выборка из
распределения цен внутри тренда.

У случайного входа направленного матожидания нет по построению, и замер это
подтверждает: средний ход на 2 ч −0.111% при 95% ДИ [−0.315; +0.112] —
неотличимо от нуля. А вот РЕАЛИЗОВАННЫЙ результат достоверно отрицателен:
−0.247R, ДИ [−0.429; −0.058], одинаково на обеих половинах выборки
(H1 −0.250, H2 −0.239). Противоречия нет: нулевой edge, пропущенный через
издержки 0.15% и геометрию, где цель берётся в 14% случаев, а стоп в 42%,
даёт устойчивый минус. Это не рыночное явление, а арифметика конструкции —
поэтому и воспроизводится на любом периоде.

Что делает этот модуль
----------------------
Добавляет во вход недостающее измерение — РАСТЯНУТОСТЬ. Вопрос «тренд есть?»
заменяется на «тренд есть И мы не гонимся за ценой?».

    extension = (last - ema20) / atr14        для лонга
    extension = (ema20 - last) / atr14        для шорта

Расстояние до опоры в единицах ATR — величина, сравнимая между инструментами
и режимами волатильности (в отличие от процентов). Вход разрешён, пока
`extension <= TREND_MAX_EXTENSION_ATR`. Цена ушла от EMA20 дальше — импульс
уже состоялся, покупать его поздно: сделка получает то же расстояние до стопа,
но меньше свободного хода до ближайшего сопротивления.

Почему именно так, а не подбором порога. Величина не оптимизирована по
доходности — она не могла быть оптимизирована честно: 31 траектория даёт
интервал шире самого эффекта. Порог 1.5 ATR взят как «цена в пределах
обычного дневного колебания от опоры», и в этом смысл: правило описывает
геометрию входа, а не подогнано под прошлый P&L. Проверять его нужно НОВОЙ
выборкой, а не этой — поэтому решение пишется в `plan_json.trend_trigger`,
чтобы через 100 сделок сравнить входы со сработавшим и несработавшим
правилом, а не гадать.

(#reversal-min-ext-2026-11-24) Дополнение: отрицательное значение теперь
проверяется ОТДЕЛЬНЫМ правилом (TREND_MIN_EXTENSION_ENABLED / _ATR) —
не как растянутость, а как «возобновления ещё не было». Верхний порог
по-прежнему трогает только положительную сторону; формула и знак не
изменились.

Ограничение, которое важно назвать
----------------------------------
Это половина перехода «состояние → событие». Настоящее событие — откат к
опоре И возобновление (цена ушла ниже EMA20 и вернулась выше) — требует
истории баров между сканами, которой `TimeframeContext` не хранит: он отдаёт
только последний срез. Здесь реализована та часть, что считается из текущего
среза: запрет входа в растяжении. Полный триггер с подтверждением возобновления —
следующий шаг, и он потребует хранения состояния между итерациями цикла.

(#reversal-min-ext-2026-11-24) Ограничение закрыто ЧАСТИЧНО: нижний порог
требует, чтобы цена была на верной стороне EMA20 — это доступная из текущего
среза половина «возобновления». Полное подтверждение (ушла ниже и вернулась
выше) по-прежнему требует хранения состояния между итерациями цикла.
Чистые функции над TimeframeContext, тестируется без ccxt/pandas.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from core.config import settings

# Режимы, к которым правило применяется. CRT, scalp, range и reversal строят
# вход от структуры (свип, микро-край, граница коридора, опора разворота) —
# у них растянутость либо уже учтена, либо не имеет смысла.
TREND_REGIMES = frozenset({"trend_up_candidate", "trend_down_candidate"})


@dataclass(frozen=True)
class TrendTrigger:
    allowed: bool
    reason: str
    extension_atr: float | None
    max_extension_atr: float
    regime: str
    side: str

    def as_dict(self) -> dict:
        return asdict(self)


def _value(ctx, key: str):
    if ctx is None:
        return None
    if isinstance(ctx, dict):
        return ctx.get(key)
    return getattr(ctx, key, None)


def _passthrough(regime: str, side: str, reason: str) -> TrendTrigger:
    return TrendTrigger(
        allowed=True,
        reason=reason,
        extension_atr=None,
        max_extension_atr=float(getattr(settings, "TREND_MAX_EXTENSION_ATR", 1.5)),
        regime=regime,
        side=side,
    )


def extension_in_atr(ctx, side: str) -> float | None:
    """Насколько цена ушла от EMA20 в единицах ATR. None — посчитать нельзя.

    Знак направленный: положительное значение = цена ушла В СТОРОНУ сделки,
    то есть вход догоняет уже состоявшееся движение. Отрицательное значение
    означает откат ПРОТИВ направления сделки — это не растянутость, и правило
    такой вход не трогает.
    """
    last = _value(ctx, "last_close")
    ema20 = _value(ctx, "ema20")
    atr = _value(ctx, "atr14")
    try:
        last = float(last)
        ema20 = float(ema20)
        atr = float(atr)
    except (TypeError, ValueError):
        return None
    if atr <= 0:
        return None
    delta = last - ema20 if str(side).lower() in ("long", "buy") else ema20 - last
    return delta / atr


def evaluate(contexts, *, regime: str, side: str) -> TrendTrigger:
    """Разрешён ли трендовый вход при текущей растянутости цены.

    fail-open: нет данных / нет контекста / выключено флагом → вход разрешён.
    Гейт входа не имеет права ронять цикл из-за отсутствующего индикатора.
    """
    regime_value = str(regime or "")
    side_value = str(side or "").lower()

    if regime_value not in TREND_REGIMES:
        return _passthrough(regime_value, side_value, "not_a_trend_regime")
    if not bool(getattr(settings, "TREND_TRIGGER_ENABLED", True)):
        return _passthrough(regime_value, side_value, "disabled")

    entry_tf = str(getattr(settings, "TREND_TRIGGER_TF", "15m"))
    ctx = None
    if isinstance(contexts, dict):
        ctx = contexts.get(entry_tf) or contexts.get("15m") or contexts.get("5m")
    if ctx is None:
        return _passthrough(regime_value, side_value, "no_context")

    extension = extension_in_atr(ctx, side_value)
    if extension is None:
        return _passthrough(regime_value, side_value, "no_indicators")

    limit = float(getattr(settings, "TREND_MAX_EXTENSION_ATR", 1.5))
    over_limit = extension > limit

    # Порог 1.5 ATR НЕ откалиброван: `extension_atr` никогда не записывался, и
    # его распределение на живых сканах неизвестно. Правило может резать 5%
    # трендовых входов, а может 80% — предсказать нечем. Поэтому режим по
    # умолчанию `shadow`: величина считается и пишется в план, вход не
    # блокируется. Через несколько дней распределение известно, порог ставится
    # по нему, и режим переключается в `enforce`.
    #
    # Это не осторожность ради осторожности. Ровно так — блокирующим правилом
    # с порогом «на глаз» — уже была отгружена гипотеза геометрии, которая на
    # правильном замере оказалась вредной (см. services/setup_reach.py).
    mode = str(getattr(settings, "TREND_TRIGGER_MODE", "shadow")).lower().strip()
    enforcing = mode == "enforce"

    # (#reversal-min-ext-2026-11-24) НИЖНЯЯ граница extension — вторая половина
    # перехода «состояние → событие», названная в докстринге модуля ограничением.
    # Верхний порог ловит «цена улетела от опоры», но не «цена ещё ПРОТИВ тренда»:
    # extension < 0 означает, что откат не завершён и возобновления не было —
    # вход ловит нож, а не продолжает движение.
    #
    # Замер по свежей ноябрьской когорте (API /signals, трендовые входы):
    #   extension < 0    24 сделки  −24.29 USDT  −1.01/сделку  ← худший бакет
    #   extension 0–2   139 сделок  −21.44 USDT  −0.15/сделку
    #   extension > 2    15 сделок  −10.15 USDT  −0.68/сделку
    #
    # Полное подтверждение возобновления требует истории баров между сканами
    # (см. «Ограничение» в докстринге). Здесь — его часть, считаемая из текущего
    # среза: цена обязана быть на ВЕРНОЙ стороне EMA20 по направлению сделки.
    #
    # Флаг отдельный, потому что 0.0 — РАБОЧЕЕ значение порога, а не «выключено»:
    # выключение делается флагом, а не подстановкой отрицательного числа.
    # Тень пишет вердикт в план (reason *_shadow), вход не блокирует — как и
    # верхний порог, чтобы правило можно было проверить на новой выборке.
    min_ext_enabled = bool(getattr(settings, "TREND_MIN_EXTENSION_ENABLED", False))
    min_ext = float(getattr(settings, "TREND_MIN_EXTENSION_ATR", 0.0))
    under_limit = min_ext_enabled and extension < min_ext
    if under_limit:
        return TrendTrigger(
            allowed=not enforcing,
            reason="below_ema20_not_resumed" if enforcing else "below_ema20_not_resumed_shadow",
            extension_atr=round(extension, 4),
            max_extension_atr=limit,
            regime=regime_value,
            side=side_value,
        )

    if over_limit:
        return TrendTrigger(
            allowed=not enforcing,
            reason="extended_from_ema20" if enforcing else "extended_from_ema20_shadow",
            extension_atr=round(extension, 4),
            max_extension_atr=limit,
            regime=regime_value,
            side=side_value,
        )

    return TrendTrigger(
        allowed=True,
        reason="within_pullback_band",
        extension_atr=round(extension, 4),
        max_extension_atr=limit,
        regime=regime_value,
        side=side_value,
    )
