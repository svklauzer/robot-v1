"""dry_run собирает ордер так же, как live (#dry-run-parity-2026-09-16).

Аудит 16.09 перед live, пункт 7: dry_run выходил ДО перевода объёма в
контракты, режима маржи, параметров и плеча. В paper не было видно ни объёма
в контрактах, ни marginMode, ни отказа «меньше одного контракта» — всё это
впервые случилось бы на живой бирже. Теперь обе ветки собирают ордер одной
функцией, а бумага получает прежний результат.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import live_executor as live_module
from services.live_executor import LiveExecutor
from tests.test_live_margin_mode import SYMBOL, _Client


def _executor(monkeypatch, mode: str, client=None) -> tuple[LiveExecutor, _Client, list]:
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: mode))
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 0.0)
    monkeypatch.setattr(settings, "LIVE_SET_LEVERAGE", True)
    monkeypatch.setattr(settings, "LIVE_MARGIN_MODE", "cross")
    events: list = []
    monkeypatch.setattr(live_module, "log_event",
                        lambda _l, level, event, **kw: events.append((event, level, kw)))
    client = client or _Client()
    ex = LiveExecutor.__new__(LiveExecutor)
    ex.client = client
    ex._leverage_set = set()
    ex._bal_cache = {}
    ex._account_state = None
    return ex, client, events


def _dry_order(events) -> dict:
    (kw,) = [kw for event, _lvl, kw in events if event == "live_dry_run_order"]
    return kw


def _open(ex, **kw):
    kw.setdefault("margin_mode", "isolated")
    kw.setdefault("leverage", 1)
    return ex.place_market(SYMBOL, "sell", kw.pop("amount", 150.0), market_type="swap",
                           reference_price=1.4, purpose="trend_open", **kw)


def test_dry_run_logs_the_order_live_would_send(monkeypatch):
    ex, client, events = _executor(monkeypatch, "dry_run")

    res = _open(ex)

    order = _dry_order(events)
    assert order["exchange_amount"] == pytest.approx(1.5) and order["submitted_unit"] == "contracts"
    assert order["contract_size"] == 100.0
    assert order["params"] == {"clientOrderId": res.client_order_id, "marginMode": "isolated"}
    assert order["leverage"] == 1 and order["margin_mode"] == "isolated"
    assert order["would_reject"] is None
    assert client.orders == [] and client.leverage_calls == []
    assert client.mode_calls == 0, "режим счёта — приватный запрос, в dry_run его нет"


def test_paper_result_is_unchanged(monkeypatch):
    ex, _client, _events = _executor(monkeypatch, "dry_run")

    res = _open(ex)

    assert res.ok is True and res.sent is False and res.status == "dry_run"
    assert res.filled_qty == pytest.approx(150.0) and res.avg_price == pytest.approx(1.4)
    assert res.error is None


def test_close_in_dry_run_is_reduce_only_without_leverage(monkeypatch):
    ex, _client, events = _executor(monkeypatch, "dry_run")

    ex.place_market(SYMBOL, "buy", 150.0, market_type="swap", reduce_only=True,
                    margin_mode="isolated", reference_price=1.4, purpose="trend_close")

    order = _dry_order(events)
    assert order["params"]["reduceOnly"] is True and order["leverage"] is None


@pytest.mark.parametrize("kwargs, reason", [
    ({"amount": 0.4}, "amount_below_min_contract"),        # 0.004 контракта при шаге 1
    ({"margin_mode": "portfolio"}, "margin_mode_invalid:portfolio"),
])
def test_dry_run_shows_what_live_would_refuse(monkeypatch, kwargs, reason):
    client = _Client()
    client.amount_to_precision = lambda _s, a: float(int(float(a) / 100.0) * 100.0)
    ex, _client, events = _executor(monkeypatch, "dry_run", client)

    res = _open(ex, **kwargs)

    assert res.ok is True, "бумага не должна зависеть от отказа, которого не было"
    assert res.error == f"would_reject:{reason}"
    ((event, level, kw),) = [e for e in events if e[0] == "live_dry_run_order"]
    assert kw["would_reject"] == reason and level == live_module.logging.WARNING


def test_dry_run_flags_the_notional_cap(monkeypatch):
    ex, _client, events = _executor(monkeypatch, "dry_run")
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 100.0)

    res = _open(ex)                                           # 150 × 1.4 = 210 > 100

    assert res.ok is True and res.error == "would_reject:notional>100.0"


def test_dry_run_and_live_build_the_same_order(monkeypatch):
    dry, _c, events = _executor(monkeypatch, "dry_run")
    dry_res = _open(dry)
    dry_order = _dry_order(events)

    live, client, _e = _executor(monkeypatch, "live")
    live_res = _open(live)
    sent = client.orders[0]

    assert sent["amount"] == pytest.approx(dry_order["exchange_amount"])
    strip = lambda p, cid: {k: v for k, v in p.items() if v != cid}  # noqa: E731
    assert strip(sent["params"], live_res.client_order_id) == strip(dry_order["params"], dry_res.client_order_id)
    assert client.leverage_calls == [(SYMBOL, dry_order["leverage"], "isolated", None)]


def test_prepare_failure_never_breaks_paper(monkeypatch):
    ex, _client, events = _executor(monkeypatch, "dry_run")
    del ex.client

    res = _open(ex)

    assert res.ok is True and res.error.startswith("would_reject:prepare_failed:")
    assert _dry_order(events)["account_mode"] == "not_checked_in_dry_run"
