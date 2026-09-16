"""Режим маржи и режим позиций в live-ордерах (#live-margin-posmode-2026-09-16).

Аудит 16.09 перед выходом в live, пункты 3 и 4:

3. Ордер уходил без `marginMode`, и ccxt подставлял cross (OKX tdMode=cross,
   HTX margin_mode=cross). План сделки — isolated 1×: позиция рисковала бы всем
   счётом, а `_ensure_leverage` настраивал плечо под isolated. Сам
   `_ensure_leverage` звал `set_margin_mode` без `lever` (ccxt okx падает) и
   `set_leverage` без режима (ставит cross), ошибку только писал в лог — ордер
   уходил при том плече, что стояло на бирже.
4. Стороны позиции не было: аккаунт OKX в Long/Short mode отклонял бы каждый
   ордер.

Для HTX там же: ccxt htx ведёт свопы через API v5 (только для мультивалютного
залога — объявление HTX 08.05.2025), сторону позиции v5 не разбирает, а
client_order_id принимает только целым числом.

Запросы ниже собирает НАСТОЯЩИЙ ccxt без сети — проверяется ровно то, что
получила бы биржа.
"""
from __future__ import annotations

from types import SimpleNamespace

import ccxt
import pytest

from core.config import settings
from services.htx_client import HTXClient
from services.live_executor import LiveExecutor
from services.okx_client import OKXClient

SYMBOL = "XRP/USDT:USDT"


# ── запросы, которые собирает ccxt ────────────────────────────────────────────
def _swap_market(market_id: str, amount_step: float) -> dict:
    return {
        "id": market_id, "symbol": SYMBOL, "base": "XRP", "quote": "USDT", "settle": "USDT",
        "baseId": "XRP", "quoteId": "USDT", "settleId": "USDT", "subType": "linear",
        "type": "swap", "spot": False, "margin": False, "swap": True, "future": False,
        "option": False, "active": True, "contract": True, "linear": True, "inverse": False,
        "contractSize": 100.0,
        "precision": {"amount": amount_step, "price": 0.0001},
        "limits": {"amount": {"min": amount_step, "max": None}, "price": {}, "cost": {}, "leverage": {}},
        "info": {"instId": market_id},
    }


@pytest.fixture
def okx_ccxt():
    ex = ccxt.okx({"apiKey": "k", "secret": "s", "password": "p"})
    ex.set_markets([_swap_market("XRP-USDT-SWAP", 0.01)])
    return ex


@pytest.fixture
def htx_ccxt():
    ex = ccxt.htx({"apiKey": "k", "secret": "s"})
    ex.set_markets([_swap_market("XRP-USDT", 1)])
    return ex


def _params(side="sell", reduce_only=False, hedged=False, market_type="swap", margin_mode="isolated",
            client_id="trendopen0123", position_side_key="positionSide"):
    return LiveExecutor.order_params(client_id=client_id, market_type=market_type, side=side,
                                     reduce_only=reduce_only, margin_mode=margin_mode, hedged=hedged,
                                     position_side_key=position_side_key)


def test_okx_open_goes_isolated_not_cross(okx_ccxt):
    body = okx_ccxt.create_order_request(SYMBOL, "market", "sell", 1.5, None, _params())
    assert body["tdMode"] == "isolated"
    assert "posSide" not in body and "reduceOnly" not in body
    assert body["clOrdId"] == "trendopen0123"


def test_okx_close_is_reduce_only_in_the_same_margin_mode(okx_ccxt):
    body = okx_ccxt.create_order_request(SYMBOL, "market", "buy", 1.5, None,
                                         _params(side="buy", reduce_only=True))
    assert body["tdMode"] == "isolated"
    assert body["reduceOnly"] is True


def test_okx_long_short_mode_names_the_position_side(okx_ccxt):
    opened = okx_ccxt.create_order_request(SYMBOL, "market", "sell", 1.5, None, _params(hedged=True))
    closed = okx_ccxt.create_order_request(SYMBOL, "market", "buy", 1.5, None,
                                           _params(side="buy", reduce_only=True, hedged=True))
    assert opened["posSide"] == "short" and opened["tdMode"] == "isolated"
    # Закрытие шорта в Long/Short mode — buy по стороне short; reduceOnly там не применяется.
    assert closed["posSide"] == "short" and "reduceOnly" not in closed


