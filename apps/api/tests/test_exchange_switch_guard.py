"""(#okx-satellite-2026-09-02) exchange_switch_guard: не дать переключению
ACTIVE_EXCHANGE молча осиротить открытые позиции/ордера на бирже, с которой
ушли.

Отказ неактивной биржи (сеть/DNS) — fail-open (она и так не торгует). Отказ
= неактивная биржа ОТВЕТИЛА и показала что-то открытое — fail-closed для
НОВОЙ торговли на активной, до ручного разбора.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import exchange_switch_guard as guard


class _FakeClient:
    def __init__(self, *, orders=None, positions=None, raises=None):
        self._orders = orders or []
        self._positions = positions if positions is not None else []
        self._raises = raises

    def fetch_open_orders(self, symbol=None):
        if self._raises:
            raise self._raises
        return self._orders

    def fetch_positions(self):
        return self._positions


@pytest.fixture(autouse=True)
def _reset_cache():
    guard._cache = None
    guard._cache_at = 0.0
    yield
    guard._cache = None
    guard._cache_at = 0.0


def _patch_inactive(monkeypatch, name: str, client):
    monkeypatch.setattr(guard, "_inactive_client", lambda: (name, client))


def test_safe_when_inactive_exchange_has_nothing_open(monkeypatch):
    _patch_inactive(monkeypatch, "okx", _FakeClient())
    result = guard.check(force=True)
    assert result["safe"] is True
    assert result["reachable"] is True
    assert result["open_orders"] == 0
    assert result["open_positions"] == 0


def test_unsafe_when_inactive_exchange_has_open_orders(monkeypatch):
    _patch_inactive(monkeypatch, "okx", _FakeClient(orders=[{"id": "1"}]))
    result = guard.check(force=True)
    assert result["safe"] is False
    assert result["open_orders"] == 1


def test_unsafe_when_inactive_exchange_has_open_positions(monkeypatch):
    _patch_inactive(
        monkeypatch, "okx", _FakeClient(positions=[{"contracts": 5.0, "symbol": "BTC/USDT:USDT"}])
    )
    result = guard.check(force=True)
    assert result["safe"] is False
    assert result["open_positions"] == 1


def test_zero_size_positions_do_not_count_as_open(monkeypatch):
    """ccxt обычно уже не возвращает плоские позиции, но проверяем защиту на
    случай, если биржа всё же прислала запись с нулевым размером."""
    _patch_inactive(
        monkeypatch, "okx", _FakeClient(positions=[{"contracts": 0.0, "symbol": "BTC/USDT:USDT"}])
    )
    result = guard.check(force=True)
    assert result["safe"] is True
    assert result["open_positions"] == 0


def test_unreachable_inactive_exchange_fails_open(monkeypatch):
    """Неактивная биржа недоступна — она и так не торгует, это не повод
    останавливать активную."""
    _patch_inactive(monkeypatch, "okx", _FakeClient(raises=ConnectionError("dns fail")))
    result = guard.check(force=True)
    assert result["safe"] is True
    assert result["reachable"] is False
    assert result["error"] is not None


def test_result_is_cached(monkeypatch):
    calls = {"n": 0}

    class _CountingClient(_FakeClient):
        def fetch_open_orders(self, symbol=None):
            calls["n"] += 1
            return super().fetch_open_orders(symbol)

    _patch_inactive(monkeypatch, "okx", _CountingClient())
    guard.check()
    guard.check()
    guard.check()
    assert calls["n"] == 1, "повторные вызовы в пределах TTL не должны бить в сеть заново"


def test_force_bypasses_the_cache(monkeypatch):
    calls = {"n": 0}

    class _CountingClient(_FakeClient):
        def fetch_open_orders(self, symbol=None):
            calls["n"] += 1
            return super().fetch_open_orders(symbol)

    _patch_inactive(monkeypatch, "okx", _CountingClient())
    guard.check()
    guard.check(force=True)
    assert calls["n"] == 2


def test_checks_htx_when_okx_is_active(monkeypatch):
    """Направление проверки следует за settings.active_exchange — проверяем
    ту биржу, которая НЕ торгует, какой бы она ни была."""
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    seen = {}

    def _fake_inactive():
        seen["name"] = "htx"
        return "htx", _FakeClient()

    monkeypatch.setattr(guard, "_inactive_client", _fake_inactive)
    result = guard.check(force=True)
    assert seen["name"] == "htx"
    assert result["inactive_exchange"] == "htx"


def test_inactive_client_selection_is_the_real_thing(monkeypatch):
    """Без подмены _inactive_client — сверяем саму функцию выбора."""
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "htx", raising=False)
    from services.okx_client import OKXClient

    name, client = guard._inactive_client()
    assert name == "okx"
    assert isinstance(client, OKXClient)

    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    from services.htx_client import HTXClient

    name, client = guard._inactive_client()
    assert name == "htx"
    assert isinstance(client, HTXClient)
