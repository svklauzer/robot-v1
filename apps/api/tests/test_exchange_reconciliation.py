"""Сверка робота с биржей (#exchange-reconciliation-2026-09-16).

Только live, только чтение, только объекты робота: ручные ордера и позиции
владельца на тех же биржах (#manual-orders-2026-09-16) не расхождение.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
from core.db import Base
from models.bot import Bot
from models.order import Order
from models.position import Position
from models.signal import Signal
from models.user import User
from services import exchange_reconciliation as recon
from services.exchange_reconciliation import ExchangeReconciliationService, mismatch_key
from services.live_executor import LiveExecutor

XRP = "XRP/USDT:USDT"
BTC = "BTC/USDT:USDT"
ROUTING = {"market_type": "swap", "exchange_symbol": XRP, "base_symbol": "XRP/USDT",
           "side": "short", "leverage": 1, "margin_mode": "isolated"}
ROBOT_STOP = "rbtslexch0001"


class _Exchange:
    def __init__(self, positions=None, orders=None, stops=None, error=None):
        self.positions = positions or []
        self.orders = orders or {}
        self.stops = stops or {}
        self.error = error
        self.calls: list = []

    def contract_size(self, _symbol):
        return 100.0

    def fetch_positions(self):
        self.calls.append("fetch_positions")
        if self.error:
            raise self.error
        return self.positions

    def fetch_open_orders(self, symbol=None):
        self.calls.append(("fetch_open_orders", symbol))
        return self.orders.get(symbol, [])

    def fetch_open_stop_orders(self, symbol):
        self.calls.append(("fetch_open_stop_orders", symbol))
        return self.stops.get(symbol, [])


def _position(symbol=XRP, side="short", contracts=1.5, margin="isolated"):
    return {"symbol": symbol, "side": side, "contracts": contracts, "contractSize": 100.0, "marginMode": margin}


def _stop(order_id=ROBOT_STOP, client_id=ROBOT_STOP, trigger=1.5075, contracts=1.5):
    return {"id": order_id, "clientOrderId": client_id, "side": "buy", "stopLossPrice": trigger, "amount": contracts}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    recon._LAST = None
    monkeypatch.setattr(settings, "EXCHANGE_RECONCILIATION_ENABLED", True)
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx")
    monkeypatch.setattr(settings, "OKX_SYMBOLS", "XRP/USDT,BTC/USDT")
    monkeypatch.setattr(settings, "TREND_MARGIN_MODE", "isolated")
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    yield
    recon._LAST = None


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__, Bot.__table__, Signal.__table__,
                                                  Order.__table__, Position.__table__])
    return sessionmaker(bind=engine)()


def _seed(db, *, qty=150.0, execution="live", stop_id=ROBOT_STOP):
    db.add_all([User(email="owner@example.com", password_hash="x"),
                Bot(user_id=1, name="Main Robot", status="running", mode="live")])
    db.flush()
    plan = {"routing": dict(ROUTING)}
    if execution:
        plan["execution"] = {"mode": execution}
    if stop_id:
        plan["exchange_stop"] = {"order_id": stop_id, "trigger": 1.5075}
    signal = Signal(bot_id=1, symbol="XRP/USDT", side="short", status="opened", exchange="okx",
                    entry_zone_json={"from": 1.42, "to": 1.43}, stop_price=1.50,
                    tp_json={"tp1": 1.38, "tp2": 1.34}, plan_json=plan)
    db.add(signal)
    db.flush()
    db.add(Position(bot_id=1, signal_id=signal.id, symbol="XRP/USDT", side="short", qty=qty,
                    entry_price=1.42, unrealized_pnl=0.0, status="open"))
    db.commit()
    return signal


def _run(exchange, db=None):
    executor = LiveExecutor.__new__(LiveExecutor)
    executor.client = exchange
    db = db or _db()
    return ExchangeReconciliationService(executor=executor).reconcile(db, bot_id=1)


def _types(items):
    return sorted(i["type"] for i in items)


def test_matching_book_and_exchange_is_ok():
    db = _db()
    _seed(db)
    result = _run(_Exchange(positions=[_position()], stops={XRP: [_stop()]}), db)

    assert result["status"] == "ok" and result["ok"] is True
    assert result["mismatches"] == [] and result["warnings"] == []
    assert result["counts"]["robot_live_positions"] == 1


def test_robot_position_gone_on_the_exchange_is_a_mismatch():
    db = _db()
    _seed(db)
    result = _run(_Exchange(positions=[_position(contracts=0.5)], stops={XRP: [_stop()]}), db)

    (item,) = result["mismatches"]
    assert item["type"] == "exchange_below_book"
    assert item["book_qty"] == 150.0 and item["exchange_qty"] == pytest.approx(50.0)
    assert result["ok"] is False


def test_manual_position_on_top_in_the_same_margin_mode_is_only_a_warning():
    db = _db()
    _seed(db)
    result = _run(_Exchange(positions=[_position(contracts=2.5)], stops={XRP: [_stop()]}), db)

    assert result["mismatches"] == []
    assert _types(result["warnings"]) == ["exchange_above_book"]


def test_manual_trading_in_another_margin_mode_is_invisible():
    db = _db()
    _seed(db)
    manual = [_position(contracts=7.0, margin="cross"), _position(BTC, "long", 3.0, "cross")]
    manual_orders = {XRP: [{"id": "m1", "clientOrderId": None, "side": "sell"}],
                     BTC: [{"id": "m2", "clientOrderId": "e847386590ce4dBC"}]}
    manual_stops = {XRP: [_stop(), _stop("m3", None, 1.6, 7.0)], BTC: [_stop("m4", "", 60000, 3.0)]}

    result = _run(_Exchange(positions=[_position()] + manual, orders=manual_orders, stops=manual_stops), db)

    assert result["mismatches"] == [] and result["warnings"] == []


def test_stale_robot_order_and_orphan_robot_stop_are_mismatches():
    db = _db()
    _seed(db)
    exchange = _Exchange(
        positions=[_position()],
        orders={XRP: [{"id": "o1", "clientOrderId": "rbttrendopen01", "side": "sell", "amount": 1.5}]},
        stops={XRP: [_stop()], BTC: [_stop("o2", "770123456789012", 60000, 0.3)]},
    )
    result = _run(exchange, db)

    assert _types(result["mismatches"]) == ["orphan_robot_stop", "stale_robot_order"]
    assert bool(result["blockers"]) == bool(settings.is_live_enabled), "в live расхождение — блокер готовности"


def test_untracked_position_in_the_robot_margin_mode_is_a_warning():
    db = _db()
    _seed(db)
    result = _run(_Exchange(positions=[_position(), _position(BTC, "long", 3.0, "isolated")],
                            stops={XRP: [_stop()]}), db)

    assert result["mismatches"] == []
    assert _types(result["warnings"]) == ["untracked_position_in_robot_margin_mode"]


def test_missing_and_stray_stops_are_warnings():
    db = _db()
    _seed(db)
    result = _run(_Exchange(positions=[_position()], stops={XRP: [_stop("x9", "rbtslexchstray")]}), db)

    assert result["mismatches"] == []
    assert _types(result["warnings"]) == ["exchange_stop_missing", "stray_robot_stop"]


def test_positions_not_opened_live_are_not_the_robots_on_the_exchange():
    db = _db()
    _seed(db, execution="paper")
    result = _run(_Exchange(), db)

    assert result["status"] == "ok" and result["counts"]["robot_live_positions"] == 0


def test_exchange_failure_is_degraded_not_a_crash():
    db = _db()
    _seed(db)
    result = _run(_Exchange(error=TimeoutError("okx down")), db)

    assert result["status"] == "degraded" and result["ok"] is False
    assert "TimeoutError" in result["error"]


def test_paper_and_dry_run_never_call_the_exchange(monkeypatch):
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "dry_run"))
    exchange = _Exchange()
    result = _run(exchange)

    assert result["status"] == "not_live" and exchange.calls == []


def test_dashboard_reads_the_cache_without_calling_the_exchange():
    exchange = _Exchange(error=AssertionError("health must not call the exchange"))
    service = ExchangeReconciliationService(client=exchange)

    assert service.check(None)["status"] == "pending"
    recon._LAST = {"status": "ok", "ok": True}
    assert service.check(None) == {"status": "ok", "ok": True}
    assert exchange.calls == []


def test_disabled_does_not_call_the_exchange(monkeypatch):
    monkeypatch.setattr(settings, "EXCHANGE_RECONCILIATION_ENABLED", False)
    exchange = _Exchange()
    result = ExchangeReconciliationService(client=exchange).check(None)

    assert result["status"] == "disabled" and exchange.calls == []


def test_mismatch_key_is_stable_for_alert_dedupe():
    item = {"type": "exchange_below_book", "symbol": XRP, "side": "short", "book_qty": 150, "exchange_qty": 50}
    assert mismatch_key(item) == mismatch_key({**item, "exchange_qty": 40})
    assert mismatch_key(item) != mismatch_key({**item, "side": "long"})


def test_the_loop_is_started_and_alerts_only_new_mismatches():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "asyncio.create_task(background_exchange_reconciliation_loop())" in source
    loop = source.split("async def background_exchange_reconciliation_loop", 1)[1].split("\nasync def ", 1)[0]
    assert "LIVE_EXECUTOR.is_live()" in loop and "_RECON_ALERTED" in loop


def test_health_page_knows_every_reconciliation_type():
    """Фронт подписывает каждый тип расхождения и читает счётчики, которые пишет сервис."""
    import inspect
    import re
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "web" / "app" / "health" / "page.tsx").read_text(encoding="utf-8")
    source = inspect.getsource(recon)
    types = set(re.findall(r'"type": "([a-z_]+)"', source))
    assert types, "типы расхождений не найдены в сервисе"
    for t in types:
        assert f"  {t}:" in page, f"нет подписи для {t}"
    for counter in ("robot_live_positions", "robot_stops", "exchange_positions"):
        assert f'"{counter}"' in source and f"counts?.{counter}" in page


def test_paper_says_the_reconciliation_is_on_and_waits_for_live(monkeypatch):
    """Включённая сверка в paper не должна выглядеть забытой («pending» без
    объяснения): статус говорит, что она включена и начнёт работу с live."""
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "dry_run"))
    exchange = _Exchange(error=AssertionError("paper must not call the exchange"))

    result = ExchangeReconciliationService(client=exchange).check(None)

    assert result["status"] == "not_live" and result["enabled"] is True and result["ok"] is True
    assert "после включения live" in result["note"]
    assert exchange.calls == []