def test_no_unknown_keys_reach_the_exchange(okx_ccxt, htx_ccxt):
    """Прежде в тело ордера уходил `defaultType` — ccxt его не разбирает."""
    okx_body = okx_ccxt.create_order_request(SYMBOL, "market", "sell", 1.5, None, _params())
    htx_body = htx_ccxt.create_contract_order_request(SYMBOL, "market", "sell", 2, None, _params())
    for body in (okx_body, htx_body):
        assert "defaultType" not in body and "marginMode" not in body


def test_htx_order_carries_margin_mode(htx_ccxt):
    body = htx_ccxt.create_contract_order_request(SYMBOL, "market", "buy", 2, None,
                                                  _params(side="buy", reduce_only=True))
    assert body["margin_mode"] == "isolated"
    assert body["reduce_only"] == 1


def _htx_params(**kw):
    kw.setdefault("client_id", HTXClient.make_client_order_id("trend_open"))
    return _params(position_side_key=HTXClient.POSITION_SIDE_PARAM, **kw)


def test_htx_client_order_id_reaches_the_exchange_intact(htx_ccxt):
    """Буквенный номер ccxt htx не передаёт, а оставляет в теле как clientOrderId;
    числовой переводит через float — номер обязан дойти до биржи без искажений,
    иначе сверка после обрыва связи его не найдёт."""
    for _ in range(200):
        params = _htx_params()
        body = htx_ccxt.create_contract_order_request(SYMBOL, "market", "sell", 2, None, params)
        assert str(body["client_order_id"]) == params["clientOrderId"]
        assert "clientOrderId" not in body


def test_htx_dual_side_names_the_position_side(htx_ccxt):
    opened = htx_ccxt.create_contract_order_request(SYMBOL, "market", "sell", 2, None,
                                                    _htx_params(hedged=True))
    closed = htx_ccxt.create_contract_order_request(SYMBOL, "market", "buy", 2, None,
                                                    _htx_params(side="buy", reduce_only=True, hedged=True))
    assert opened["position_side"] == "short" and opened["margin_mode"] == "isolated"
    assert closed["position_side"] == "short" and "reduce_only" not in closed
    assert "positionSide" not in opened and "positionSide" not in closed


def test_client_order_id_formats():
    for _ in range(50):
        htx_id = HTXClient.make_client_order_id("trend_open")
        assert htx_id.isdigit() and 0 < int(htx_id) < 2 ** 53
        okx_id = OKXClient.make_client_order_id("trend_open")
        assert okx_id.isalnum() and len(okx_id) <= 32 and okx_id.startswith("trendope")


def test_spot_order_has_no_margin_fields():
    params = _params(side="sell", reduce_only=True, market_type="spot", margin_mode=None)
    assert params == {"clientOrderId": "trendopen0123", "reduceOnly": True}


