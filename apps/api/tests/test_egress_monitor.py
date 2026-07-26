"""Монитор исходящей сети (#egress-monitor-2026-07-26).

Статус-страница Render: «All Systems Operational», «No downtime recorded» за
26.07. Инстанс в те же часы не видел ни HTX, ни Kraken. Противоречия нет —
глобальная страница отражает платформенные сервисы, а не egress конкретного
инстанса. Доказательства собираем сами, с контрольной группой хостов, чтобы
отличить «лежит сеть инстанса» от «недоступны конкретно биржи».
"""
import json
import time

import pytest

from core.config import settings
from services import egress_monitor as em


@pytest.fixture
def journal(tmp_path, monkeypatch):
    path = tmp_path / "egress.jsonl"
    monkeypatch.setattr(settings, "EGRESS_MONITOR_PATH", str(path))
    return path


def _fake_probe(results: dict[str, bool]):
    def _probe(host, timeout):
        ok = results.get(host, True)
        return {"host": host, "dns_ms": 5.0, "tcp_ms": 10.0 if ok else 0.0,
                "ok": ok, "stage": "ok" if ok else "tcp",
                **({} if ok else {"error_type": "TimeoutError"})}
    return _probe


def test_control_group_separates_egress_failure_from_exchange_failure(monkeypatch):
    """Ключевая методика: без контрольной группы нельзя отличить одно от другого."""
    # Лежит всё — значит сеть инстанса.
    monkeypatch.setattr(em, "_probe", _fake_probe({h: False for h, _ in em.TARGETS}))
    assert em.probe_once()["verdict"] == "egress_down"

    # Лежат только биржи, контрольные живы — адресная проблема.
    only_exchanges_down = {h: (g != "exchange") for h, g in em.TARGETS}
    monkeypatch.setattr(em, "_probe", _fake_probe(only_exchanges_down))
    assert em.probe_once()["verdict"] == "exchanges_unreachable"

    # Всё живо.
    monkeypatch.setattr(em, "_probe", _fake_probe({}))
    assert em.probe_once()["verdict"] == "ok"


def test_control_group_is_diverse():
    """Контрольная группа из одной компании ничего не доказывает."""
    control = [h for h, g in em.TARGETS if g == "control"]
    assert len(control) >= 3, "контрольная группа слишком мала"
    # разные домены второго уровня = разные операторы
    roots = {".".join(h.split(".")[-2:]) for h in control}
    assert len(roots) >= 3, f"контрольные хосты недостаточно разнородны: {control}"


def test_outage_windows_are_reported_with_timestamps(journal, monkeypatch):
    """Поддержке нужны окна с метками времени — их и отдаём."""
    now = time.time()
    rows = [
        {"ts": now - 600, "v": "ok", "ex": "3/3", "ctl": "3/3", "dns_ms_max": 5},
        {"ts": now - 540, "v": "egress_down", "ex": "0/3", "ctl": "0/3",
         "bad": [{"h": "api.huobi.pro", "s": "tcp", "e": "TimeoutError"}]},
        {"ts": now - 480, "v": "egress_down", "ex": "0/3", "ctl": "0/3"},
        {"ts": now - 420, "v": "ok", "ex": "3/3", "ctl": "3/3", "dns_ms_max": 7},
    ]
    journal.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    hist = em.history(hours=1)

    assert hist["status"] == "ok"
    assert hist["samples"] == 4
    assert hist["availability_pct"] == 50.0
    assert len(hist["outage_windows"]) == 1
    window = hist["outage_windows"][0]
    assert window["verdict"] == "egress_down"
    assert window["minutes"] == pytest.approx(1.0, abs=0.1)
    assert "from" in window and "to" in window


def test_ongoing_outage_is_flagged(journal):
    now = time.time()
    rows = [
        {"ts": now - 300, "v": "ok", "ex": "3/3", "ctl": "3/3"},
        {"ts": now - 240, "v": "egress_down", "ex": "0/3", "ctl": "0/3"},
        {"ts": now - 180, "v": "egress_down", "ex": "0/3", "ctl": "0/3"},
    ]
    journal.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    hist = em.history(hours=1)
    assert hist["outage_windows"][-1].get("ongoing") is True


def test_broken_lines_do_not_kill_history(journal):
    now = time.time()
    journal.write_text(
        json.dumps({"ts": now - 60, "v": "ok", "ex": "3/3", "ctl": "3/3"}) + "\n"
        + "{битая строка\n"
        + json.dumps({"ts": now - 30, "v": "ok", "ex": "3/3", "ctl": "3/3"}) + "\n",
        encoding="utf-8",
    )

    hist = em.history(hours=1)
    assert hist["samples"] == 2


def test_no_data_is_reported_honestly(journal):
    assert em.history(hours=1)["status"] == "no_data"


def test_snapshot_writes_details_only_when_degraded(journal, monkeypatch):
    monkeypatch.setattr(em, "_probe", _fake_probe({h: False for h, _ in em.TARGETS}))
    em.log_snapshot(em.probe_once())

    monkeypatch.setattr(em, "_probe", _fake_probe({}))
    em.log_snapshot(em.probe_once())

    rows = [json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert "bad" in rows[0], "при сбое нужны детали для тикета"
    assert "bad" not in rows[1], "в норме журнал должен оставаться компактным"
