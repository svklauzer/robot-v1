"""Причина простоя цикла обязана быть в ленте решений
(#loop-skip-visibility-2026-09-04).

Простой 04.09.2026: 5 часов 50 минут без сделок и без единой записи в ленте.
Робот «running», задача цикла жива, egress 100%, обе биржи доступны, рыночные
данные свежие — на всех экранах работающая система с пустой лентой. Держал её
exchange_switch_guard, и узнать это можно было только из логов Render.

Три пути пропуска в background_robot_loop писали ТОЛЬКО в лог приложения. Тест
закрывает и это, и обратную опасность: писать на каждом тике нельзя — за тот же
простой лента получила бы ~320 одинаковых записей в таблице, где их 58 тысяч.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.config import settings
from services.loop_skip_reporter import LoopSkipReporter, SYSTEM_SYMBOL

API = Path(__file__).resolve().parents[1]


class _Recorder:
    """Двойник DecisionEventService: копит записи вместо БД."""

    def __init__(self, explode: bool = False):
        self.rows: list[dict] = []
        self._explode = explode

    def record(self, db, **kwargs):
        if self._explode:
            raise RuntimeError("БД недоступна")
        self.rows.append(kwargs)
        return object()


@pytest.fixture(autouse=True)
def _heartbeat(monkeypatch):
    monkeypatch.setattr(settings, "LOOP_SKIP_HEARTBEAT_SEC", 900.0, raising=False)


# ── что пишем ───────────────────────────────────────────────────────────────

def test_first_skip_is_recorded():
    rep, rec = LoopSkipReporter(), _Recorder()

    assert rep.report(None, rec, reason="loop_skip_exchange_switch",
                      payload={"open_positions": 1}) is True

    row = rec.rows[0]
    assert row["decision"] == "loop_skip_exchange_switch"
    assert row["status"] == "blocked"
    assert row["symbol"] == SYSTEM_SYMBOL
    assert row["payload"]["open_positions"] == 1
    assert row["payload"]["repeat"] is False


def test_same_reason_does_not_flood_the_feed():
    """Главная опасность правки. Цикл тикает раз в ~66 с: за простой 04.09
    (5 ч 50 мин) запись каждый тик дала бы ~320 одинаковых событий."""
    rep, rec = LoopSkipReporter(), _Recorder()

    for _ in range(320):
        rep.report(None, rec, reason="loop_skip_exchange_switch")

    assert len(rec.rows) == 1


def test_heartbeat_marks_that_the_stall_continues(monkeypatch):
    """Без пульса «стоим пять часов» неотличимо от «стояли минуту в 23:06» —
    длительность простоя по ленте было бы не восстановить."""
    rep, rec = LoopSkipReporter(), _Recorder()
    rep.report(None, rec, reason="loop_skip_exchange_switch")

    rep._last_at -= 901.0  # прошёл пульс
    assert rep.report(None, rec, reason="loop_skip_exchange_switch") is True

    assert len(rec.rows) == 2
    assert rec.rows[1]["payload"]["repeat"] is True


def test_changed_reason_is_recorded_immediately():
    rep, rec = LoopSkipReporter(), _Recorder()
    rep.report(None, rec, reason="loop_skip_exchange_switch")
    rep.report(None, rec, reason="loop_skip_live_safety")

    assert [r["decision"] for r in rec.rows] == [
        "loop_skip_exchange_switch", "loop_skip_live_safety",
    ]
    assert rec.rows[1]["payload"]["repeat"] is False


def test_resume_is_recorded_once():
    """Простой заканчивается молча. Без отметки нельзя ответить, КОГДА поехало,
    не сверяя косвенные признаки."""
    rep, rec = LoopSkipReporter(), _Recorder()
    rep.report(None, rec, reason="loop_skip_exchange_switch")

    assert rep.report(None, rec, reason=None) is True
    assert rec.rows[-1]["decision"] == "loop_resumed"
    assert rec.rows[-1]["payload"]["after_skip"] == "loop_skip_exchange_switch"

    # Работающий цикл молчит: каждый тик «всё хорошо» ленту не засоряет.
    for _ in range(100):
        assert rep.report(None, rec, reason=None) is False
    assert len(rec.rows) == 2


def test_healthy_loop_writes_nothing():
    rep, rec = LoopSkipReporter(), _Recorder()
    for _ in range(50):
        rep.report(None, rec, reason=None)
    assert rec.rows == []


def test_reporting_failure_never_breaks_the_loop():
    """Видимость причины простоя не имеет права сама стать причиной простоя."""
    rep, rec = LoopSkipReporter(), _Recorder(explode=True)

    assert rep.report(None, rec, reason="loop_skip_live_safety") is False


# ── связь с циклом ──────────────────────────────────────────────────────────

def test_all_three_skip_paths_report_to_the_feed():
    """Сторож на возврат немого пропуска. Раньше ветка validation_gates была
    буквально `pass` — ни лога, ни события."""
    src = (API / "main.py").read_text(encoding="utf-8")
    body = src.split("async def background_robot_loop", 1)[1][:6000]

    for reason in ("loop_skip_validation_gates", "loop_skip_exchange_switch",
                   "loop_skip_live_safety"):
        assert reason in body, f"путь пропуска {reason} снова молчит в ленте"

    assert "skip_reporter.report(db, loop.decisions, reason=None)" in body, (
        "возобновление работы не фиксируется — момент выхода из простоя "
        "снова придётся вычислять косвенно"
    )


def test_every_loop_skip_code_has_a_ui_label():
    """Иначе в ленте будет машинный код — ровно то, что чинили утром."""
    web = API.parent / "web" / "app" / "intelligence" / "page.tsx"
    if not web.exists():
        pytest.skip("нет фронтенда")

    src = (API / "services" / "loop_skip_reporter.py").read_text(encoding="utf-8")
    main_src = (API / "main.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'reason="(loop_[a-z_]+)"', main_src))
    codes |= set(re.findall(r'DECISION_RESUMED = "([a-z_]+)"', src))

    ui = web.read_text(encoding="utf-8")
    missing = sorted(c for c in codes if f"{c}:" not in ui)
    assert not missing, f"код простоя без ярлыка в ленте: {missing}"


# ── вторая ось молчания (#scan-visibility-2026-09-05) ───────────────────────

def test_scan_silence_is_a_separate_axis_from_loop_silence():
    """04.09 лента молчала семь часов, и это было неотличимо от остановки
    цикла: задача была жива, шаги делались, но ни один символ не доходил до
    одобрения — а все выходы до одобрения идут через `continue`, без записи.

    Полчаса ушло на проверку живости задачи вместо чтения с экрана. Две оси
    молчания обязаны иметь разные коды возобновления, иначе «поехало» не
    отвечает на вопрос, ЧТО именно поехало.
    """
    from services.loop_skip_reporter import (
        DECISION_RESUMED, DECISION_SCAN_NO_CANDIDATE, DECISION_SCAN_RESUMED,
    )

    assert DECISION_SCAN_RESUMED != DECISION_RESUMED
    assert DECISION_SCAN_NO_CANDIDATE != DECISION_RESUMED


def test_each_reporter_writes_its_own_resume_code():
    from services.loop_skip_reporter import LoopSkipReporter

    db, decisions = object(), _Recorder()
    reporter = LoopSkipReporter(resumed_decision="scan_candidates_resumed")

    reporter.report(db, decisions, reason="scan_no_candidate")
    reporter.report(db, decisions, reason=None)

    assert [r["decision"] for r in decisions.rows] == [
        "scan_no_candidate", "scan_candidates_resumed",
    ]


def test_scan_summary_is_written_once_per_step_not_per_symbol():
    """Цикл тикает раз в ~66 с при семи символах. Запись на символ дала бы ~450
    строк в час в таблицу, где уже 58 тысяч событий, — лента перестала бы
    читаться ровно тогда, когда она нужна."""
    source = (API / "workers" / "robot_loop.py").read_text(encoding="utf-8")

    assert source.count("self.scan_reporter.report(") == 1

    call_at = source.index("self.scan_reporter.report(")
    loop_at = source.index('for symbol in bot.config_json.get("symbols", [])')
    assert call_at > loop_at, "итог шага записывается внутри цикла по символам"


def test_summary_names_the_reason_for_every_symbol():
    """Одна строка «одобрено 0» без разбивки не отличает «рынок не даёт
    сетапов» от «сломался анализ»: и то и другое выглядит как тишина."""
    source = (API / "workers" / "robot_loop.py").read_text(encoding="utf-8")

    for marker in ('_scan_skips[symbol] = "no_analysis"',
                   '_scan_skips[symbol] = f"hold:',
                   '"by_symbol": _scan_skips'):
        assert marker in source, f"пропала разбивка по символам: {marker}"


import contextlib


@contextlib.contextmanager
def _frozen(moment: float):
    """Фиксированное «сейчас» для репортёра: он берёт время через time.time()."""
    import time as _time

    original = _time.time
    _time.time = lambda: moment
    try:
        yield
    finally:
        _time.time = original



# ── дребезг (#scan-flap-2026-09-05) ─────────────────────────────────────────

def test_a_single_tick_of_silence_is_not_reported():
    """05.09 лента набрала 19 системных записей за два часа: пара «замолчал →
    возобновил» каждые два тика. `_approved` растёт, едва символ прошёл ОТБОР
    СЕТАПА, а вход дальше блокируется своим гейтом и пишет своё событие — так
    состояние и мигало каждый проход.

    Один проход без одобренных — не простой. Запись обязана дождаться, пока
    состояние продержится.
    """
    from services.loop_skip_reporter import LoopSkipReporter

    db, rec = object(), _Recorder()
    reporter = LoopSkipReporter(min_duration_sec=300.0)

    import time as _t
    base = _t.time()

    with _frozen(base):
        assert reporter.report(db, rec, reason="scan_no_candidate") is False
    with _frozen(base + 60):
        assert reporter.report(db, rec, reason=None) is False

    assert rec.rows == [], "мигание записано в ленту"


def test_sustained_silence_is_reported_once_and_then_closed():
    from services.loop_skip_reporter import LoopSkipReporter

    db, rec = object(), _Recorder()
    reporter = LoopSkipReporter(resumed_decision="scan_candidates_resumed",
                                min_duration_sec=300.0)

    import time as _t
    base = _t.time()

    with _frozen(base):
        reporter.report(db, rec, reason="scan_no_candidate")
    with _frozen(base + 400):
        assert reporter.report(db, rec, reason="scan_no_candidate") is True
    with _frozen(base + 500):
        assert reporter.report(db, rec, reason=None) is True

    assert [r["decision"] for r in rec.rows] == [
        "scan_no_candidate", "scan_candidates_resumed",
    ]
    assert rec.rows[0]["payload"]["repeat"] is False
    assert rec.rows[1]["payload"]["held_sec"] >= 400


def test_resume_is_not_written_for_a_silence_nobody_saw():
    """Маркер возобновления закрывает объявленное молчание. Если о молчании не
    сообщали, закрывать нечего — иначе в ленте появляется «поехало» без «стоим».
    """
    from services.loop_skip_reporter import LoopSkipReporter

    db, rec = object(), _Recorder()
    reporter = LoopSkipReporter(min_duration_sec=300.0)

    import time as _t
    base = _t.time()

    with _frozen(base):
        reporter.report(db, rec, reason="scan_no_candidate")
    with _frozen(base + 10):
        reporter.report(db, rec, reason=None)

    assert not any(r["status"] == "ok" for r in rec.rows)


def test_immediate_reporting_stays_the_default():
    """У оси «цикл не сделал шаг» задержка вредна: там важна каждая секунда
    простоя, и первое же событие обязано попасть в ленту."""
    from services.loop_skip_reporter import LoopSkipReporter

    db, rec = object(), _Recorder()
    reporter = LoopSkipReporter()

    assert reporter.report(db, rec, reason="loop_skip_live_safety") is True
    assert rec.rows[0]["payload"]["repeat"] is False
