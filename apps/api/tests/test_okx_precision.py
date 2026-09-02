"""OKXClient.amount_to_precision / contract_size — сверка логикой, а не сетью
(#okx-satellite-2026-09-02, план раздел 1/8).

HTXClient.amount_to_precision конвертирует объём в целые контракты для
линейных свопов — это стандартное поведение КОНТРАКТНЫХ рынков вообще (не
HTX-специфика: ccxt нормализует contract/contractSize одинаково для любой
биржи), но живьём против настоящего ccxt.okx().load_markets() не сверялось
(нет сетевого доступа в этой среде). Этот файл фиксирует логику через
фикстурные market metadata — воспроизводит форму, которую ccxt реально отдаёт
для OKX linear-swap рынков (contract=True, contractSize=<база>, тот же формат,
что и HTX), без обращения к сети.
"""
from __future__ import annotations

import pytest

from services.okx_client import OKXClient


class _FakeExchange:
    """Минимальная замена ccxt.okx — только то, что использует OKXClient."""

    def __init__(self, markets: dict):
        self.markets = markets

    def market(self, symbol):
        return self.markets[symbol]

    def amount_to_precision(self, symbol, amount):
        # Спот-путь ccxt: округление по step size, здесь — просто до 6 знаков.
        return round(float(amount), 6)

    def price_to_precision(self, symbol, price):
        return round(float(price), 4)


# Форма, в которой ccxt отдаёт OKX linear-swap рынок: contract=True +
# contractSize в базовой монете — тот же контракт, что и для HTX.
_OKX_SWAP_MARKETS = {
    "BTC/USDT:USDT": {"contract": True, "contractSize": 0.01, "limits": {}},
    "ADA/USDT:USDT": {"contract": True, "contractSize": 10.0, "limits": {}},
}
_OKX_SPOT_MARKETS = {
    "BTC/USDT": {"contract": False, "limits": {}},
}


@pytest.fixture(autouse=True)
def _reset_market_cache():
    # _cached_markets — состояние КЛАССА, разделяемое между тестами. Без сброса
    # фикстурный набор рынков из одного теста просочился бы в следующий через
    # load_markets()'ый быстрый путь.
    OKXClient._cached_markets = {}
    OKXClient._markets_loaded = False
    yield
    OKXClient._cached_markets = {}
    OKXClient._markets_loaded = False


def _client(markets: dict) -> OKXClient:
    c = OKXClient.__new__(OKXClient)
    c.exchange = _FakeExchange(markets)
    return c


def test_swap_amount_rounds_down_to_whole_contracts():
    c = _client(_OKX_SWAP_MARKETS)
    # 0.0456 BTC / 0.01 per contract = 4.56 contracts -> floor to 4 -> 0.04 BTC
    result = c.amount_to_precision("BTC/USDT:USDT", 0.0456)
    assert result == pytest.approx(0.04)


def test_swap_amount_below_one_contract_rounds_up_to_the_minimum():
    """Ниже одного контракта, но > 0: HTX-логика подстраховывает минимум в
    1 контракт, а не в ноль — иначе сделка молча исчезает."""
    c = _client(_OKX_SWAP_MARKETS)
    result = c.amount_to_precision("BTC/USDT:USDT", 0.003)  # < 0.01 (1 contract)
    assert result == pytest.approx(0.01)


def test_swap_amount_never_exceeds_the_requested_amount_by_more_than_one_contract():
    c = _client(_OKX_SWAP_MARKETS)
    requested = 0.0479
    result = c.amount_to_precision("BTC/USDT:USDT", requested)
    contract_size = 0.01
    assert result <= requested + contract_size

def test_spot_market_uses_ccxt_precision_not_contract_math():
    c = _client(_OKX_SPOT_MARKETS)
    result = c.amount_to_precision("BTC/USDT", 0.123456789)
    assert result == pytest.approx(0.123457)


def test_contract_size_reads_swap_market_metadata():
    c = _client(_OKX_SWAP_MARKETS)
    assert c.contract_size("ADA/USDT:USDT") == pytest.approx(10.0)


def test_contract_size_is_none_for_spot():
    c = _client(_OKX_SPOT_MARKETS)
    assert c.contract_size("BTC/USDT") is None


def test_contract_size_is_none_for_unknown_symbol():
    c = _client({})

    class _NoMarket(_FakeExchange):
        def market(self, symbol):
            raise KeyError(symbol)

    c.exchange = _NoMarket({})
    assert c.contract_size("XYZ/USDT") is None
