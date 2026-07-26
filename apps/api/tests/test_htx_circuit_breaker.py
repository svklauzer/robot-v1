"""Размыкатель биржи (#htx-outage-2026-07-26).

Инцидент 26.07: HTX перестал отвечать (RequestTimeout на ПУБЛИЧНОМ
/v1/common/timestamp — API-ключ ни при чём). Каждый вызов уходил в
timeout 45s × 5 попыток + 20s блокирующего sleep = 245s; скан 8 символов
морозил event loop на десятки минут, /health переставал отвечать и Render
убивал инстанс. В логах видно ровно это: последний health 17:23:49,
рестарт 17:24:55 — 66 секунд тишины.

Инвариант, который защищаем: НЕДОСТУПНОСТЬ БИРЖИ НЕ ДОЛЖНА РОНЯТЬ СЕРВИС.
"""
import time

import pytest

from core.config import settings
from services.htx_client import HTXClient


class _Boom(Exception):
    """ccxt.RequestTimeout выглядит именно так: str(e) == 'htx GET <url>'."""


def _dead(*_a, **_k):
    raise _Boom("htx GET https://api.huobi.pro/v1/common/timestamp")


def _alive(*_a, **_k):
    return {"ok": True}


@pytest.fixture(autouse=True)
def _reset_circuit(monkeypatch):
    # Изолируем размыкатель от egress-гварда: здесь проверяется реакция на
    # ошибки БИРЖИ, а не на отсутствие DNS (у DNS свой набор тестов).
    from services import net_guard

    net_guard.reset_cache()
    monkeypatch.setattr(net_guard, "_blocking_resolve", lambda host: True)

    HTXClient._cb_consecutive_failures = 0
    HTXClient._cb_open_until = 0.0
    HTXClient._cb_host_index = 0
    yield
    HTXClient._cb_consecutive_failures = 0
    HTXClient._cb_open_until = 0.0
    HTXClient._cb_host_index = 0
    net_guard.reset_cache()


def _client():
    return HTXClient.__new__(HTXClient)   # без сети и ccxt-инициализации


def test_circuit_opens_after_threshold_and_then_fails_instantly():
    c = _client()
    threshold = int(settings.HTX_CIRCUIT_FAILURE_THRESHOLD)

    for _ in range(threshold):
        with pytest.raises(_Boom):
            c._retry(_dead, retries=3, delay=0.001)

    assert HTXClient.circuit_state()["open"] is True

    # Ключевое: следующий вызов не идёт в сеть и не блокирует loop.
    started = time.time()
    with pytest.raises(HTXClient.ExchangeUnavailable):
        c._retry(_dead, retries=5, delay=10.0)
    assert time.time() - started < 0.05, "разомкнутая цепь обязана падать мгновенно"


def test_adaptive_retries_shorten_the_freeze_window():
    """После первой неудачи повторять по 3–5 раз бессмысленно — это лишь
    удлиняет заморозку. Считаем фактические попытки."""
    c = _client()
    calls = {"n": 0}

    def counting(*_a, **_k):
        calls["n"] += 1
        raise _Boom("htx GET ...")

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
    assert HTXClient._cb_consecutive_failures == 1

    assert c._retry(_alive, retries=1) == {"ok": True}
    assert HTXClient._cb_consecutive_failures == 0
    assert HTXClient.circuit_state()["open"] is False


def test_host_rotates_when_circuit_opens():
    """У HTX несколько эндпоинтов (идёт миграция huobi.pro → htx.com). Если
    основной не маршрутизируется из ДЦ, следующая проба уходит на запасной."""
    c = _client()
    hosts = HTXClient._cb_hosts()
    assert len(hosts) > 1, "список запасных хостов пуст — ротация невозможна"

    before = HTXClient.circuit_state()["active_host"]
    for _ in range(int(settings.HTX_CIRCUIT_FAILURE_THRESHOLD)):
        with pytest.raises(_Boom):
            c._retry(_dead, retries=1, delay=0.001)

    assert HTXClient.circuit_state()["active_host"] != before


def test_circuit_reopens_for_a_probe_after_timeout():
    c = _client()
    for _ in range(int(settings.HTX_CIRCUIT_FAILURE_THRESHOLD)):
        with pytest.raises(_Boom):
            c._retry(_dead, retries=1, delay=0.001)
    assert HTXClient.circuit_state()["open"] is True

    HTXClient._cb_open_until = time.time() - 1   # окно истекло
    assert HTXClient.circuit_state()["open"] is False
    assert c._retry(_alive, retries=1) == {"ok": True}


def test_timeout_budget_cannot_freeze_the_event_loop_for_minutes():
    """Регресс-тест на первопричину инцидента: суммарный бюджет заморозки до
    размыкания цепи должен быть в пределах десятков секунд, а не минут."""
    timeout_s = int(settings.HTX_HTTP_TIMEOUT_MS) / 1000
    threshold = int(settings.HTX_CIRCUIT_FAILURE_THRESHOLD)

    first_call = timeout_s * 3 + (2 + 4)          # полный набор попыток
    later_calls = timeout_s * (threshold - 1)     # по одной попытке
    budget = first_call + later_calls

    assert timeout_s <= 20, "таймаут вернулся к значению, которое морозит loop"
    assert budget < 90, f"бюджет заморозки {budget:.0f}с — инстанс снова убьют"
