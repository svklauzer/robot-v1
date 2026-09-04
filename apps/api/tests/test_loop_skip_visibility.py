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
