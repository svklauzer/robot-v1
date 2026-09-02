"""Предохранитель исходящей сети (#egress-guard-2026-07-26).

Второй раунд логов инцидента 26.07 показал худший симптом:

    18:05:39  Uvicorn running on http://0.0.0.0:10000
    18:06:21  ==> No open HTTP ports detected on 0.0.0.0
    18:10:59  ==> Port scan timeout reached

Порт слушается, но Render не может подключиться — accept() не выполняется,
потому что event loop заблокирован. И блокировал его НЕ HTTP: между попытками
проходило по 65 секунд при таймауте ccxt 15с. Столько уходит в `getaddrinfo`,
который синхронный и таймауту ccxt не подчиняется.

Инвариант: НИ ОДИН сетевой сбой не должен блокировать вызывающий поток дольше
короткого лимита, даже если резолвер висит минуту.
"""
import time

import pytest

from core.config import settings
from services import net_guard


@pytest.fixture(autouse=True)
def _clean_cache():
    net_guard.reset_cache()
    yield
    net_guard.reset_cache()


def test_hanging_resolver_does_not_block_the_caller(monkeypatch):
    """Ключевой тест: резолвер висит 30с, вызывающий поток обязан вернуться
    за доли секунды. Именно этого не хватало 26.07."""
    def _hang(host):
        time.sleep(30)
        return True

    monkeypatch.setattr(net_guard, "_blocking_resolve", _hang)

    started = time.time()
    ok = net_guard.resolve_ok("api.huobi.pro", timeout=0.3, ttl=0)
    elapsed = time.time() - started

    assert ok is False, "зависший резолв обязан считаться недоступностью"
    assert elapsed < 2.0, f"вызывающий поток заблокирован на {elapsed:.1f}с — сервис снова убьют"


def test_fast_resolve_passes(monkeypatch):
    monkeypatch.setattr(net_guard, "_blocking_resolve", lambda host: True)
    assert net_guard.resolve_ok("api.huobi.pro", timeout=1.0, ttl=0) is True


def test_result_is_cached_so_the_probe_is_not_paid_per_call(monkeypatch):
    calls = {"n": 0}

    def _count(host):
        calls["n"] += 1
        return True

    monkeypatch.setattr(net_guard, "_blocking_resolve", _count)
    for _ in range(5):
        net_guard.resolve_ok("api.huobi.pro", timeout=1.0, ttl=60)

    assert calls["n"] == 1, "проверка резолва не должна платиться на каждом вызове"


def test_guard_can_be_disabled(monkeypatch):
    monkeypatch.setattr(net_guard, "_blocking_resolve", lambda host: (_ for _ in ()).throw(OSError("no dns")))
    old = settings.EGRESS_GUARD_ENABLED
    try:
        settings.EGRESS_GUARD_ENABLED = False
        assert net_guard.resolve_ok("api.huobi.pro", ttl=0) is True
    finally:
        settings.EGRESS_GUARD_ENABLED = old


def test_htx_client_fails_fast_when_dns_is_down(monkeypatch):
    """HTXClient не должен уходить в ccxt, если хост не резолвится."""
    from services.htx_client import HTXClient

    HTXClient._cb_consecutive_failures = 0
    HTXClient._cb_open_until = 0.0
    monkeypatch.setattr(net_guard, "_blocking_resolve", lambda host: (_ for _ in ()).throw(OSError("no dns")))

    called = {"n": 0}

    def _should_not_run(*_a, **_k):
        called["n"] += 1
        return "unreachable"

    client = HTXClient.__new__(HTXClient)
    started = time.time()
    with pytest.raises(HTXClient.ExchangeUnavailable):
        client._retry(_should_not_run, retries=5, delay=5.0)

    assert called["n"] == 0, "при мёртвом DNS в сеть ходить нельзя"
    assert time.time() - started < 2.0

    HTXClient._cb_consecutive_failures = 0
    HTXClient._cb_open_until = 0.0


def test_okx_client_fails_fast_when_dns_is_down(monkeypatch):
    """(#okx-satellite-2026-09-02) Тот же контракт, что и у HTXClient выше —
    OKXClient переиспользует тот же net_guard.resolve_ok()."""
    from services.okx_client import OKXClient

    OKXClient._cb_consecutive_failures = 0
    OKXClient._cb_open_until = 0.0
    monkeypatch.setattr(net_guard, "_blocking_resolve", lambda host: (_ for _ in ()).throw(OSError("no dns")))

    called = {"n": 0}

    def _should_not_run(*_a, **_k):
        called["n"] += 1
        return "unreachable"

    client = OKXClient.__new__(OKXClient)
    started = time.time()
    with pytest.raises(OKXClient.ExchangeUnavailable):
        client._retry(_should_not_run, retries=5, delay=5.0)

    assert called["n"] == 0, "при мёртвом DNS в сеть ходить нельзя"
    assert time.time() - started < 2.0

    OKXClient._cb_consecutive_failures = 0
    OKXClient._cb_open_until = 0.0


def test_scan_path_is_off_the_event_loop():
    """Регресс на первопричину: тяжёлый скан обязан уходить в поток.

    Пока `analyze_symbol` и пред-проход бюджета вызывались синхронно внутри
    `async def step`, любой сетевой сбой замораживал весь веб-процесс.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "workers" / "robot_loop.py"
    text = src.read_text(encoding="utf-8")

    assert "asyncio.to_thread(self.intelligence.analyze_symbol" in text, (
        "analyze_symbol вернулся в event loop — сетевой сбой снова уронит сервис"
    )
    assert "asyncio.to_thread(\n                    self._compute_dynamic_budget" in text or \
           "to_thread(self._compute_dynamic_budget" in text, (
        "пред-проход бюджета вернулся в event loop"
    )
