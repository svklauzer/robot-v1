"""Проверка счёта перед включением live (#live-preflight-2026-09-16)."""
from __future__ import annotations

from pathlib import Path

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
from services import live_preflight as lp
from services.live_executor import LiveExecutor
from services.live_preflight import LivePreflight

XRP = "XRP/USDT:USDT"
BTC = "BTC/USDT:USDT"


class _Exchange:
    UNIFIED_TRADING_ACCOUNT = True

    def __init__(self, *, free=600.0, balance_error=None, account=None, positions=None,
                 orders=None, stops=None, sizes=None):
        self.free = free
        self.balance_error = balance_error
        self.account = account if account is not None else {"hedged": False, "blocker": None, "info": {}}
        self.positions = positions or []
        self.orders = orders or {}
        self.stops = stops or {}
        self.sizes = sizes if sizes is not None else {XRP: 100.0, BTC: 0.01}
        self.writes: list = []

    def fetch_balance(self, params=None):
        if self.balance_error:
            raise self.balance_error
        return {"USDT": {"free": self.free, "total": self.free + 100}}

    def fetch_derivatives_account(self):
        return self.account

    def contract_size(self, symbol):
        return self.sizes.get(symbol)

    def fetch_positions(self):
        return self.positions

    def fetch_open_orders(self, symbol=None):
        return self.orders.get(symbol, [])

    def fetch_open_stop_orders(self, symbol):
        return self.stops.get(symbol, [])

    def __getattr__(self, name):
        if name.startswith(("create", "cancel", "set_")):
            raise AssertionError(f"preflight вызвал изменяющий метод {name}")
        raise AttributeError(name)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx")
    monkeypatch.setattr(settings, "OKX_SYMBOLS", "XRP/USDT,BTC/USDT")
    for key in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
        monkeypatch.setattr(settings, key, "set")
    monkeypatch.setattr(settings, "TREND_MARGIN_MODE", "isolated")
    monkeypatch.setattr(settings, "ENABLE_FUTURES", True)
    monkeypatch.setattr(settings, "ENABLE_FUTURES_EXECUTION", True)
    monkeypatch.setattr(settings, "FUTURES_LEVERAGE", 1)
    monkeypatch.setattr(settings, "MAX_POSITION_MARGIN_PCT", 0.2)
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 250.0)
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 0.4)
    monkeypatch.setattr(lp, "TESTED_CCXT_VERSION", __import__("ccxt").__version__)
    from services import validation_gates

    monkeypatch.setattr(validation_gates.ValidationGateService, "evaluate", lambda self, db, limit=None: {"blockers": []})
    monkeypatch.setattr(type(settings), "production_blockers", lambda self: [])


def _db(*, signals=()):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__, Bot.__table__, Signal.__table__,
                                                  Order.__table__, Position.__table__])
    db = sessionmaker(bind=engine)()
    db.add_all([User(email="owner@example.com", password_hash="x"),
                Bot(user_id=1, name="Main Robot", status="running", mode="paper")])
    db.flush()
    for execution, margin in signals:
        db.add(Signal(bot_id=1, symbol="XRP/USDT", side="short", status="opened", exchange="okx",
                      entry_zone_json={"from": 1.4, "to": 1.41}, stop_price=1.5, tp_json={"tp1": 1.3, "tp2": 1.2},
                      required_margin=margin,
                      plan_json={"routing": {"market_type": "swap", "exchange_symbol": XRP, "side": "short",
                                             "leverage": 1, "margin_mode": "isolated"},
                                 "execution": {"mode": execution}}))
    db.commit()
    return db


def _run(exchange, db=None):
    executor = LiveExecutor.__new__(LiveExecutor)
    executor.client = exchange
    db = db or _db()
    bot = db.query(Bot).first()
    return LivePreflight(executor).run(db, bot)


def _check(result, check_id):
    return next(c for c in result["checks"] if c["id"] == check_id)


def test_clean_account_is_ready_and_lists_the_switches():
    result = _run(_Exchange())

    assert result["ready"] is True, [c for c in result["checks"] if c["status"] == "fail"]
    assert {"ROBOT_MODE", "TRADING_MODE", "ENABLE_LIVE_ORDERS", "LIVE_EXECUTION_MODE"} <= set(result["switches_to_flip"])
    assert "ACTIVE_EXCHANGE" not in result["switches_to_flip"]
    assert _check(result, "exchange_activity")["status"] == "ok"


