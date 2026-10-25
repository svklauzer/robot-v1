"""MarketDataService.MARKET_DATA_TYPE: свечи и тикер берутся с того же рынка, на
котором торгуем (#market-data-swap-2026-10-25).

В ccxt символ BTC/USDT — это СПОТ, своп — BTC/USDT:USDT. Без подмены индикаторы и
paper-цены считались по споту, а исполнялось и стоп-контролировалось на свопе.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from core.config import settings
from services.market_data import MarketDataService


def _client(last=100.0):
    stub = MagicMock()
    stub.fetch_ticker.return_value = {"last": last, "bid": last - 0.1, "ask": last + 0.1}
    stub.fetch_ohlcv.return_value = [[i, 1, 1, 1, 1, 1] for i in range(200)]
    return stub


def _svc(monkeypatch, *, exchange="okx", market="swap"):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", exchange, raising=False)
    monkeypatch.setattr(settings, "MARKET_DATA_TYPE", market, raising=False)
    s = MarketDataService(exchange=exchange)
    s.client = _client()
    return s


def test_default_spot_keeps_symbol(monkeypatch):
    s = _svc(monkeypatch, market="spot")
    s.ticker_snapshot("BTC/USDT")
    s.client.fetch_ticker.assert_called_with("BTC/USDT")


def test_swap_okx_requests_contract_symbol_everywhere(monkeypatch):
    s = _svc(monkeypatch)
    snap = s.snapshot("BTC/USDT")
    s.client.fetch_ticker.assert_called_with("BTC/USDT:USDT")
    s.client.fetch_ohlcv.assert_called_with("BTC/USDT:USDT", "5m", 200)
    assert snap["symbol"] == "BTC/USDT"          # ключ системы не меняется
    assert snap["data_market"] == "swap"
    assert s.ticker_snapshot("ETH/USDT")["data_market"] == "swap"
    s.client.fetch_ticker.assert_called_with("ETH/USDT:USDT")
    s.ohlcv("SOL/USDT", timeframe="15m", limit=250)
    s.client.fetch_ohlcv.assert_called_with("SOL/USDT:USDT", timeframe="15m", limit=250)


def test_swap_is_okx_only(monkeypatch):
    s = _svc(monkeypatch, exchange="htx")
    s.ticker_snapshot("BTC/USDT")
    s.client.fetch_ticker.assert_called_with("BTC/USDT")


def test_contract_symbol_not_doubled(monkeypatch):
    s = _svc(monkeypatch)
    s.ticker_snapshot("BTC/USDT:USDT")
    s.client.fetch_ticker.assert_called_with("BTC/USDT:USDT")


def test_unknown_value_means_spot(monkeypatch):
    s = _svc(monkeypatch, market="swapp")
    s.ticker_snapshot("BTC/USDT")
    s.client.fetch_ticker.assert_called_with("BTC/USDT")


def test_symbol_without_swap_falls_back_to_spot(monkeypatch):
    class BadSymbol(Exception):
        pass

    s = _svc(monkeypatch)
    calls = []

    def fetch(sym):
        calls.append(sym)
        if sym.endswith(":USDT"):
            raise BadSymbol("okx does not have market symbol NEW/USDT:USDT")
        return {"last": 1.0, "bid": 0.9, "ask": 1.1}

    s.client.fetch_ticker.side_effect = fetch
    assert s.ticker_snapshot("NEW/USDT")["last"] == 1.0
    assert calls == ["NEW/USDT:USDT", "NEW/USDT"]


def test_network_error_is_not_masked_by_spot(monkeypatch):
    import pytest

    s = _svc(monkeypatch)
    s.client.fetch_ticker.side_effect = TimeoutError("net")
    with pytest.raises(TimeoutError):
        s.ticker_snapshot("BTC/USDT")
    assert s.client.fetch_ticker.call_count == 1
