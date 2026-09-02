"""ACTIVE_EXCHANGE — единственная точка переключения HTX/OKX (#okx-satellite-2026-09-02).

Переключение только ручное (Render env + редеплой), обе биржи никогда не
торгуют одновременно. Дефолт "htx" обязан сохранять боевое поведение без
единого изменения конфига.
"""
from __future__ import annotations

import pytest

from core.config import Settings, settings
from services.exchange_client import ExchangeClient
from services.htx_client import HTXClient
from services.okx_client import OKXClient


# ── active_exchange: нормализация, fail-safe ────────────────────────────────

def test_active_exchange_defaults_to_htx():
    assert Settings().active_exchange == "htx"


def test_active_exchange_accepts_okx():
    assert Settings(ACTIVE_EXCHANGE="okx").active_exchange == "okx"


def test_active_exchange_is_case_and_whitespace_insensitive():
    assert Settings(ACTIVE_EXCHANGE=" OKX ").active_exchange == "okx"


def test_active_exchange_falls_back_to_htx_on_garbage():
    """Опечатка в env не имеет права выбрать неопределённое поведение —
    только уже проверенную HTX."""
    assert Settings(ACTIVE_EXCHANGE="binance").active_exchange == "htx"
    assert Settings(ACTIVE_EXCHANGE="").active_exchange == "htx"


# ── production_blockers(): креденшлы только активной биржи ──────────────────

def _prod_settings(**over):
    base = dict(
        APP_ENV="production",
        DB_AUTO_CREATE_SCHEMA=False,
        JWT_SECRET="real-secret",
        OWNER_PASSWORD="real-password",
        OWNER_API_TOKEN="token",
        TELEGRAM_BOT_TOKEN="token",
    )
    base.update(over)
    return Settings(**base)


def test_htx_creds_required_when_htx_is_active():
    blockers = _prod_settings(ACTIVE_EXCHANGE="htx", HTX_API_KEY="", HTX_API_SECRET="").production_blockers()
    assert any("HTX API credentials" in b for b in blockers), blockers
    assert not any("OKX API credentials" in b for b in blockers), blockers


def test_htx_creds_not_required_when_okx_is_active():
    """Неактивная биржа не используется для торговли — её отсутствующие ключи
    не обязаны блокировать прод."""
    blockers = _prod_settings(
        ACTIVE_EXCHANGE="okx",
        HTX_API_KEY="", HTX_API_SECRET="",
        OKX_API_KEY="k", OKX_API_SECRET="s", OKX_API_PASSPHRASE="p",
    ).production_blockers()
    assert not any("HTX API credentials" in b for b in blockers), blockers
    assert not any("OKX API credentials" in b for b in blockers), blockers


def test_okx_creds_required_when_okx_is_active():
    blockers = _prod_settings(
        ACTIVE_EXCHANGE="okx", OKX_API_KEY="", OKX_API_SECRET="", OKX_API_PASSPHRASE="",
    ).production_blockers()
    assert any("OKX API credentials" in b for b in blockers), blockers


def test_okx_creds_missing_passphrase_still_blocks():
    """OKX требует третий креденшл — key+secret без passphrase не считаются
    настроенными: боевые вызовы всё равно упадут на авторизации."""
    blockers = _prod_settings(
        ACTIVE_EXCHANGE="okx", OKX_API_KEY="k", OKX_API_SECRET="s", OKX_API_PASSPHRASE="",
    ).production_blockers()
    assert any("OKX API credentials" in b for b in blockers), blockers


def test_okx_creds_not_required_when_htx_is_active():
    blockers = _prod_settings(
        ACTIVE_EXCHANGE="htx",
        HTX_API_KEY="k", HTX_API_SECRET="s",
        OKX_API_KEY="", OKX_API_SECRET="", OKX_API_PASSPHRASE="",
    ).production_blockers()
    assert not any("OKX API credentials" in b for b in blockers), blockers


# ── контракт: HTXClient/OKXClient структурно совпадают с ExchangeClient ─────

def test_htx_and_okx_clients_satisfy_the_exchange_client_protocol():
    """(#okx-satellite-2026-09-02) Формальная проверка, что OKXClient не забыл
    ни одного метода из общего торгового контракта — вручную сверять 19 методов
    глазами ненадёжно, а расхождение обнаружилось бы только посреди сделки."""
    assert isinstance(HTXClient.__new__(HTXClient), ExchangeClient)
    assert isinstance(OKXClient.__new__(OKXClient), ExchangeClient)
