from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.bot import Bot
from models.signal import Signal
from models.subscriber import Subscriber
from services import system_health as system_health_module
from services.system_health import SystemHealthService


class _StaticSummary:
    def __init__(self, payload):
        self.payload = payload

    def summary(self, *args, **kwargs):
        return self.payload


class _StaticCheck:
    def __init__(self, payload):
        self.payload = payload

    def check(self, *args, **kwargs):
        return self.payload


class _StaticSnapshot:
    def __init__(self, payload):
        self.payload = payload

    def snapshot(self, *args, **kwargs):
        return self.payload


class _StaticMl:
    def __init__(self, payload):
        self.payload = payload

    def safe_summary(self):
        return self.payload


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_system_health_service_builds_owner_payload_without_route_globals(monkeypatch):
    monkeypatch.setattr(system_health_module, "MarketConnectivityService", lambda: _StaticCheck({"ok": True, "symbol": "ETH/USDT"}))
    monkeypatch.setattr(system_health_module, "ExchangeReconciliationService", lambda: _StaticCheck({"ok": True, "drift_count": 0}))
    monkeypatch.setattr(system_health_module, "TelegramDeliveryLog", lambda: _StaticSummary({"sla_pct": 100.0}))
    monkeypatch.setattr(system_health_module, "BillingService", lambda: _StaticSummary({"pending": 0}))
    monkeypatch.setattr(system_health_module, "RevenueMetricsService", lambda: _StaticSummary({"gross_revenue": 0.0}))
    monkeypatch.setattr(system_health_module, "FundingArbEngine", lambda: _StaticSummary({"open_positions": 0}))
    monkeypatch.setattr(system_health_module, "LiveSafetyService", lambda: _StaticSnapshot({"blocked": False}))
    monkeypatch.setattr(system_health_module, "MLOutcomeStatsService", lambda: _StaticMl({"status": "ok"}))

    db = _db_session()
    try:
        db.add(Bot(user_id=1, name="Main Robot", status="running", mode="paper", config_json={}))
        db.add(
            Signal(
                bot_id=1,
                symbol="BTC/USDT",
                side="long",
                status="published",
                entry_zone_json={"from": 100, "to": 101},
                stop_price=99,
                tp_json={"tp1": 102},
            )
        )
        db.add(
            Subscriber(
                telegram_user_id="42",
                username="tester",
                full_name="Test User",
                status="active",
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
        )
        db.flush()

        payload = SystemHealthService().summary(
            db,
            loops={"robot_loop": {"enabled": True, "task_created": False, "task_done": None}},
            market_symbol="ETH/USDT",
        )

        assert payload["api"]["ok"] is True
        assert payload["bot"]["status"] == "running"
        assert payload["loops"]["robot_loop"]["enabled"] is True
        assert payload["market"] == {"ok": True, "symbol": "ETH/USDT"}
        assert payload["exchange_reconciliation"] == {"ok": True, "drift_count": 0}
        assert payload["signals"]["total"] == 1
        assert payload["signals"]["published"] == 1
        assert payload["subscribers"]["active"] == 1
        assert payload["telegram_delivery"] == {"sla_pct": 100.0}
        assert payload["payments"] == {"pending": 0}
        assert payload["live_safety"] == {"blocked": False}
        assert payload["ml_outcomes"] == {"status": "ok"}
        assert "production_readiness" in payload
    finally:
        db.close()
