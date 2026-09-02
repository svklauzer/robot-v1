"""get_exchange_client() — единственная точка выбора биржи (#okx-satellite-2026-09-02).

Каждый сервис, который раньше жёстко конструировал HTXClient(), теперь зовёт
эту функцию — если она когда-нибудь выберет не тот клиент, сломаются все 8
точек разом одинаково, поэтому эта единственная функция и есть то, что нужно
держать под тестом строже всего.
"""
from __future__ import annotations

from core.config import settings
from services.exchange_factory import get_exchange_client
from services.htx_client import HTXClient
from services.okx_client import OKXClient


def test_defaults_to_htx(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "htx", raising=False)
    assert isinstance(get_exchange_client(), HTXClient)


def test_switches_to_okx(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    assert isinstance(get_exchange_client(), OKXClient)


def test_falls_back_to_htx_on_garbage_value(monkeypatch):
    """Опечатка в env (см. Settings.active_exchange) не имеет права выбрать
    неопределённое поведение — только уже проверенную HTX."""
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "bybit", raising=False)
    assert isinstance(get_exchange_client(), HTXClient)


def test_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "OKX", raising=False)
    assert isinstance(get_exchange_client(), OKXClient)


def test_explicit_override_wins_over_active_exchange(monkeypatch):
    """(#okx-satellite-exchange-routing-2026-09-02) Уже открытый Signal должен
    вестись через свою биржу независимо от того, что сейчас активно."""
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    assert isinstance(get_exchange_client("htx"), HTXClient)


def test_explicit_override_okx_regardless_of_active(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "htx", raising=False)
    assert isinstance(get_exchange_client("okx"), OKXClient)


def test_none_override_preserves_active_exchange_behavior(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    assert isinstance(get_exchange_client(None), OKXClient)