def test_capital_is_free_margin_plus_robot_positions_with_config_leverage():
    db = _db(signals=[("live", 200.0), ("paper", 300.0)])
    capital = _check(_run(_Exchange(free=600.0), db), "capital")

    assert capital["capital_usdt"] == 800.0 and capital["robot_live_margin_usdt"] == 200.0
    assert capital["leverage"] == 1 and capital["max_order_notional_usdt"] == 160.0
    assert capital["accounts"] == ["swap"]


def test_account_mode_blocker_fails():
    blocker = "okx_account_mode_spot_only: переключить Trading mode"
    result = _run(_Exchange(account={"hedged": False, "blocker": blocker}))

    assert result["ready"] is False
    assert _check(result, "account_mode")["status"] == "fail"


def test_rejected_key_fails_access_and_capital():
    result = _run(_Exchange(balance_error=PermissionError("50113 Invalid Sign")))

    assert result["ready"] is False
    assert "50113" in _check(result, "balance")["detail"]
    assert _check(result, "capital")["status"] == "fail"


def test_missing_keys_fail(monkeypatch):
    monkeypatch.setattr(settings, "OKX_API_PASSPHRASE", "")
    assert _check(_run(_Exchange()), "api_keys")["status"] == "fail"


def test_unknown_contract_size_fails():
    result = _run(_Exchange(sizes={XRP: 100.0}))
    assert _check(result, "markets")["symbols"] == [BTC]


def test_manual_trading_is_reported_not_blocking():
    exchange = _Exchange(
        positions=[
            {"symbol": BTC, "side": "long", "contracts": 3.0, "marginMode": "isolated"},
            {"symbol": XRP, "side": "long", "contracts": 5.0, "marginMode": "cross"},
        ],
        orders={XRP: [{"id": "m1", "clientOrderId": None, "side": "buy"}]},
        stops={BTC: [{"id": "m2", "clientOrderId": "", "side": "sell"},
                     {"id": "r1", "clientOrderId": "rbtslexch00aa", "side": "buy"}]},
    )
    result = _run(exchange)

    assert result["ready"] is True
    mixing = _check(result, "manual_positions_mixing")
    assert mixing["status"] == "warn" and [p["symbol"] for p in mixing["positions"]] == [BTC]
    manual = _check(result, "manual_activity")
    assert manual["status"] == "info" and len(manual["orders"]) == 2 and len(manual["positions"]) == 1
    leftovers = _check(result, "robot_leftovers")
    assert leftovers["status"] == "warn" and leftovers["orders"][0]["id"] == "r1"


def test_positions_opened_in_paper_are_named():
    db = _db(signals=[("paper", 100.0), ("dry_run", 100.0)])
    check = _check(_run(_Exchange(), db), "paper_positions")
    assert check["status"] == "warn" and len(check["signal_ids"]) == 2


def test_untested_ccxt_version_warns(monkeypatch):
    monkeypatch.setattr(lp, "TESTED_CCXT_VERSION", "0.0.1")
    assert _check(_run(_Exchange()), "ccxt_version")["status"] == "warn"


def test_validation_gates_block_live(monkeypatch):
    from services import validation_gates

    monkeypatch.setattr(validation_gates.ValidationGateService, "evaluate",
                        lambda self, db, limit=None: {"blockers": ["positive_to_negative > 25%"]})
    result = _run(_Exchange())
    assert result["ready"] is False and _check(result, "validation_gates")["status"] == "fail"


def test_preflight_runs_only_on_demand():
    main = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert main.count("LivePreflight()") == 1
    endpoint = main.split('@app.get("/live/preflight"', 1)[1].split("\n@app.", 1)[0]
    assert "require_owner_action" in endpoint and "asyncio.to_thread" in endpoint


def test_health_page_renders_the_preflight():
    page = (Path(__file__).resolve().parents[2] / "web" / "app" / "health" / "page.tsx").read_text(encoding="utf-8")
    assert 'apiGet("/live/preflight")' in page
    for field in ("switches_to_flip", "preflight.switches", "sw.ok", "sw.current", "sw.required",
                  "check.title", "check.detail", "check.status", "preflight.ready"):
        assert field in page, field
    for status in (lp.OK, lp.INFO, lp.WARN, lp.FAIL):
        assert f"{status}:" in page.split("const PREFLIGHT_MARK", 1)[1].split("\n", 1)[0]


def test_live_execution_mode_is_pinned_in_the_blueprint():
    blueprint = (Path(__file__).resolve().parents[3] / "render.yaml").read_text(encoding="utf-8")
    block = blueprint.split("key: LIVE_EXECUTION_MODE", 1)[1].split("- key:", 1)[0]
    assert "value: dry_run" in block
