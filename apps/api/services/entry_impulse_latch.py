"""Защёлка импульса входа: событие ждёт состояние (#entry-impulse-2026-09-04).

Задача
------
Трендовый кандидат — СОСТОЯНИЕ. Оно требует согласия 4h и 1h и держится сутками;
в коде это записано ещё 30.07: «движок покупает по цене очередного скана — вход
оказывается случайной выборкой из распределения цен внутри тренда».

Условия входа по ТЗ — СОБЫТИЯ. «ADX развернулся вверх», «Stoch пересёк сигнальную»
истинны один бар.

Оба требуются истинными ОДНОВРЕМЕННО. Но они последовательны по природе: импульс
случается на младшем ТФ раньше, чем тренд успевает проступить на 4h и 1h. К
моменту, когда состояние становится истинным, событие уже прошло.

Замер 04.09 показывает это в лоб: из 71 оценки условий ТЗ 68 отвалились по
`adx_not_rising`, медиана `adx_delta` −0.589, максимум за сутки +0.28. То есть в
момент, когда наш детектор говорит «кандидат», сила тренда уже падает почти
всегда. Не пороговая проблема — фазовая.

Что делает защёлка
------------------
Запоминает импульс на несколько минут. Когда состояние наконец подтверждается,
недавнее событие ещё «держится», и вход разрешается — при том, что НИ ОДНО
условие не ослаблено. Меняется только требование одновременности, которого
рынок не обязан выполнять.

Что она НЕ делает
-----------------
Не удлиняет жизнь импульса задним числом: окно отсчитывается от момента события
и истекает само. Не заменяет собой направление, объём и структуру — kama, di и
obv остаются как были. Подменяет ровно одно условие — `adx_rising`, и только то
его прочтение, что ADX обязан расти ИМЕННО СЕЙЧАС.

Режим по умолчанию — shadow: защёлка считается и пишется в события, вход не
меняется. В репозитории уже дважды отгружали правило с порогом «на глаз», и один
раз замер показал вред (см. services/setup_reach.py). Сначала цифра «сколько
блокировок имели живую защёлку», потом enforce.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from core.config import settings

# События, которые считаются импульсом. Оба — переходы, а не состояния: их
# видно только по сравнению с предыдущим баром, и именно поэтому они не могут
# ждать, пока подтвердится 4h.
IMPULSE_ADX_TURN = "adx_turned_up"
IMPULSE_STOCH_CROSS = "stoch_crossed"


@dataclass(frozen=True)
class Impulse:
    """Снимок импульса на момент события."""
    side: str
    kind: str
    at: float
    adx: float | None
    adx_delta: float | None
    stoch_k: float | None
    stoch_d: float | None

    def age_sec(self, now: float) -> float:
        return max(0.0, now - self.at)

    def as_dict(self, now: float) -> dict:
        return {
            "side": self.side,
            "kind": self.kind,
            "age_sec": round(self.age_sec(now), 1),
            "adx": self.adx,
            "adx_delta": self.adx_delta,
            "stoch_k": self.stoch_k,
            "stoch_d": self.stoch_d,
        }


def _num(source, key: str) -> float | None:
    if not isinstance(source, dict):
        source = getattr(source, "__dict__", {}) or {}
    try:
        value = source.get(key)
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _entry_ctx(timeframes):
    if not isinstance(timeframes, dict):
        return None
    name = str(getattr(settings, "ENTRY_IMPULSE_TF", "15m"))
    return timeframes.get(name) or timeframes.get("15m") or timeframes.get("5m")


def detect(timeframes, side: str) -> tuple[str | None, dict]:
    """Импульс в сторону `side` прямо сейчас: (вид события, показания).

    Показания возвращаются ВСЕГДА, даже когда события нет: в разборе нужно
    видеть, чего не хватило, иначе защёлка превращается в «просто не сработала».
    """
    ctx = _entry_ctx(timeframes)
    readings = {"adx": None, "adx_delta": None, "stoch_k": None, "stoch_d": None}
    if ctx is None:
        return None, readings

    adx = _num(ctx, "adx14")
    adx_prev = _num(ctx, "adx14_prev")
    k, k_prev = _num(ctx, "stoch_rsi_k"), _num(ctx, "stoch_rsi_k_prev")
    d, d_prev = _num(ctx, "stoch_rsi_d"), _num(ctx, "stoch_rsi_d_prev")

    readings = {
        "adx": adx,
        "adx_delta": (round(adx - adx_prev, 4)
                      if adx is not None and adx_prev is not None else None),
        "stoch_k": k,
        "stoch_d": d,
    }

    rise_min = float(getattr(settings, "ENTRY_IMPULSE_ADX_RISE_MIN", 0.0))
    if readings["adx_delta"] is not None and readings["adx_delta"] > rise_min:
        return IMPULSE_ADX_TURN, readings

    # Пересечение — событие одного бара, и без предыдущих значений его не
    # отличить от «уже давно выше». Поэтому нужны все четыре числа.
    if None not in (k, k_prev, d, d_prev):
        crossed_up = k_prev <= d_prev and k > d
        crossed_down = k_prev >= d_prev and k < d
        if (side == "long" and crossed_up) or (side == "short" and crossed_down):
            return IMPULSE_STOCH_CROSS, readings

    return None, readings


class ImpulseLatch:
    """Живёт в памяти цикла: перезапуск обнуляет — и это честно.

    Защёлка утверждает «импульс был N минут назад». После перезапуска мы этого
    не знаем, и восстановленная из ниоткуда запись означала бы разрешение входа
    по событию, которого никто не видел.
    """

    def __init__(self) -> None:
        self._latched: dict[tuple[str, str], Impulse] = {}

    def observe(self, symbol: str, side: str, timeframes, *,
                now: float | None = None) -> Impulse | None:
        """Замечает импульс и продлевает защёлку. Возвращает свежее событие."""
        if side not in ("long", "short"):
            return None
        moment = time.time() if now is None else now
        kind, readings = detect(timeframes, side)
        if kind is None:
            return None

        impulse = Impulse(side=side, kind=kind, at=moment, **readings)
        self._latched[(str(symbol), side)] = impulse
        return impulse

    def live(self, symbol: str, side: str, *, now: float | None = None) -> Impulse | None:
        """Импульс, ещё не истёкший. Просроченный сразу выбрасывается."""
        moment = time.time() if now is None else now
        key = (str(symbol), str(side))
        impulse = self._latched.get(key)
        if impulse is None:
            return None
        if impulse.age_sec(moment) > _window_sec():
            del self._latched[key]
            return None
        return impulse

    def snapshot(self, symbol: str, side: str, *, now: float | None = None) -> dict:
        """Состояние защёлки для записи в событие разбора."""
        moment = time.time() if now is None else now
        impulse = self.live(symbol, side, now=moment)
        return {
            "mode": mode(),
            "window_sec": _window_sec(),
            "live": impulse is not None,
            "impulse": impulse.as_dict(moment) if impulse else None,
        }

    def forget(self, symbol: str, side: str) -> None:
        self._latched.pop((str(symbol), str(side)), None)


def _window_sec() -> float:
    return float(getattr(settings, "ENTRY_IMPULSE_WINDOW_SEC", 1800.0))


def mode() -> str:
    return str(getattr(settings, "ENTRY_IMPULSE_LATCH_MODE", "shadow")).lower().strip()


def substitutes_adx_rising(latch_snapshot: dict) -> bool:
    """Снимает ли живая защёлка отказ `adx_not_rising`.

    Только в enforce и только для этого одного условия. Направление (di), сторона
    KAMA и объём (obv) остаются нетронутыми: защёлка утверждает «импульс был
    недавно», а не «всё остальное тоже в порядке».
    """
    return bool(latch_snapshot.get("live")) and mode() == "enforce"
