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

    def __init__(self, *, resumed_decision: str = DECISION_RESUMED,
                 min_duration_sec: float = 0.0) -> None:
        self._reason: str | None = None
        self._last_at: float = 0.0
        # (#scan-flap-2026-09-05) Когда состояние держится один тик, пара
        # «замолчал → возобновил» не сообщает ничего и пишется каждые два тика.
        # Так и вышло у оси сканирования: `_approved` растёт, едва символ прошёл
        # ОТБОР СЕТАПА, а вход дальше блокируется своим гейтом и пишет своё
        # событие. За два часа лента набрала 19 системных записей, ни одна из
        # которых не отвечала на вопрос «стоим мы или нет».
        #
        # Порог задаёт, сколько состояние обязано продержаться, прежде чем его
        # запишут. Ноль — прежнее поведение: у оси «цикл не сделал шаг» задержка
        # вредна, там важна каждая секунда простоя.
        self._min_duration = float(min_duration_sec)
        self._since: float = 0.0
        self._announced: bool = False
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
            announced = self._announced
            held_sec = (now - self._since) if self._since else 0.0
            self._reason = None
            self._last_at = 0.0
            self._since = 0.0
            self._announced = False
            # Возобновление пишется только там, где было о чём объявлять:
            # без этого маркер закрывал молчание, которого читатель не видел.
            if prev and announced:
                return self._write(
                    db, decisions, self._resumed_decision, "ok",
                    {"after_skip": prev, "held_sec": round(held_sec, 1),
                     "note": "цикл возобновил работу"},
                )
            return False

        changed = reason != prev
        if changed:
            self._since = now
            self._announced = False

        # Состояние обязано продержаться: одиночный тик — не простой.
        if (now - self._since) < self._min_duration:
            self._reason = reason
            return False

        stale = self._announced and (now - self._last_at) >= self._heartbeat_sec()
        if self._announced and not stale:
            return False

        # «Повтор» — это «ту же причину уже объявляли», а не «пора по пульсу».
        # На первом событии `_last_at` равен нулю, и любая проверка на давность
        # считает его просроченным: запись получала repeat=True с порога.
        is_repeat = self._announced

        self._reason = reason
        self._last_at = now
        self._announced = True

        body = dict(payload or {})
        body["repeat"] = is_repeat
        body["held_sec"] = round(now - self._since, 1)
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
