"""Лимитный вход (#limit-entry-2026-09-19).

Владелец решил входить лимитом, а не по рынку: тейкерская ставка 0.05% против
мейкерской 0.02%, и на входе исчезает спред. Зона входа уже считает цену лучше
рынка — до сих пор ей пользовалась только бумага, а live слал market и получал
фактический филл.

Здесь проверяется механика отправки: форму запроса собирает НАСТОЯЩИЙ ccxt без
сети, а поведение по сроку жизни — на двойнике биржи.
"""
from __future__ import annotations

import ccxt
import pytest

from core.config import settings
from services.live_executor import LiveExecutor

SYMBOL = "XRP/USDT:USDT"


def _swap_market() -> dict:
    return {
        "id": "XRP-USDT-SWAP", "symbol": SYMBOL, "base": "XRP", "quote": "USDT", "settle": "USDT",
        "baseId": "XRP", "quoteId": "USDT", "settleId": "USDT", "subType": "linear",
        "type": "swap", "spot": False, "margin": False, "swap": True, "future": False,
        "option": False, "active": True, "contract": True, "linear": True, "inverse": False,
        "contractSize": 100.0,
        "precision": {"amount": 0.01, "price": 0.0001},
        "limits": {"amount": {"min": 0.01, "max": None}, "price": {}, "cost": {}, "leverage": {}},
        "info": {"instId": "XRP-USDT-SWAP"},
    }


@pytest.fixture
def okx_ccxt():
    ex = ccxt.okx({"apiKey": "k", "secret": "s", "password": "p"})
    ex.set_markets([_swap_market()])
    return ex


def test_limit_entry_goes_post_only_to_the_exchange(okx_ccxt):
    """postOnly — весь смысл лимитного входа: ордер обязан встать мейкером или
    не встать вовсе. Без него вход молча превращается в тейкерский, и экономия,
    ради которой он заводился, исчезает."""
    params = {**LiveExecutor.order_params(client_id="rbtlimit01", market_type="swap", side="buy",
                                          reduce_only=False, margin_mode="isolated", hedged=False,
                                          position_side_key="positionSide"),
              "postOnly": True}

    body = okx_ccxt.create_order_request(SYMBOL, "limit", "buy", 1.5, 1.4097, params)

    assert body["ordType"] == "post_only"
    assert body["px"] == "1.4097"
    assert body["tdMode"] == "isolated"


# ── поведение по сроку жизни ────────────────────────────────────────────────
class _Exchange:
    """Двойник биржи: отдаёт заранее заданную судьбу ордера."""

    UNIFIED_TRADING_ACCOUNT = True

    def __init__(self, *, fills: float = 0.0, status: str = "open"):
        self.fills = fills
        self.status = status
        self.created: list = []
        self.cancelled: list = []

    def contract_size(self, _symbol):
        return 100.0

    def market_limits(self, _symbol):
        return {"amount_unit": "contracts", "min_amount": 0.01, "amount_step": 0.01}

    def amount_to_precision(self, _symbol, amount):
        return amount

    def create_order_once(self, symbol, order_type, side, amount, price, params):
        self.created.append({"type": order_type, "price": price, "params": params, "amount": amount})
        return {"id": "o1", "status": "open", "filled": 0.0, "average": None}

    def fetch_order(self, _oid, _symbol):
        return {"id": "o1", "status": self.status, "filled": self.fills, "average": 1.4097}

    def cancel_order(self, oid, symbol):
        self.cancelled.append((oid, symbol))


def _executor(exchange) -> LiveExecutor:
    ex = LiveExecutor.__new__(LiveExecutor)
    ex.client = exchange
    ex._bal_cache = {}
    ex._account_state = {"hedged": False, "blocker": None, "info": {}}
    return ex


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    monkeypatch.setattr(LiveExecutor, "derivatives_account",
                        lambda self: {"hedged": False, "blocker": None, "info": {}})
    monkeypatch.setattr(LiveExecutor, "_ensure_leverage",
                        lambda self, *a, **k: None)
    monkeypatch.setattr(settings, "LIVE_FILL_POLL_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_PCT", 0.0)
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 0.0)


def test_unfilled_limit_is_cancelled_and_is_not_an_error():
    """Цена не вернулась — сделки просто нет. Писать сюда ошибку нельзя: kill
    switch посчитал бы обычный неисполненный вход отказом биржи."""
    exchange = _Exchange(fills=0.0, status="open")

    result = _executor(exchange).place_limit(
        SYMBOL, "buy", 150.0, market_type="swap", limit_price=1.4097, ttl_sec=0.0,
        margin_mode="isolated", purpose="trend_open")

    assert exchange.cancelled == [("o1", SYMBOL)]
    assert result.ok is False and result.status == "unfilled"
    assert result.error is None and result.filled_qty == 0.0


def test_partial_fill_keeps_what_the_exchange_gave():
    """Часть позиции на бирже есть — учёт обязан её знать, иначе выходы уйдут
    в пустоту."""
    exchange = _Exchange(fills=0.6, status="open")

    result = _executor(exchange).place_limit(
        SYMBOL, "buy", 150.0, market_type="swap", limit_price=1.4097, ttl_sec=0.0,
        margin_mode="isolated", purpose="trend_open")

    assert result.ok is True
    assert result.filled_qty == pytest.approx(60.0)   # 0.6 контракта × 100
    assert exchange.cancelled == [("o1", SYMBOL)]


def test_filled_limit_needs_no_cancel():
    exchange = _Exchange(fills=1.5, status="closed")

    result = _executor(exchange).place_limit(
        SYMBOL, "buy", 150.0, market_type="swap", limit_price=1.4097, ttl_sec=0.0,
        margin_mode="isolated", purpose="trend_open")

    assert result.ok is True and result.status == "closed"
    assert exchange.cancelled == []
    assert exchange.created[0]["type"] == "limit"
    assert exchange.created[0]["price"] == 1.4097
    assert exchange.created[0]["params"]["postOnly"] is True


def test_market_entry_is_untouched():
    """Рыночный путь не должен ничего знать о лимитах."""
    exchange = _Exchange(fills=1.5, status="closed")

    result = _executor(exchange).place_market(
        SYMBOL, "buy", 150.0, market_type="swap", reference_price=1.41,
        margin_mode="isolated", purpose="trend_open")

    assert result.ok is True
    assert exchange.created[0]["type"] == "market" and exchange.created[0]["price"] is None
    assert "postOnly" not in exchange.created[0]["params"]
    assert exchange.cancelled == []


def test_both_entries_share_one_body():
    """Режим маржи, режим счёта, плечо, контракты, идемпотентность — всё это
    одинаково нужно обоим типам ордера. Вторая копия начала бы отставать."""
    import inspect

    source = inspect.getsource(LiveExecutor)
    assert source.count("def _place(") == 1
    for wrapper in ("def place_market(", "def place_limit("):
        body = source.split(wrapper, 1)[1].split("\n    def ", 1)[0]
        assert "self._place(" in body, wrapper
