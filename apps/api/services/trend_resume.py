"""Событие «возобновление тренда» — наблюдение в тени (#trend-resume-2026-10-23).

Зачем
-----
Трендовый кандидат — СОСТОЯНИЕ (`price < EMA20 < EMA50` на 4h и 1h), оно держится
часами и днями и не говорит, где цена внутри движения. Вход идёт «по цене очередного
скана». Вечер 22–23.10: пять шортов подряд, ни один не вышел в заметную прибыль
(макс. прибыль 0.0–0.5% при стопе 1.0–1.6%), все получили стоп при развороте.
`trend_trigger` смотрит только расстояние до EMA20 и это не объясняет исход (замер
по 300 сделкам). Не хватает именно СОБЫТИЯ: откат к опоре и возобновление.

Что делает модуль
-----------------
Только НАБЛЮДАЕТ. На каждом скане запоминает по символу, где цена относительно EMA20
старшего ТФ (по умолчанию 1h) в ATR, и по запросу отдаёт снимок для записи в план
сделки. Вход он НЕ меняет, ничего не блокирует, любая ошибка → пустой снимок.

Определения (ЗАМОРОЖЕНЫ до просмотра результатов, не подбираются)
  d      = (EMA20 − цена)/ATR для шорта, (цена − EMA20)/ATR для лонга: «насколько
           цена ушла ОТ опоры в сторону сделки» (как в trend_trigger.extension_in_atr).
  touched = за последние WINDOW минут был хотя бы один замер с d <= 0
           (цена касалась EMA20 или заходила за неё против сделки).
  resumed = touched И сейчас d > 0 (цена вернулась на сторону сделки).
Первичная метрика — `resumed`. Остальные поля (d_now, d_min_win, minutes_since_touch)
пишутся для разведочного анализа; решение по ним принимать нельзя.

Ограничение: история в памяти процесса. После рестарта/деплоя она пустая, поэтому
снимок содержит `observed_min` и `history_complete`; в анализ берутся только записи
с полной историей. Свечи между сканами не хранятся — это замер по скану (~раз в минуту).
"""
from __future__ import annotations

import time
from collections import deque

from core.config import settings


def _value(ctx, key):
    if ctx is None:
        return None
    if isinstance(ctx, dict):
        return ctx.get(key)
    return getattr(ctx, key, None)


def _signed_ext(ctx, side: str):
    """(EMA20 − цена)/ATR для шорта, (цена − EMA20)/ATR для лонга. None — нельзя."""
    try:
        last = float(_value(ctx, "last_close"))
        ema20 = float(_value(ctx, "ema20"))
        atr = float(_value(ctx, "atr14"))
    except (TypeError, ValueError):
        return None
    if atr <= 0:
        return None
    delta = last - ema20 if str(side).lower() in ("long", "buy") else ema20 - last
    return delta / atr


class TrendResumeTracker:
    def __init__(self, *, now=time.time):
        self._now = now
        self._hist: dict[tuple[str, str], deque] = {}
        self._first: dict[str, float] = {}

    # ── настройки (читаются при каждом вызове, перезапуск не нужен) ──────────
    @staticmethod
    def _enabled() -> bool:
        return bool(getattr(settings, "TREND_RESUME_ENABLED", True))

    @staticmethod
    def _tf() -> str:
        return str(getattr(settings, "TREND_RESUME_TF", "1h"))

    @staticmethod
    def _window_sec() -> float:
        return float(getattr(settings, "TREND_RESUME_WINDOW_MIN", 360.0)) * 60.0

    def observe(self, symbol: str, contexts) -> None:
        """Запомнить положение цены для обеих сторон. Никогда не бросает."""
        try:
            if not self._enabled() or not isinstance(contexts, dict):
                return
            ctx = contexts.get(self._tf())
            if ctx is None:
                return
            t = float(self._now())
            self._first.setdefault(symbol, t)
            horizon = t - self._window_sec()
            for side in ("long", "short"):
                d = _signed_ext(ctx, side)
                if d is None:
                    continue
                q = self._hist.setdefault((symbol, side), deque())
                # не чаще раза в 30 с: скан раз в ~минуту, лишние точки не нужны
                if q and t - q[-1][0] < 30.0:
                    q[-1] = (q[-1][0], d)
                else:
                    q.append((t, d))
                while q and q[0][0] < horizon:
                    q.popleft()
        except Exception:  # noqa: BLE001 — наблюдатель не должен ронять цикл
            return

    def snapshot(self, symbol: str, side: str) -> dict:
        """Снимок для plan_json.trend_resume. Всегда dict, никогда не бросает."""
        try:
            if not self._enabled():
                return {"enabled": False}
            s = "long" if str(side).lower() in ("long", "buy") else "short"
            q = self._hist.get((symbol, s))
            if not q:
                return {"enabled": True, "side": s, "tf": self._tf(), "samples": 0,
                        "history_complete": False, "resumed": None}
            t = float(self._now())
            window = self._window_sec()
            vals = [d for _, d in q]
            d_now = vals[-1]
            touched_at = next((ts for ts, d in reversed(q) if d <= 0.0), None)
            touched = touched_at is not None
            observed = max(0.0, t - self._first.get(symbol, t))
            return {
                "enabled": True,
                "side": s,
                "tf": self._tf(),
                "window_min": round(window / 60.0, 1),
                "samples": len(vals),
                "d_now": round(d_now, 4),
                "d_min_win": round(min(vals), 4),
                "d_max_win": round(max(vals), 4),
                "touched": touched,
                "minutes_since_touch": (round((t - touched_at) / 60.0, 1)
                                        if touched_at is not None else None),
                "resumed": bool(touched and d_now > 0.0),
                "observed_min": round(observed / 60.0, 1),
                # полная история: символ наблюдается не меньше окна (с запасом 10%)
                "history_complete": observed >= window * 0.9,
            }
        except Exception:  # noqa: BLE001
            return {"enabled": True, "error": True}
