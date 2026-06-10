"""Tests for DailyQualityReportService."""
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.bot import Bot
from models.signal import Signal
from models.telegram_delivery import TelegramDelivery
from models.user import User
from services.daily_quality_report import DailyQualityReportService


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__,
        Bot.__table__,
        Signal.__table__,
        TelegramDelivery.__table__,
    ])
    return sessionmaker(bind=engine)()


def _closed_signal(bot_id: int, pnl: float, reason: str, positive_then_negative: bool = False) -> Signal:
    return Signal(
        bot_id=bot_id,
        symbol="BTC/USDT",
        side="long",
        status="closed",
        entry_zone_json={"from": 100, "to": 101},
        stop_price=99,
        tp_json={"tp1": 102, "tp2": 104},
        confidence=70,
        rationale="test",
        closed_at=datetime.now(timezone.utc),
        closed_net_pnl=pnl,
        closed_reason=reason,
        plan_json={"lifecycle": {"positive_then_negative": positive_then_negative}},
    )


def test_daily_quality_report_empty_db_returns_ok_status():
    db = _db_session()
    try:
        report = DailyQualityReportService(hours=24).build(db)

        assert "status" in report
        assert "trading" in report
        assert "active_signals" in report
        assert "telegram_sla" in report
        assert "validation_gates" in report
        assert "live_safety" in report
        assert "generated_at" in report
    finally:
        db.close()


def test_daily_quality_report_detects_high_failed_setup_share():
    db = _db_session()
    try:
        user = User(email="o@example.com", password_hash="h")
        db.add(user)
        db.flush()
        bot = Bot(user_id=user.id, name="Main Robot", status="running", mode="paper", config_json={})
        db.add(bot)
        db.flush()

        # 4 failed_setup out of 5 = 80%
        for _ in range(4):
            db.add(_closed_signal(bot.id, -1.0, "failed_setup_exit"))
        db.add(_closed_signal(bot.id, 2.0, "tp2_reached"))
        db.flush()

        report = DailyQualityReportService(hours=24).build(db)

        trading = report["trading"]
        assert trading["closed_count"] == 5
        assert trading["failed_setup_share_pct"] == 80.0
        assert report["status"] == "attention_required"
        assert any("failed_setup_exit" in issue for issue in report["issues"])
    finally:
        db.close()


def test_daily_quality_report_reports_positive_then_negative():
    db = _db_session()
    try:
        user = User(email="o@example.com", password_hash="h")
        db.add(user)
        db.flush()
        bot = Bot(user_id=user.id, name="Main Robot", status="running", mode="paper", config_json={})
        db.add(bot)
        db.flush()

        # 3 positive_then_negative out of 4 = 75%
        for _ in range(3):
            db.add(_closed_signal(bot.id, -0.5, "stop_loss", positive_then_negative=True))
        db.add(_closed_signal(bot.id, 3.0, "tp2_reached", positive_then_negative=False))
        db.flush()

        report = DailyQualityReportService(hours=24).build(db)

        trading = report["trading"]
        assert trading["positive_then_negative_share_pct"] == 75.0
        assert any("positive_then_negative" in issue for issue in report["issues"])
    finally:
        db.close()


def test_daily_quality_report_reasons_breakdown():
    db = _db_session()
    try:
        user = User(email="o@example.com", password_hash="h")
        db.add(user)
        db.flush()
        bot = Bot(user_id=user.id, name="Main Robot", status="running", mode="paper", config_json={})
        db.add(bot)
        db.flush()

        db.add(_closed_signal(bot.id, -1.0, "failed_setup_exit"))
        db.add(_closed_signal(bot.id, -1.0, "failed_setup_exit"))
        db.add(_closed_signal(bot.id, 0.1, "protective_breakeven_profit_guard"))
        db.flush()

        report = DailyQualityReportService(hours=24).build(db)

        reasons = report["trading"]["reasons"]
        assert reasons.get("failed_setup_exit") == 2
        assert reasons.get("protective_breakeven_profit_guard") == 1
        # Most common reason should be first
        assert list(reasons.keys())[0] == "failed_setup_exit"
    finally:
        db.close()
