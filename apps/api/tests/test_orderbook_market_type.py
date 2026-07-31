"""Книга должна быть от того же инструмента, которым торгуем (#ob-market-2026-07-30).

Спот и перпетуал — разные книги. Пока фид был жёстко спотовым, шорты (они всегда
идут на своп) принимали решения о входе по чужому стакану: OBI, CVD, стенки и
перенос входа к уровню в entry_zone. После ENABLE_FUTURES_EXECUTION=true это
стало верно для всей вселенной.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import orderbook_feed as ob


@pytest.fixture(autouse=True)
def _no_explicit_url(monkeypatch):
    monkeypatch.setattr(settings, "OB_WS_URL", "", raising=False)


def test_spot_symbol_format():
    assert ob._ws_symbol("BTC/USDT", "spot") == "btcusdt"


def test_swap_symbol_format():
    """HTX linear-swap ждёт BTC-USDT в верхнем регистре, а не btcusdt."""
    assert ob._ws_symbol("BTC/USDT", "swap") == "BTC-USDT"


def test_contract_suffix_is_stripped_on_both_markets():
    assert ob._ws_symbol("BTC/USDT:USDT", "swap") == "BTC-USDT"
    assert ob._ws_symbol("BTC/USDT:USDT", "spot") == "btcusdt"


def test_endpoint_follows_market_type(monkeypatch):
    monkeypatch.setattr(settings, "OB_MARKET_TYPE", "spot", raising=False)
    assert "huobi.pro" in ob.ws_url()
    monkeypatch.setattr(settings, "OB_MARKET_TYPE", "swap", raising=False)
    assert "linear-swap" in ob.ws_url()


def test_explicit_url_overrides_market_type(monkeypatch):
    monkeypatch.setattr(settings, "OB_MARKET_TYPE", "swap", raising=False)
    monkeypatch.setattr(settings, "OB_WS_URL", "wss://example/ws", raising=False)
    assert ob.ws_url() == "wss://example/ws"


def test_unknown_market_type_falls_back_to_spot(monkeypatch):
    monkeypatch.setattr(settings, "OB_MARKET_TYPE", "нечто", raising=False)
    assert ob.ob_market_type() == "spot"
    assert "huobi.pro" in ob.ws_url()
