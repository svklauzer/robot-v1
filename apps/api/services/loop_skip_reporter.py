"""Почему торговый цикл не торгует — в ленту решений, а не только в лог
(#loop-skip-visibility-2026-09-04).

Простой 04.09.2026: 5 часов 50 минут без единой сделки и без единой записи в
ленте. Робот «running», цикл жив (`task_done: false`), сеть 100%, обе биржи
доступны, рыночные данные свежие. На всех экранах система выглядела работающей
и молчащей одновременно.

Держал её `exchange_switch_guard`. В `background_robot_loop` три пути пропуска
шага, и все три писали ТОЛЬКО в лог приложения:

    validation_gates.live_blockers -> robot_loop_validation_skip
    exchange_switch небезопасен    -> robot_loop_exchange_switch_skip
    safety.blocked                 -> robot_loop_safety_skip

Причину можно было узнать лишь из логов Render, куда никто не смотрит
ежеминутно. Диагностика заняла час на том, что должно читаться с экрана.

Почему не писать на каждом тике
-------------------------------
Цикл тикает раз в ~66 с. За тот простой это дало бы ~320 одинаковых записей, а
в таблице уже 58 тысяч событий. Лента перестала бы быть читаемой ровно тогда,
когда она нужнее всего.

Поэтому пишем на СМЕНУ состояния плюс редкий пульс: первый пропуск, смена
причины, возобновление — и по одной записи раз в LOOP_SKIP_HEARTBEAT_SEC, пока
причина держится. Пульс нужен, чтобы «стоим уже пять часов» отличалось от
«стояли минуту в 23:06»: без него длительность простоя не восстановить.

Возобновление пишется отдельным событием намеренно. Простой заканчивается
молча, и без такой отметки нельзя ответить на вопрос «когда именно поехало»,
не сверяя косвенные признаки.
"""
from __future__ import annotations

import time

from core.config import settings

# Событие про весь цикл, а не про инструмент: символ псевдо, чтобы запись не
# притворялась решением по конкретной монете.
SYSTEM_SYMBOL = "SYSTEM"

DECISION_RESUMED = "loop_resumed"

# Вторая ось молчания: шаг сделан, но ни один символ не дошёл до одобрения.
# 04.09 лента молчала семь часов подряд именно так, и это было неотличимо от
# остановки цикла — диагностика ушла на проверку живости задачи, хотя система
# работала и просто не находила сетапов.
DECISION_SCAN_NO_CANDIDATE = "scan_no_candidate"
DECISION_SCAN_RESUMED = "scan_candidates_resumed"


class LoopSkipReporter:
    """Состояние между тиками: что писали в прошлый раз и когда."""

    def __init__(self, *, resumed_decision: str = DECISION_RESUMED) -> None:
        self._reason: str | None = None
        self._last_at: float = 0.0
        # (#scan-visibility-2026-09-05) Своя отметка возобновления на каждую ось
        # молчания. Их две: цикл не сделал шаг вовсе и цикл прошёл шаг, но ни
        # один символ не дошёл до одобрения. С общим кодом «поехало» нельзя
        # понять, что именно поехало.
        self._resumed_decision = resumed_decision

    def _heartbeat_sec(self) -> float:
        return float(getattr(settings, "LOOP_SKIP_HEARTBEAT_SEC", 900.0))

    def report(self, db, decisions, *, reason: str | None, payload: dict | None = None) -> bool:
        """reason=None — цикл работает. Возвращает True, если событие записано.

        Исключение при записи не имеет права уронить торговый цикл: видимость
        причины простоя не стоит того, чтобы стать причиной простоя.
        """
        now = time.time()
        prev = self._reason

        if not reason:
            self._reason = None
            self._last_at = 0.0
            if prev:
                return self._write(
                    db, decisions, self._resumed_decision, "ok",
                    {"after_skip": prev, "note": "цикл возобновил работу"},
                )
            return False

        changed = reason != prev
        stale = (now - self._last_at) >= self._heartbeat_sec()
        if not (changed or stale):
            return False

        self._reason = reason
        self._last_at = now

        body = dict(payload or {})
        body["repeat"] = not changed
        return self._write(db, decisions, reason, "blocked", body)

    def _write(self, db, decisions, decision: str, status: str, payload: dict) -> bool:
        try:
            decisions.record(
                db,
                symbol=SYSTEM_SYMBOL,
                status=status,
                decision=decision,
                action=None,
                payload=payload,
            )
            return True
        except Exception:  # noqa: BLE001 — см. докстринг report()
            return False
