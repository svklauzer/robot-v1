"""MarketDataService.source обязан отражать реально выбранную биржу, а не
захардкоженную "htx" (#market-source-label-2026-09-02).

Найдено вживую на Health-странице: ACTIVE_EXCHANGE=okx задеплоен, карточка
Market показывала "htx / <цена>" — цена реально шла с OKX (клиент резолвился
верно), но лейбл источника лгал. Причина: snapshot()/ticker_snapshot() отдавали
"source": "htx" литералом, не спрашивая self.client, какая биржа реально
используется.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from core.config import settings
from services.market_data import MarketDataService


def _client_stub(last=100.0, bid=99.9, ask=100.1):
    stub = MagicMock()
    stub.fetch_ticker.return_value = {"last": last, "bid": bid, "ask": ask}
    stub.fetch_ohlcv.return_value = [[1, 1, 1, 1, 1, 1]] * 200
    return stub


def test_ticker_snapshot_source_follows_explicit_okx_override(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "htx", raising=False)
    service = MarketDataService(exchange="okx")
    service.client = _client_stub()

    assert service.ticker_snapshot("BTC/USDT")["source"] == "okx"


def test_ticker_snapshot_source_follows_active_exchange_when_okx(monkeypatch):
    """Ровно сценарий, увиденный на Health: ACTIVE_EXCHANGE=okx задеплоен,
    источник обязан подписываться "okx", а не оставаться "htx"."""
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    service = MarketDataService()
    service.client = _client_stub()

    assert service.ticker_snapshot("BTC/USDT")["source"] == "okx"


def test_snapshot_source_follows_active_exchange_when_okx(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    service = MarketDataService()
    service.client = _client_stub()

    assert service.snapshot("BTC/USDT")["source"] == "okx"


def test_source_still_defaults_to_htx(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "htx", raising=False)
    service = MarketDataService()
    service.client = _client_stub()

    assert service.ticker_snapshot("BTC/USDT")["source"] == "htx"
    assert service.snapshot("BTC/USDT")["source"] == "htx"


def test_mock_snapshot_source_is_unaffected():
    """mock_snapshot — отдельная ветка (ALLOW_MARKET_MOCK), её "mock"-лейбл не
    должен превратиться в имя биржи."""
    service = MarketDataService()
    assert service.mock_snapshot("BTC/USDT")["source"] == "mock"
