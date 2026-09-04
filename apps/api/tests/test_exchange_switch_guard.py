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


@pytest.fixture(autouse=True)
def _live_mode(monkeypatch):
    """(#switch-guard-paper-2026-09-04) Гейт стал live-only, и тесты ниже
    проверяют именно live-поведение — поэтому режим задаётся явно.

    Раньше он этого не требовал, и это само по себе было симптомом: гейт,
    осмысленный только для реальных денег, одинаково работал в обоих режимах
    и в paper останавливал торговлю из-за постороннего остатка на счёте.
    """
    monkeypatch.setattr(settings, "ENABLE_LIVE_ORDERS", True, raising=False)
    monkeypatch.setattr(settings, "EXCHANGE_SWITCH_GUARD_ENABLED", True, raising=False)


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


# ── paper: у гейта здесь нет предмета (#switch-guard-paper-2026-09-04) ──────
#
# Боевой простой 04.09.2026: 5 часов 27 минут без единого события. Робот
# «running», цикл жив (task_done=false), сеть 100%, обе биржи доступны — и ни
# одной сделки. Держал этот гейт: HTX (неактивная) вернула open_positions=1.
#
# Но бот в paper на биржу НИЧЕГО не выставляет: его позиции живут в таблице
# Position, а fetch_positions() спрашивает РЕАЛЬНЫЙ счёт владельца. Осиротить
# переключением там нечего по построению — гейт проверял предмет, которого в
# этом режиме не существует, и остановил всю бумажную торговлю из-за
# постороннего остатка на счёте.

def test_paper_mode_never_blocks_on_exchange_side_state(monkeypatch):
    """Регресс ровно на простой 04.09: та же позиция на неактивной бирже,
    но в paper — торговля обязана идти."""
    monkeypatch.setattr(settings, "ENABLE_LIVE_ORDERS", False, raising=False)
    monkeypatch.setattr(settings, "TRADING_MODE", "paper_trade", raising=False)
    _patch_inactive(
        monkeypatch, "htx",
        _FakeClient(positions=[{"contracts": 1.0, "symbol": "BTC/USDT:USDT"}]),
    )

    result = guard.check(force=True)

    assert result["safe"] is True
    assert result["error"] == "paper_mode_no_exchange_side_positions"


def test_paper_mode_does_not_touch_the_exchange_at_all(monkeypatch):
    """Побочная выгода: в paper гейт не ходит в сеть вовсе. Раньше он дёргал
    API брошенной биржи каждые 5 минут без какой-либо пользы."""
    monkeypatch.setattr(settings, "ENABLE_LIVE_ORDERS", False, raising=False)
    monkeypatch.setattr(settings, "TRADING_MODE", "paper_trade", raising=False)

    called = {"n": 0}

    class _Exploding:
        def fetch_open_orders(self, symbol=None):
            called["n"] += 1
            raise AssertionError("в paper гейт не имеет права ходить на биржу")

        def fetch_positions(self):
            called["n"] += 1
            raise AssertionError("в paper гейт не имеет права ходить на биржу")

    _patch_inactive(monkeypatch, "htx", _Exploding())

    assert guard.check(force=True)["safe"] is True
    assert called["n"] == 0


def test_live_mode_still_blocks(monkeypatch):
    """Обратная сторона: на реальных деньгах защита обязана остаться —
    именно там переключение действительно может осиротить позицию."""
    monkeypatch.setattr(settings, "ENABLE_LIVE_ORDERS", True, raising=False)
    _patch_inactive(
        monkeypatch, "htx",
        _FakeClient(positions=[{"contracts": 1.0, "symbol": "BTC/USDT:USDT"}]),
    )

    assert guard.check(force=True)["safe"] is False


# ── флаг и видимость ────────────────────────────────────────────────────────

def test_flag_can_disable_the_guard(monkeypatch):
    """У предохранителя, способного остановить ВСЮ торговлю, обязан быть
    выключатель: 04.09 снять его можно было только правкой кода."""
    monkeypatch.setattr(settings, "EXCHANGE_SWITCH_GUARD_ENABLED", False, raising=False)
    _patch_inactive(
        monkeypatch, "htx",
        _FakeClient(positions=[{"contracts": 1.0, "symbol": "BTC/USDT:USDT"}]),
    )

    result = guard.check(force=True)

    assert result["safe"] is True
    assert result["enabled"] is False


def test_result_says_what_exactly_is_open(monkeypatch):
    """Гейт отдавал только счётчик. При `open_positions: 1` нельзя было
    отличить реальный остаток (закрывать руками) от записи без размера,
    засчитанной по fail-closed (чинить код) — а держит он всю торговлю."""
    _patch_inactive(
        monkeypatch, "htx",
        _FakeClient(
            orders=[{"id": "o1", "symbol": "ETH/USDT:USDT", "side": "buy", "amount": 0.5}],
            positions=[{"contracts": 2.0, "symbol": "BTC/USDT:USDT", "side": "long"}],
        ),
    )

    found = guard.check(force=True)["found"]

    assert {"BTC/USDT:USDT", "ETH/USDT:USDT"} == {f["symbol"] for f in found}
    assert all(f["size_unknown"] is False for f in found)


def test_size_unknown_is_flagged_explicitly(monkeypatch):
    """Запись, у которой ccxt не отдал ни одного известного поля размера,
    засчитывается открытой (fail-closed) — но это обязано быть ВИДНО, иначе
    артефакт биржи неотличим от реальной позиции."""
    _patch_inactive(
        monkeypatch, "htx", _FakeClient(positions=[{"symbol": "BTC/USDT:USDT"}]),
    )

    result = guard.check(force=True)

    assert result["safe"] is False
    assert result["found"][0]["size_unknown"] is True