@pytest.fixture
def direct_retry(monkeypatch):
    monkeypatch.setattr(OKXClient, "_retry", lambda self, fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(HTXClient, "_retry", lambda self, fn, *a, **k: fn(*a, **k))


def test_okx_leverage_is_set_for_the_trade_margin_mode(okx_ccxt, direct_retry):
    sent = []
    okx_ccxt.privatePostAccountSetLeverage = lambda req: sent.append(req) or {}
    client = OKXClient.__new__(OKXClient)
    client.exchange = okx_ccxt

    client.set_swap_leverage(SYMBOL, 1, "isolated")
    client.set_swap_leverage(SYMBOL, 1, "isolated", "short")

    assert sent[0] == {"lever": 1, "mgnMode": "isolated", "instId": "XRP-USDT-SWAP", "posSide": "net"}
    assert sent[1]["posSide"] == "short"


def test_htx_leverage_body_has_no_foreign_keys(htx_ccxt, direct_retry):
    sent = []
    htx_ccxt.contractPrivatePostV5PositionLever = lambda req: sent.append(req) or {}
    client = HTXClient.__new__(HTXClient)
    client.exchange = htx_ccxt

    client.set_swap_leverage(SYMBOL, 1, "isolated")
    client.set_swap_leverage(SYMBOL, 1, "isolated", "long")

    assert sent[0] == {"lever_rate": 1, "contract_code": "XRP-USDT", "margin_mode": "isolated"}
    assert sent[1]["position_side"] == "long"


# ── режим счёта ────────────────────────────────────────────────────────────────
def _htx_with_modes(htx_ccxt, asset_mode, position_mode):
    calls = []

    def _asset(params=None):
        calls.append("asset_mode")
        return {"code": 200, "message": "Success", "data": {"asset_mode": asset_mode}}

    def _position(params=None):
        calls.append("position_mode")
        return {"code": 200, "message": "Success", "data": {"position_mode": position_mode}}

    htx_ccxt.contractPrivateGetV5AccountAssetMode = _asset
    htx_ccxt.contractPrivateGetV5PositionMode = _position
    client = HTXClient.__new__(HTXClient)
    client.exchange = htx_ccxt
    return client, calls


def test_htx_multi_assets_dual_side_is_hedged(htx_ccxt, direct_retry):
    client, _ = _htx_with_modes(htx_ccxt, 1, "dual_side")
    state = client.fetch_derivatives_account()
    assert state["hedged"] is True and state["blocker"] is None


def test_htx_new_single_asset_one_way_is_allowed(htx_ccxt, direct_retry):
    client, _ = _htx_with_modes(htx_ccxt, 2, "single_side")
    state = client.fetch_derivatives_account()
    assert state["hedged"] is False and state["blocker"] is None


def test_htx_old_single_asset_mode_blocks(htx_ccxt, direct_retry):
    client, calls = _htx_with_modes(htx_ccxt, 0, "single_side")
    state = client.fetch_derivatives_account()
    assert state["blocker"].startswith("htx_asset_mode_single_old")
    assert calls == ["asset_mode"], "режим позиций у такого счёта не спрашиваем"


@pytest.mark.parametrize("acct_lv, pos_mode, hedged, blocked", [
    ("1", "net_mode", False, True),
    ("2", "net_mode", False, False),
    ("2", "long_short_mode", True, False),
    ("3", "net_mode", False, False),
])
def test_okx_account_mode(okx_ccxt, direct_retry, acct_lv, pos_mode, hedged, blocked):
    okx_ccxt.privateGetAccountConfig = lambda params=None: {
        "code": "0", "msg": "",
        "data": [{"uid": "1", "acctLv": acct_lv, "posMode": pos_mode, "mainUid": "1"}],
    }
    client = OKXClient.__new__(OKXClient)
    client.exchange = okx_ccxt

    state = client.fetch_derivatives_account()

    assert state["hedged"] is hedged
    assert bool(state["blocker"]) is blocked


def test_okx_leverage_error_is_not_swallowed(okx_ccxt, direct_retry):
    def _reject(_req):
        raise ccxt.BadRequest("okx 59000")

    okx_ccxt.privatePostAccountSetLeverage = _reject
    client = OKXClient.__new__(OKXClient)
    client.exchange = okx_ccxt
    with pytest.raises(ccxt.BadRequest):
        client.set_swap_leverage(SYMBOL, 1, "isolated")


# ── путь live-ордера ───────────────────────────────────────────────────────────
class _Client:
    POSITION_SIDE_PARAM = "positionSide"

    def __init__(self, *, hedged=False, blocker=None, leverage_error=None, mode_error=None,
                 create_error=None):
        self.hedged = hedged
        self.blocker = blocker
        self.leverage_error = leverage_error
        self.mode_error = mode_error
        self.create_error = create_error
        self.leverage_calls: list[tuple] = []
        self.mode_calls = 0
        self.orders: list[dict] = []

    def contract_size(self, _symbol):
        return 100.0

    def amount_to_precision(self, _symbol, amount):
        return float(amount)

    def fetch_derivatives_account(self):
        self.mode_calls += 1
        if self.mode_error:
            raise self.mode_error
        return {"hedged": self.hedged, "blocker": self.blocker}

    @staticmethod
    def make_client_order_id(purpose):
        return f"id{purpose}"

    def set_swap_leverage(self, symbol, leverage, margin_mode, position_side=None):
        self.leverage_calls.append((symbol, leverage, margin_mode, position_side))
        if self.leverage_error:
            raise self.leverage_error

    def create_order_once(self, symbol, type_, side, amount, price=None, params=None):
        if self.create_error:
            raise self.create_error
        self.orders.append({"symbol": symbol, "side": side, "amount": amount, "params": dict(params)})
        return {"id": "1", "status": "closed", "filled": amount, "average": 1.4}

    def fetch_open_orders(self, _symbol):
        return []

    def fetch_closed_orders(self, _symbol, limit=20):
        return []


def _executor(monkeypatch, client: _Client) -> LiveExecutor:
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 0.0)
    monkeypatch.setattr(settings, "LIVE_SET_LEVERAGE", True)
    monkeypatch.setattr(settings, "LIVE_MARGIN_MODE", "cross")
    ex = LiveExecutor.__new__(LiveExecutor)
    ex.client = client
    ex._leverage_set = set()
    ex._bal_cache = {}
    ex._account_state = None
    return ex


