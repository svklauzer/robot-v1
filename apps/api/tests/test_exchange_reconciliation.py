from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.bot import Bot
from models.order import Order
from models.position import Position
from models.signal import Signal
from models.user import User
from services.exchange_reconciliation import ExchangeReconciliationService


class FakeClient:
    def __init__(self, orders=None, positions=None, error=None):
        self.orders = orders or []
        self.positions = positions or []
        self.error = error
        self.calls = []

    def load_markets(self):
        self.calls.append("load_markets")
        if self.error:
            raise self.error
        return {"BTC/USDT": {}}

    def fetch_balance(self):
        self.calls.append("fetch_balance")
        return {"USDT": {"free": 1000}}

    def fetch_open_orders(self, symbol=None):
        self.calls.append(("fetch_open_orders", symbol))
        return self.orders

    def fetch_positions(self):
        self.calls.append("fetch_positions")
        return self.positions


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__, Bot.__table__, Signal.__table__, Order.__table__, Position.__table__])
    return sessionmaker(bind=engine)()


def _seed_live_position(db):
    user = User(email="owner@example.com", password_hash="x")
    bot = Bot(user_id=1, name="Main Robot", status="running", mode="live")
    signal = Signal(
        bot_id=1,
        symbol="BTC/USDT",
        side="long",
        status="opened",
        entry_zone_json={"from": 100, "to": 101},
        stop_price=99,
        tp_json={"tp1": 102, "tp2": 104},
    )
    db.add_all([user, bot, signal])
    db.flush()
    order = Order(
        bot_id=bot.id,
        signal_id=signal.id,
        symbol="BTC/USDT",
        side="buy",
        order_type="market",
        status="open",
        qty=1.0,
        filled_qty=1.0,
        client_order_id="client-1",
        exchange_order_id="ex-1",
    )
    position = Position(
        bot_id=bot.id,
        signal_id=signal.id,
        symbol="BTC/USDT",
        side="long",
        qty=1.0,
        entry_price=100,
        unrealized_pnl=0.0,
        status="open",
    )
    db.add_all([order, position])
    db.commit()
    return order, position


def test_exchange_reconciliation_disabled_does_not_call_exchange(monkeypatch):
    monkeypatch.setattr("services.exchange_reconciliation.settings.EXCHANGE_RECONCILIATION_ENABLED", False)
    monkeypatch.setattr("services.exchange_reconciliation.settings.ENABLE_LIVE_ORDERS", False)
    monkeypatch.setattr("services.exchange_reconciliation.settings.TRADING_MODE", "paper_signal")
    db = _db()
    client = FakeClient()

    result = ExchangeReconciliationService(client=client).check(db)

    assert result["status"] == "disabled"
    assert result["ok"] is True
    assert client.calls == []


def test_exchange_reconciliation_matches_local_live_state(monkeypatch):
    monkeypatch.setattr("services.exchange_reconciliation.settings.EXCHANGE_RECONCILIATION_ENABLED", True)
    db = _db()
    _seed_live_position(db)
    client = FakeClient(
        orders=[{"id": "ex-1", "clientOrderId": "client-1", "symbol": "BTC/USDT"}],
        positions=[
            {"symbol": "BTC/USDT", "side": "long", "contracts": 1},
            {"symbol": "ETH/USDT", "side": "short", "contracts": 0},
        ],
    )

    result = ExchangeReconciliationService(client=client).check(db, symbol="BTC/USDT")

    assert result["status"] == "ok"
    assert result["ok"] is True
    assert result["counts"]["local_open_orders"] == 1
    assert result["counts"]["exchange_open_orders"] == 1
    assert result["counts"]["local_live_positions"] == 1
    assert result["counts"]["exchange_positions"] == 1
    assert result["mismatches"] == []


def test_exchange_reconciliation_reports_missing_remote_state(monkeypatch):
    monkeypatch.setattr("services.exchange_reconciliation.settings.EXCHANGE_RECONCILIATION_ENABLED", True)
    db = _db()
    _seed_live_position(db)

    result = ExchangeReconciliationService(client=FakeClient()).check(db, symbol="BTC/USDT")

    assert result["status"] == "mismatch"
    assert result["ok"] is False
    assert {item["type"] for item in result["mismatches"]} == {
        "missing_exchange_order",
        "missing_exchange_position",
    }


def test_exchange_reconciliation_reports_reconnect_failure(monkeypatch):
    monkeypatch.setattr("services.exchange_reconciliation.settings.EXCHANGE_RECONCILIATION_ENABLED", True)
    db = _db()

    result = ExchangeReconciliationService(client=FakeClient(error=TimeoutError("htx down"))).check(db)

    assert result["status"] == "degraded"
    assert result["ok"] is False
    assert "TimeoutError" in result["error"]
