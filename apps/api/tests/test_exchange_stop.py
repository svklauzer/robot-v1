"""Стоп-лосс на бирже для live-позиций (#exchange-stop-2026-09-16).

Аудит 16.09 перед live, пункт 2: стопы робота только программные — рестарт,
деплой или OOM оставляли live-позицию на бирже без защиты. Здесь проверяется:

  • какие запросы получает биржа (настоящий ccxt без сети);
  • сверка: постановка, перенос вслед за программным стопом, частичное
    закрытие, лишние стопы, отказы, снятие при закрытии;
  • что в off ничего не происходит, а dry_run проходит ту же сверку без биржи.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import ccxt
import pytest

from core.config import settings
from services import exchange_stop as es
from services.exchange_stop import ExchangeStopService
from services.execution_engine import ExecutionEngine
from services.htx_client import HTXClient
from services.live_executor import LiveExecutor
from services.okx_client import OKXClient
from tests.test_live_margin_mode import _swap_market

SYMBOL = "XRP/USDT:USDT"
ROUTING = {"market_type": "swap", "exchange_symbol": SYMBOL, "base_symbol": "XRP/USDT",
           "side": "short", "leverage": 1, "margin_mode": "isolated"}


# ── запросы бирж ───────────────────────────────────────────────────────────────
@pytest.fixture
def direct_retry(monkeypatch):
    monkeypatch.setattr(OKXClient, "_retry", lambda self, fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(HTXClient, "_retry", lambda self, fn, *a, **k: fn(*a, **k))


def _stop_params(exchange_client, hedged=False):
    return LiveExecutor.order_params(
        client_id=exchange_client.make_client_order_id("slexch"), market_type="swap", side="buy",
        reduce_only=True, margin_mode="isolated", hedged=hedged,
        position_side_key=exchange_client.POSITION_SIDE_PARAM,
    )


def _okx(direct_retry):
    ex = ccxt.okx({"apiKey": "k", "secret": "s", "password": "p"})
    ex.set_markets([_swap_market("XRP-USDT-SWAP", 0.01)])
    client = OKXClient.__new__(OKXClient)
    client.exchange = ex
    return client, ex


def _htx(direct_retry):
    ex = ccxt.htx({"apiKey": "k", "secret": "s"})
    ex.set_markets([_swap_market("XRP-USDT", 1)])
    client = HTXClient.__new__(HTXClient)
    client.exchange = ex
    return client, ex


def test_okx_stop_is_a_reduce_only_market_conditional(direct_retry):
    client, ex = _okx(direct_retry)
    sent = []
    ex.privatePostTradeOrderAlgo = lambda req: sent.append(req) or {
        "code": "0", "data": [{"algoId": "777", "sCode": "0", "sMsg": ""}]}

    order = client.create_stop_loss_order(SYMBOL, "buy", 1.5, 1.5075, _stop_params(client))

    assert order["id"] == "777"
    body = sent[0]
    assert body["ordType"] == "conditional" and body["slTriggerPx"] == "1.5075"
    assert body["slOrdPx"] == "-1", "стоп обязан исполняться по рынку"
    assert body["reduceOnly"] is True and body["tdMode"] == "isolated"
    assert body["sz"] == "1.5" and body["side"] == "buy"


def test_okx_stop_in_long_short_mode_names_the_position(direct_retry):
    client, ex = _okx(direct_retry)
    sent = []
    ex.privatePostTradeOrderAlgo = lambda req: sent.append(req) or {
        "code": "0", "data": [{"algoId": "1", "sCode": "0"}]}

    client.create_stop_loss_order(SYMBOL, "buy", 1.5, 1.5075, _stop_params(client, hedged=True))

    assert sent[0]["posSide"] == "short" and "reduceOnly" not in sent[0]


def test_okx_lists_and_cancels_algo_stops(direct_retry):
    client, ex = _okx(direct_retry)
    calls = {}
    ex.privateGetTradeOrdersAlgoPending = lambda req: calls.setdefault("fetch", req) and {
        "code": "0", "data": [{"algoId": "777", "instId": "XRP-USDT-SWAP", "ordType": "conditional",
                               "clOrdId": "rbtslexch0a1b2c3d", "side": "buy", "sz": "1.5", "slTriggerPx": "1.5075", "slOrdPx": "-1",
                               "state": "live", "reduceOnly": "true", "cTime": "1"}]}
    ex.privatePostTradeCancelAlgos = lambda req: calls.setdefault("cancel", req) and {
        "code": "0", "data": [{"algoId": "777", "sCode": "0"}]}

    stops = [LiveExecutor._normalize_stop(o) for o in client.fetch_open_stop_orders(SYMBOL)]
    client.cancel_stop_order("777", SYMBOL)

    assert calls["fetch"] == {"instId": "XRP-USDT-SWAP", "ordType": "conditional"}
    assert stops == [{"order_id": "777", "client_order_id": "rbtslexch0a1b2c3d", "side": "buy",
                      "trigger": 1.5075, "contracts": 1.5}]
    assert calls["cancel"] == [{"algoId": "777", "instId": "XRP-USDT-SWAP"}]


def test_htx_stop_goes_to_v5_algo_as_sl(direct_retry):
    client, ex = _htx(direct_retry)
    sent = []
    ex.contractPrivatePostV5AlgoOrder = lambda req: sent.append(req) or {
        "code": 200, "message": "Success", "data": [{"algo_id": "15", "algo_client_order_id": None}]}

    params = _stop_params(client)
    order = client.create_stop_loss_order(SYMBOL, "buy", 2, 1.5075, params)

    assert order["id"] == "15"
    body = sent[0]
    assert body["type"] == "sl" and body["sl_trigger_price"] == "1.5075"
    assert "sl_order_price" not in body, "цена ордера превратила бы стоп в лимитный"
    assert body["margin_mode"] == "isolated" and body["volume"] == "2"
    assert str(body["algo_client_order_id"]) == params["clientOrderId"]
    assert not {"reduceOnly", "clientOrderId", "marginMode", "stopLossPrice"} & set(body)


def test_htx_lists_and_cancels_sl_stops(direct_retry):
    client, ex = _htx(direct_retry)
    calls = {}
    ex.contractPrivateGetV5AlgoOrderOpens = lambda req: calls.setdefault("fetch", req) and {
        "code": 200, "data": [{"algo_id": "15", "algo_client_order_id": "770123456789012",
                               "contract_code": "XRP-USDT", "side": "buy", "type": "sl",
                               "volume": "2", "sl_trigger_price": "1.5075", "state": "new",
                               "created_time": "1"}]}
    ex.contractPrivatePostV5AlgoCancelOrders = lambda req: calls.setdefault("cancel", req) and {
        "code": 200, "data": [{"algo_id": "15"}]}

    stops = [LiveExecutor._normalize_stop(o) for o in client.fetch_open_stop_orders(SYMBOL)]
    client.cancel_stop_order("15", SYMBOL)

    assert calls["fetch"] == {"contract_code": "XRP-USDT", "type": "sl"}
    assert stops == [{"order_id": "15", "client_order_id": "770123456789012", "side": "buy",
                      "trigger": 1.5075, "contracts": 2.0}]
    assert calls["cancel"] == [{"contract_code": "XRP-USDT", "algo_id": "15"}]


# ── сверка ─────────────────────────────────────────────────────────────────────
class _Exchange:
    """Биржа-двойник: книга стопов и журнал вызовов в порядке исполнения."""

    POSITION_SIDE_PARAM = "positionSide"

    def __init__(self):
        self.stops: dict[str, dict] = {}
        self.log: list[tuple] = []
        self.place_error = None
        self.fetch_error = None
        self.cancel_error = None
        self._n = 0

    def contract_size(self, _symbol):
        return 100.0

    def amount_to_precision(self, _symbol, amount):
        return float(amount)

    def price_to_precision(self, _symbol, price):
        return round(float(price), 4)

    def fetch_derivatives_account(self):
        return {"hedged": False, "blocker": None}

    @staticmethod
    def make_client_order_id(purpose):
        from services.robot_orders import alnum_client_id

        return alnum_client_id(purpose)

    def create_stop_loss_order(self, symbol, side, amount, trigger_price, params=None):
        self.log.append(("place", trigger_price, amount))
        if self.place_error:
            raise self.place_error
        self._n += 1
        order_id = f"s{self._n}"
        self.stops[order_id] = {"id": order_id, "symbol": symbol, "side": side, "amount": amount,
                                "stopLossPrice": trigger_price, "params": dict(params or {})}
        return {"id": order_id}

    def fetch_open_stop_orders(self, symbol):
        self.log.append(("fetch",))
        if self.fetch_error:
            raise self.fetch_error
        return [{**o, "clientOrderId": o.get("clientOrderId") or (o.get("params") or {}).get("clientOrderId")}
                for o in self.stops.values() if o["symbol"] == symbol]

    def cancel_stop_order(self, order_id, symbol):
        self.log.append(("cancel", order_id))
        if self.cancel_error:
            raise self.cancel_error
        self.stops.pop(order_id, None)
        return {"id": order_id}


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    monkeypatch.setattr(settings, "LIVE_EXCHANGE_STOP_ENABLED", True)
    monkeypatch.setattr(settings, "LIVE_EXCHANGE_STOP_BUFFER_PCT", 0.5)
    monkeypatch.setattr(settings, "LIVE_EXCHANGE_STOP_MIN_MOVE_PCT", 0.1)
    monkeypatch.setattr(settings, "LIVE_EXCHANGE_STOP_VERIFY_SEC", 60.0)
    monkeypatch.setattr(settings, "LIVE_EXCHANGE_STOP_HALT_AFTER_FAILURES", 3)

    exchange = _Exchange()
    executor = LiveExecutor.__new__(LiveExecutor)
    executor.client = exchange
    executor._leverage_set = set()
    executor._bal_cache = {}
    executor._account_state = None

    signal = SimpleNamespace(id=7, bot_id=1, symbol="XRP/USDT", side="short", stop_price=1.50,
                             plan_json={"routing": dict(ROUTING)})
    position = SimpleNamespace(symbol="XRP/USDT", side="short", qty=150.0, status="open", mark_price=1.42)
    alerts, kills = [], []

    async def alert(title, body):
        alerts.append((title, body))

    from services import live_safety

    monkeypatch.setattr(live_safety.LiveSafetyService, "set_kill_switch",
                        lambda self, db, bot, enabled, reason: kills.append(reason))
    db = SimpleNamespace(query=lambda *_a, **_k: SimpleNamespace(
        filter=lambda *_a, **_k: SimpleNamespace(first=lambda: SimpleNamespace(id=1), all=lambda: [])),
        flush=lambda: None)
    return SimpleNamespace(service=ExchangeStopService(executor), exchange=exchange, executor=executor,
                           signal=signal, position=position, alert=alert, alerts=alerts, kills=kills, db=db)


async def _sync(w, price=1.42):
    return await w.service.sync(w.db, w.signal, w.position, price=price, alert=w.alert)


def _state(w):
    return w.signal.plan_json["exchange_stop"]


@pytest.mark.anyio
async def test_places_a_stop_beyond_the_software_stop(live):
    out = await _sync(live)

    assert out["action"] == "placed"
    (stop,) = live.exchange.stops.values()
    # Шорт: стоп выше входа, биржевой ещё на 0.5% выше программного 1.50.
    assert stop["side"] == "buy" and stop["stopLossPrice"] == 1.5075
    assert stop["amount"] == pytest.approx(1.5), "150 XRP = 1.5 контракта по 100"
    assert stop["params"]["reduceOnly"] is True and stop["params"]["marginMode"] == "isolated"
    assert _state(live)["order_id"] == out["order_id"] and _state(live)["software_stop"] == 1.50


@pytest.mark.anyio
async def test_long_stop_sits_below(live):
    live.signal.side = live.position.side = "long"
    live.signal.stop_price = 1.30
    await _sync(live, price=1.42)

    (stop,) = live.exchange.stops.values()
    assert stop["side"] == "sell" and stop["stopLossPrice"] == pytest.approx(1.2935)


@pytest.mark.anyio
async def test_unchanged_stop_is_not_rechecked_every_pass(live):
    await _sync(live)
    live.exchange.log.clear()

    out = await _sync(live)

    assert out["action"] == "unchanged" and live.exchange.log == []


@pytest.mark.anyio
async def test_stop_is_verified_on_the_exchange_after_the_window(live):
    await _sync(live)
    _state(live)["checked_ts"] = time.time() - 120
    live.exchange.log.clear()

    out = await _sync(live)

    assert out["action"] == "verified"
    assert live.exchange.log == [("fetch",)]


@pytest.mark.anyio
async def test_moved_software_stop_moves_the_exchange_stop_new_first(live):
    first = (await _sync(live))["order_id"]
    live.signal.stop_price = 1.42                       # безубыток
    live.exchange.log.clear()

    out = await _sync(live, price=1.38)

    assert out["action"] == "replaced"
    assert [e[0] for e in live.exchange.log] == ["fetch", "place", "cancel"], \
        "старый стоп снят раньше, чем встал новый — позиция без защиты между вызовами"
    assert list(live.exchange.stops) == [out["order_id"]] and first not in live.exchange.stops
    assert _state(live)["trigger"] == pytest.approx(1.4271)


@pytest.mark.anyio
async def test_tiny_trail_steps_do_not_churn_orders(live):
    await _sync(live)
    live.signal.stop_price = 1.4995                     # сдвиг 0.03% < 0.1%
    live.exchange.log.clear()

    out = await _sync(live)

    assert out["action"] == "unchanged" and live.exchange.log == []


@pytest.mark.anyio
async def test_partial_close_resizes_the_stop(live):
    await _sync(live)
    live.position.qty = 75.0

    out = await _sync(live)

    assert out["action"] == "replaced"
    (stop,) = live.exchange.stops.values()
    assert stop["amount"] == pytest.approx(0.75)


@pytest.mark.anyio
async def test_stray_robot_stops_on_the_symbol_are_cancelled(live):
    await _sync(live)
    live.exchange.stops["old"] = {"id": "old", "symbol": SYMBOL, "side": "buy", "amount": 3.0,
                                  "stopLossPrice": 1.60, "clientOrderId": "rbtslexchdeadbeef"}
    _state(live)["checked_ts"] = 0

    out = await _sync(live)

    assert out["action"] == "verified" and out["cancelled"] == 1
    assert "old" not in live.exchange.stops and len(live.exchange.stops) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("manual_client_id", [None, "", "e847386590ce4dBC67a0fe4c", "123456"])
async def test_manual_stops_are_never_touched(live, manual_client_id):
    """(#manual-orders-2026-09-16) Владелец торгует руками на той же бирже: его
    стоп по тому же символу и в ту же сторону робот не снимает ни при сверке, ни
    при закрытии, и не принимает за свой."""
    live.exchange.stops["manual"] = {"id": "manual", "symbol": SYMBOL, "side": "buy", "amount": 1.5,
                                     "stopLossPrice": 1.5075, "clientOrderId": manual_client_id}

    out = await _sync(live)
    assert out["action"] == "placed", "ручной стоп с той же ценой принят за стоп робота"
    _state(live)["checked_ts"] = 0
    await _sync(live)
    await live.service.cancel_all(live.db, live.signal, alert=live.alert)

    assert list(live.exchange.stops) == ["manual"]
    assert ("cancel", "manual") not in live.exchange.log


@pytest.mark.anyio
async def test_other_open_trades_stops_are_kept(live, monkeypatch):
    live.exchange.stops["neighbour"] = {"id": "neighbour", "symbol": SYMBOL, "side": "buy",
                                        "amount": 1.0, "stopLossPrice": 1.70,
                                        "clientOrderId": "rbtslexchneighbour"}
    monkeypatch.setattr(es, "_foreign_order_ids", lambda db, signal: {"neighbour"})

    await _sync(live)
    await live.service.cancel_all(live.db, live.signal, alert=live.alert)

    assert list(live.exchange.stops) == ["neighbour"]


@pytest.mark.anyio
async def test_restart_adopts_the_stop_already_on_the_exchange(live):
    """После рестарта записи может не быть — стоп на бирже принимается, второй не ставится."""
    await _sync(live)
    live.signal.plan_json.pop("exchange_stop")
    live.exchange.log.clear()

    out = await _sync(live)

    assert out["action"] == "verified"
    assert [e[0] for e in live.exchange.log] == ["fetch"]
    assert len(live.exchange.stops) == 1


@pytest.mark.anyio
async def test_failed_placement_keeps_the_old_stop_and_escalates(live):
    await _sync(live)
    old = set(live.exchange.stops)
    live.signal.stop_price = 1.42
    live.exchange.place_error = RuntimeError("51000 slTriggerPx error")

    for attempt in range(1, 4):
        out = await _sync(live, price=1.38)
        assert out["action"] == "place_failed"
        assert set(live.exchange.stops) == old, "прежний стоп снят, хотя новый не встал"
        assert _state(live)["failures"] == attempt

    assert len(live.alerts) == 2, "алерт на первом отказе и при включении kill switch"
    assert len(live.kills) == 1

    live.exchange.place_error = None
    out = await _sync(live, price=1.38)
    assert out["action"] == "replaced" and _state(live)["failures"] == 0


@pytest.mark.anyio
async def test_exchange_unreachable_changes_nothing(live):
    live.exchange.fetch_error = RuntimeError("timeout")
    out = await _sync(live)
    assert out["action"] == "fetch_failed" and live.exchange.stops == {}
    assert "exchange_stop" not in live.signal.plan_json


@pytest.mark.anyio
async def test_price_already_beyond_the_trigger_is_left_to_the_software_stop(live):
    out = await _sync(live, price=1.52)
    assert out["action"] == "price_beyond_trigger" and live.exchange.log == []


@pytest.mark.anyio
async def test_closed_position_cancels_the_stop(live):
    await _sync(live)
    live.position.status = "closed"

    out = await _sync(live)

    assert out["action"] == "cancelled" and live.exchange.stops == {}
    assert _state(live)["order_id"] is None


@pytest.mark.anyio
async def test_failed_cancel_tells_the_owner(live):
    await _sync(live)
    live.exchange.cancel_error = RuntimeError("timeout")

    out = await live.service.cancel_all(live.db, live.signal, alert=live.alert, reason="stop_loss")

    assert out["action"] == "cancel_failed"
    assert live.alerts and "НЕ СНЯТ" in live.alerts[-1][0]
    assert _state(live)["cancel_failed"] is True and _state(live)["order_id"]


@pytest.mark.anyio
async def test_already_executed_stop_counts_as_cancelled(live):
    await _sync(live)
    live.exchange.cancel_error = ccxt.OrderNotFound("51400 order does not exist")

    out = await live.service.cancel_all(live.db, live.signal, alert=live.alert)

    assert out["action"] == "cancelled" and live.alerts == []


@pytest.mark.anyio
async def test_spot_route_gets_no_exchange_stop(live):
    live.signal.plan_json = {"routing": {**ROUTING, "market_type": "spot", "exchange_symbol": "XRP/USDT",
                                         "side": "long"}}
    live.signal.side = live.position.side = "long"
    live.signal.stop_price = 1.30
    out = await _sync(live)
    assert out["action"] == "not_applicable" and live.exchange.log == []


@pytest.mark.anyio
async def test_off_does_nothing(live, monkeypatch):
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "off"))
    assert (await _sync(live))["action"] == "inactive"
    assert (await live.service.cancel_all(live.db, live.signal))["action"] == "inactive"
    assert live.exchange.log == [] and "exchange_stop" not in live.signal.plan_json


# ── dry_run: та же сверка без биржи (#dry-run-parity-2026-09-16) ───────────────
@pytest.fixture
def dry(live, monkeypatch):
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "dry_run"))
    events = []
    monkeypatch.setattr(es, "log_event", lambda _l, _lvl, event, **kw: events.append((event, kw)))
    live.events = events
    return live


def _dry_events(w, action):
    return [kw for event, kw in w.events if event == "live_dry_run_exchange_stop" and kw.get("action") == action]


@pytest.mark.anyio
async def test_dry_run_walks_the_live_path_without_the_exchange(dry):
    out = await _sync(dry)

    assert out["action"] == "placed" and out["mode"] == "dry_run"
    assert dry.exchange.log == [], "в dry_run к бирже не обращаемся"
    state = _state(dry)
    assert state["mode"] == "dry_run" and state["order_id"].startswith("dry")
    assert state["trigger"] == 1.5075 and state["contracts"] == pytest.approx(1.5)
    (placed,) = _dry_events(dry, "place")
    assert placed["side"] == "buy" and placed["params"]["marginMode"] == "isolated"
    assert placed["params"]["reduceOnly"] is True


@pytest.mark.anyio
async def test_dry_run_follows_the_software_stop_and_partial_close(dry):
    first = (await _sync(dry))["order_id"]
    dry.signal.stop_price = 1.42
    moved = await _sync(dry, price=1.38)
    dry.position.qty = 75.0
    resized = await _sync(dry, price=1.38)

    assert moved["action"] == "replaced" and resized["action"] == "replaced"
    assert [kw["order_id"] for kw in _dry_events(dry, "cancel")] == [first, moved["order_id"]]
    assert _state(dry)["contracts"] == pytest.approx(0.75)
    assert dry.exchange.log == []


@pytest.mark.anyio
async def test_dry_run_does_not_rewrite_an_unchanged_stop(dry):
    await _sync(dry)
    _state(dry)["checked_ts"] = 0          # в live это запустило бы сверку с биржей
    before = dict(_state(dry))

    out = await _sync(dry)

    assert out["action"] == "unchanged" and _state(dry) == before


@pytest.mark.anyio
async def test_dry_run_close_cancels_the_simulated_stop(dry):
    order_id = (await _sync(dry))["order_id"]

    out = await dry.service.cancel_all(dry.db, dry.signal, reason="stop_loss")

    assert out["action"] == "cancelled" and _state(dry)["order_id"] is None
    assert [kw["order_id"] for kw in _dry_events(dry, "cancel")] == [order_id]


@pytest.mark.anyio
async def test_switching_to_live_replaces_the_simulated_stop_with_a_real_one(dry, monkeypatch):
    await _sync(dry)
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    _state(dry)["checked_ts"] = 0

    out = await _sync(dry)

    assert out["action"] == "placed" and out["mode"] == "live"
    assert list(dry.exchange.stops) == [out["order_id"]]
    assert _state(dry)["mode"] == "live"


@pytest.mark.anyio
async def test_unknown_contract_size_warns_once(dry, monkeypatch):
    monkeypatch.setattr(_Exchange, "contract_size", lambda self, _s: None)
    es._UNIT_WARNED.clear()

    for _ in range(3):
        assert (await _sync(dry))["action"] == "unit_unresolved"

    assert [e for e, _ in dry.events].count("exchange_stop_unit_unresolved") == 1


@pytest.mark.anyio
async def test_switch_off_disables_it(live, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_EXCHANGE_STOP_ENABLED", False)
    assert (await _sync(live))["action"] == "inactive"


@pytest.mark.anyio
async def test_a_crash_inside_never_reaches_the_lifecycle(live, monkeypatch):
    def boom(*_a, **_k):
        raise KeyError("routing")

    monkeypatch.setattr(ExchangeStopService, "desired", boom)
    out = await _sync(live)
    assert out["action"] == "error"


# ── сопровождение и закрытие ───────────────────────────────────────────────────
@pytest.mark.anyio
async def test_lifecycle_hook_does_not_touch_the_db_when_off(monkeypatch):
    from services.signal_lifecycle import SignalLifecycleManager

    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "off"))
    lifecycle = SignalLifecycleManager.__new__(SignalLifecycleManager)
    calls = []
    monkeypatch.setattr(SignalLifecycleManager, "_get_open_position_for_signal",
                        lambda self, db, signal: calls.append(signal.id))

    await lifecycle._sync_exchange_stop(SimpleNamespace(), SimpleNamespace(id=1, plan_json={}))

    assert calls == [], "запрос позиции при LIVE_EXECUTION_MODE=off"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["dry_run", "live"])
async def test_lifecycle_hook_syncs_in_dry_run_and_live(monkeypatch, mode):
    from services.signal_lifecycle import SignalLifecycleManager

    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: mode))
    lifecycle = SignalLifecycleManager.__new__(SignalLifecycleManager)
    lifecycle.router = SimpleNamespace(owner_alert=None)
    position = SimpleNamespace(status="open", mark_price=1.42)
    monkeypatch.setattr(SignalLifecycleManager, "_get_open_position_for_signal",
                        lambda self, db, signal: position)
    seen = []

    async def fake_sync(self, db, signal, pos, *, price=None, alert=None):
        seen.append((pos, price))
        return {"action": "placed"}

    monkeypatch.setattr(ExchangeStopService, "sync", fake_sync)
    monkeypatch.setattr(ExchangeStopService, "__init__", lambda self, executor=None: None)

    await lifecycle._sync_exchange_stop(SimpleNamespace(), SimpleNamespace(id=1, plan_json={}))

    assert seen == [(position, 1.42)]


def test_flat_position_past_the_trigger_books_the_trigger_price():
    signal = SimpleNamespace(plan_json={"exchange_stop": {"order_id": "s1", "trigger": 1.5075}})
    short = SimpleNamespace(side="short")

    assert ExecutionEngine._exchange_stop_exit(signal, short, 1.53) == 1.5075
    assert ExecutionEngine._exchange_stop_exit(signal, short, 1.45) is None, \
        "цена не дошла до стопа — позицию закрыл не он"
    assert ExecutionEngine._exchange_stop_exit(SimpleNamespace(plan_json={}), short, 1.53) is None


def test_settings_are_pinned_in_the_blueprint():
    from pathlib import Path

    from services.config_inspector import _PINNED_ON_PURPOSE

    blueprint = (Path(__file__).resolve().parents[3] / "render.yaml").read_text(encoding="utf-8")
    for key in ("LIVE_EXCHANGE_STOP_ENABLED", "LIVE_EXCHANGE_STOP_BUFFER_PCT"):
        assert f"key: {key}" in blueprint
        assert key in _PINNED_ON_PURPOSE
        assert hasattr(settings, key)


# ── закрытие в движке ──────────────────────────────────────────────────────────
from tests.test_live_close_safety import world  # noqa: E402,F401 — фикстура live-закрытия


@pytest.fixture
def cancels(monkeypatch):
    calls = []

    async def fake_cancel(self, signal, route, reason):
        calls.append(reason)

    monkeypatch.setattr(ExecutionEngine, "_cancel_exchange_stops", fake_cancel)
    return calls


@pytest.mark.anyio
async def test_full_close_cancels_the_exchange_stop_after_the_exchange_closed(world, cancels):
    out = await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="stop_loss")
    assert out["status"] == "closed"
    assert cancels == ["stop_loss"]


@pytest.mark.anyio
async def test_refused_close_keeps_the_exchange_stop(world, cancels):
    world.state["reply"] = {"mode": "live", "ok": False, "status": "error", "error": "timeout"}
    out = await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="stop_loss")
    assert out["status"] == "live_close_failed"
    assert cancels == [], "позиция на бирже осталась — страховку снимать нельзя"


@pytest.mark.anyio
async def test_flat_after_the_exchange_stop_fired_books_the_trigger(world, cancels):
    world.signal.plan_json["exchange_stop"] = {"order_id": "s1", "trigger": 1.5075}
    world.state["exchange_qty"] = 0.0

    out = await world.engine.close_paper_position(world.signal, exit_price=1.53, reason="stop_loss")

    assert out["status"] == "closed" and out["exit_price"] == 1.5075
    assert cancels == ["stop_loss"]
    assert world.sent == [], "позиции на бирже нет — ордер не отправляется"


def test_signal_card_reads_the_fields_the_service_writes():
    """Фронт показывает стоп на бирже из plan_json.exchange_stop — те же ключи,
    что пишет ExchangeStopService._record/_on_place_failed."""
    from pathlib import Path
    import inspect

    page = (Path(__file__).resolve().parents[2] / "web" / "app" / "signals" / "page.tsx").read_text(encoding="utf-8")
    assert "exchangeStop={plan.exchange_stop}" in page
    component = page.split("function StopValue", 1)[1].split("\nfunction ", 1)[0]
    source = inspect.getsource(es)
    for key in ("order_id", "trigger", "failures", "mode"):
        assert f"exchangeStop?.{key}" in component or f"exchangeStop.{key}" in component, key
        assert f'"{key}"' in source, key
    assert es.STATE_KEY == "exchange_stop"
    assert 'exchangeStop?.mode === "dry_run"' in component