def _open(ex, side="sell", **kw):
    kw.setdefault("margin_mode", "isolated")
    kw.setdefault("leverage", 1)
    return ex.place_market(SYMBOL, side, 150.0, market_type="swap", reference_price=1.4,
                           purpose="trend_open", **kw)


def _close(ex, side="buy", **kw):
    kw.setdefault("margin_mode", "isolated")
    return ex.place_market(SYMBOL, side, 150.0, market_type="swap", reduce_only=True,
                           reference_price=1.4, purpose="trend_close", **kw)


def test_open_sets_leverage_then_sends_isolated(monkeypatch):
    client = _Client()
    res = _open(_executor(monkeypatch, client))

    assert res.ok and res.sent
    assert client.leverage_calls == [(SYMBOL, 1, "isolated", None)]
    assert client.orders[0]["params"] == {"clientOrderId": res.client_order_id, "marginMode": "isolated"}
    assert client.orders[0]["amount"] == pytest.approx(1.5)


def test_leverage_failure_refuses_the_open(monkeypatch):
    client = _Client(leverage_error=RuntimeError("59107 position exists"))
    res = _open(_executor(monkeypatch, client))

    assert not res.ok and not res.sent
    assert res.error.startswith("leverage_setup_failed:")
    assert client.orders == [], "без подтверждённого плеча позиция не открывается"


def test_close_never_waits_on_leverage(monkeypatch):
    client = _Client(leverage_error=RuntimeError("leverage api down"))
    res = _close(_executor(monkeypatch, client))

    assert res.ok and res.sent
    assert client.leverage_calls == []
    assert client.orders[0]["params"]["reduceOnly"] is True
    assert client.orders[0]["params"]["marginMode"] == "isolated"


def test_long_short_account_gets_position_side(monkeypatch):
    client = _Client(hedged=True)
    ex = _executor(monkeypatch, client)

    _open(ex, side="sell")
    _close(ex, side="buy")

    assert client.leverage_calls == [(SYMBOL, 1, "isolated", "short")]
    opened, closed = (o["params"] for o in client.orders)
    assert opened["positionSide"] == "short"
    assert closed["positionSide"] == "short" and "reduceOnly" not in closed
    assert client.mode_calls == 1, "режим позиций кешируется, а не спрашивается на каждый ордер"


def test_route_without_margin_mode_uses_live_margin_mode(monkeypatch):
    client = _Client()
    ex = _executor(monkeypatch, client)
    monkeypatch.setattr(settings, "LIVE_MARGIN_MODE", "Isolated")

    _open(ex, margin_mode=None)

    assert client.orders[0]["params"]["marginMode"] == "isolated"


def test_unknown_margin_mode_is_refused_not_defaulted_to_cross(monkeypatch):
    client = _Client()
    ex = _executor(monkeypatch, client)
    monkeypatch.setattr(settings, "LIVE_MARGIN_MODE", "portfolio")

    res = _open(ex, margin_mode=None)

    assert not res.ok and res.error == "margin_mode_invalid:portfolio"
    assert client.orders == [] and client.leverage_calls == []


def test_leverage_is_set_once_per_mode_and_value(monkeypatch):
    client = _Client()
    ex = _executor(monkeypatch, client)

    _open(ex)
    _open(ex)
    _open(ex, leverage=2)

    assert client.leverage_calls == [(SYMBOL, 1, "isolated", None), (SYMBOL, 2, "isolated", None)]


def test_leverage_is_capped(monkeypatch):
    client = _Client()
    ex = _executor(monkeypatch, client)
    monkeypatch.setattr(settings, "LIVE_MAX_LEVERAGE", 3.0)

    _open(ex, leverage=20)

    assert client.leverage_calls == [(SYMBOL, 3, "isolated", None)]


def test_position_mode_fetch_failure_falls_back_to_one_way(monkeypatch):
    client = _Client(mode_error=RuntimeError("timeout"))
    res = _open(_executor(monkeypatch, client))

    assert res.ok
    assert "positionSide" not in client.orders[0]["params"]


