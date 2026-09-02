"""Размыкатель OKX (#okx-satellite-2026-09-02).

Зеркало test_htx_circuit_breaker.py: OKXClient повторяет тот же контракт
размыкателя, что и HTXClient (см. его докстринг про инцидент 26.07 —
недоступность биржи не должна ронять сервис, независимо от того, какая
биржа сейчас активна через ACTIVE_EXCHANGE).

Отличие от HTX-версии: OKX_API_HOSTNAME_FALLBACKS пуст по умолчанию (у OKX
нет задокументированной истории многохостовой миграции, как у HTX) — тест
ротации хостов явно задаёт список из нескольких хостов, чтобы проверить саму
логику ротации, а не полагаться на боевой дефолт.
"""
import time

import pytest

from core.config import settings
from services.okx_client import OKXClient


class _Boom(Exception):
    """ccxt.RequestTimeout выглядит примерно так же и для OKX."""


def _dead(*_a, **_k):
    raise _Boom("okx GET https://www.okx.com/api/v5/public/time")


def _alive(*_a, **_k):
    return {"ok": True}


@pytest.fixture(autouse=True)
def _reset_circuit(monkeypatch):
    from services import net_guard

    net_guard.reset_cache()
    monkeypatch.setattr(net_guard, "_blocking_resolve", lambda host: True)

    OKXClient._cb_consecutive_failures = 0
    OKXClient._cb_open_until = 0.0
    OKXClient._cb_host_index = 0
    yield
    OKXClient._cb_consecutive_failures = 0
    OKXClient._cb_open_until = 0.0
    OKXClient._cb_host_index = 0
    net_guard.reset_cache()


def _client():
    return OKXClient.__new__(OKXClient)   # без сети и ccxt-инициализации


def test_circuit_opens_after_threshold_and_then_fails_instantly():
    c = _client()
    threshold = int(settings.OKX_CIRCUIT_FAILURE_THRESHOLD)

    for _ in range(threshold):
        with pytest.raises(_Boom):
            c._retry(_dead, retries=3, delay=0.001)

    assert OKXClient.circuit_state()["open"] is True

    started = time.time()
    with pytest.raises(OKXClient.ExchangeUnavailable):
        c._retry(_dead, retries=5, delay=10.0)
    assert time.time() - started < 0.05, "разомкнутая цепь обязана падать мгновенно"


def test_adaptive_retries_shorten_the_freeze_window():
    c = _client()
    calls = {"n": 0}

    def counting(*_a, **_k):
        calls["n"] += 1
        raise _Boom("okx GET ...")

    with pytest.raises(_Boom):
        c._retry(counting, retries=5, delay=0.001)
    first_call_attempts = calls["n"]

    calls["n"] = 0
    with pytest.raises(_Boom):
        c._retry(counting, retries=5, delay=0.001)
    second_call_attempts = calls["n"]

    assert first_call_attempts == 5, "первый сбой может быть разовым — полный набор"
    assert second_call_attempts == 1, "повторный сбой — одна попытка, не пять"


def test_success_closes_the_circuit():
    c = _client()
    with pytest.raises(_Boom):
        c._retry(_dead, retries=1, delay=0.001)
    assert OKXClient._cb_consecutive_failures == 1

    assert c._retry(_alive, retries=1) == {"ok": True}
    assert OKXClient._cb_consecutive_failures == 0
    assert OKXClient.circuit_state()["open"] is False


def test_host_rotates_when_multiple_hosts_configured(monkeypatch):
    """OKX_API_HOSTNAME_FALLBACKS пуст по умолчанию — задаём явно, чтобы
    проверить саму логику ротации (общий код с HTXClient), не боевой дефолт."""
    monkeypatch.setattr(settings, "OKX_API_HOSTNAME", "www.okx.com", raising=False)
    monkeypatch.setattr(
        settings, "OKX_API_HOSTNAME_FALLBACKS", "www.okx.com,aws.okx.com", raising=False
    )
    c = _client()
    hosts = OKXClient._cb_hosts()
    assert len(hosts) > 1

    before = OKXClient.circuit_state()["active_host"]
    for _ in range(int(settings.OKX_CIRCUIT_FAILURE_THRESHOLD)):
        with pytest.raises(_Boom):
            c._retry(_dead, retries=1, delay=0.001)

    assert OKXClient.circuit_state()["active_host"] != before


def test_single_host_default_degrades_gracefully():
    """Боевой дефолт: OKX_API_HOSTNAME_FALLBACKS пуст. Размыкатель обязан
    работать (открываться/держать состояние) даже без списка хостов для
    ротации — просто не меняет active_host."""
    assert OKXClient._cb_hosts() == []   # дефолт: ни primary, ни fallbacks не заданы
    c = _client()
    threshold = int(settings.OKX_CIRCUIT_FAILURE_THRESHOLD)
    for _ in range(threshold):
        with pytest.raises(_Boom):
            c._retry(_dead, retries=1, delay=0.001)
    assert OKXClient.circuit_state()["open"] is True
    assert OKXClient.circuit_state()["active_host"] is None


def test_circuit_reopens_for_a_probe_after_timeout():
    c = _client()
    for _ in range(int(settings.OKX_CIRCUIT_FAILURE_THRESHOLD)):
        with pytest.raises(_Boom):
            c._retry(_dead, retries=1, delay=0.001)
    assert OKXClient.circuit_state()["open"] is True

    OKXClient._cb_open_until = time.time() - 1   # окно истекло
    assert OKXClient.circuit_state()["open"] is False
    assert c._retry(_alive, retries=1) == {"ok": True}


def test_timeout_budget_cannot_freeze_the_event_loop_for_minutes():
    timeout_s = int(settings.OKX_HTTP_TIMEOUT_MS) / 1000
    threshold = int(settings.OKX_CIRCUIT_FAILURE_THRESHOLD)

    first_call = timeout_s * 3 + (2 + 4)
    later_calls = timeout_s * (threshold - 1)
    budget = first_call + later_calls

    assert timeout_s <= 20, "таймаут вернулся к значению, которое морозит loop"
    assert budget < 90, f"бюджет заморозки {budget:.0f}с — инстанс снова убьют"