def test_position_mode_failure_keeps_the_last_known_mode(monkeypatch):
    client = _Client(hedged=True)
    ex = _executor(monkeypatch, client)
    assert ex.position_hedged() is True

    ex._account_state = ({"hedged": True, "blocker": None}, 0.0)   # кеш устарел
    client.mode_error = RuntimeError("timeout")
    assert ex.position_hedged() is True


def test_rejected_order_forgets_the_cached_position_mode(monkeypatch):
    client = _Client(create_error=RuntimeError("51000 posSide error"))
    ex = _executor(monkeypatch, client)

    res = _open(ex)

    assert not res.ok
    assert ex._account_state is None


def test_account_mode_blocker_refuses_open_but_not_close(monkeypatch):
    client = _Client(blocker="okx_account_mode_spot_only: ...")
    ex = _executor(monkeypatch, client)

    opened = _open(ex)
    closed = _close(ex)

    assert not opened.ok and opened.error.startswith("okx_account_mode_spot_only")
    assert client.leverage_calls == []
    assert closed.ok and len(client.orders) == 1


def test_order_uses_the_exchange_formats(monkeypatch):
    client = _Client(hedged=True)
    client.POSITION_SIDE_PARAM = "position_side"
    res = _open(_executor(monkeypatch, client))

    assert res.client_order_id == "idtrend_open"
    assert client.orders[0]["params"]["position_side"] == "short"
    assert "positionSide" not in client.orders[0]["params"]


def test_spot_order_skips_margin_and_position_mode(monkeypatch):
    client = _Client()
    ex = _executor(monkeypatch, client)

    res = ex.place_market("XRP/USDT", "buy", 150.0, market_type="spot", reference_price=1.4,
                          purpose="trend_open", margin_mode=None, leverage=1)

    assert res.ok
    assert client.leverage_calls == [] and client.mode_calls == 0
    assert client.orders[0]["params"] == {"clientOrderId": res.client_order_id}


# ── размер позиции на бирже: только режим маржи сделки ─────────────────────────
def test_exchange_position_counts_only_the_trade_margin_mode(monkeypatch):
    monkeypatch.setattr(settings, "LIVE_MARGIN_MODE", "cross")
    ex = LiveExecutor.__new__(LiveExecutor)
    ex.client = SimpleNamespace(
        contract_size=lambda _s: 100.0,
        fetch_positions=lambda: [
            {"symbol": SYMBOL, "side": "short", "contracts": 1.8, "marginMode": "isolated"},
            {"symbol": SYMBOL, "side": "short", "contracts": 5.0, "marginMode": "cross"},
        ],
    )

    assert ex.exchange_position_base(SYMBOL, "short", "swap", "isolated") == pytest.approx(180.0)
    assert ex.exchange_position_base(SYMBOL, "short", "swap", None) == pytest.approx(500.0)


def test_engine_asks_for_the_position_in_the_route_margin_mode(monkeypatch):
    from services import live_executor as live_module
    from services.execution_engine import ExecutionEngine
    from services.market_routing import TradeRoute

    seen = {}

    def _fake(symbol, side, market_type, margin_mode=None):
        seen.update(symbol=symbol, side=side, market_type=market_type, margin_mode=margin_mode)
        return 150.0

    monkeypatch.setattr(live_module.LIVE_EXECUTOR, "exchange_position_base", _fake)
    route = TradeRoute(market_type="swap", exchange_symbol=SYMBOL, base_symbol="XRP/USDT",
                       side="short", leverage=1, margin_mode="isolated", reason="test")

    assert ExecutionEngine._exchange_position_base(route, SimpleNamespace(side="SHORT")) == 150.0
    assert seen == {"symbol": SYMBOL, "side": "short", "market_type": "swap", "margin_mode": "isolated"}


def test_reconcile_finds_an_htx_order_whose_id_came_back_as_a_number(monkeypatch):
    client_id = HTXClient.make_client_order_id("trend_open")
    ex = LiveExecutor.__new__(LiveExecutor)
    ex.client = SimpleNamespace(
        fetch_open_orders=lambda _s: [{"id": "9", "clientOrderId": None,
                                       "info": {"client_order_id": int(client_id)}}],
        fetch_closed_orders=lambda _s, limit=20: [],
    )

    assert ex._find_by_client_id(SYMBOL, client_id)["id"] == "9"
