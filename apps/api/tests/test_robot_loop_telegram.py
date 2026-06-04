import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
from core.db import Base
from models.bot import Bot
from models.intelligence_event import IntelligenceEvent
from models.signal import Signal
from models.user import User
from workers.robot_loop import RobotLoop


class FailingTelegramRouter:
    async def publish_new_signal(self, **kwargs):
        raise TimeoutError("telegram timeout")


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__,
        Bot.__table__,
        Signal.__table__,
        IntelligenceEvent.__table__,
    ])
    Session = sessionmaker(bind=engine)
    return Session()


def test_private_paper_signal_stays_published_when_optional_telegram_delivery_fails():
    db = _db_session()
    old_mode = settings.TRADING_MODE
    old_live = settings.ENABLE_LIVE_ORDERS

    try:
        settings.TRADING_MODE = "paper_trade"
        settings.ENABLE_LIVE_ORDERS = False

        user = User(email="owner@example.com", password_hash="hash")
        db.add(user)
        db.flush()
        bot = Bot(user_id=user.id, name="Main Robot", status="running", mode="paper", config_json={})
        db.add(bot)
        db.flush()
        signal = Signal(
            bot_id=bot.id,
            symbol="BTC/USDT",
            side="long",
            status="published",
            entry_zone_json={"from": 100.0, "to": 101.0},
            stop_price=99.0,
            tp_json={"tp1": 102.0, "tp2": 104.0},
            confidence=80.0,
            rationale="test",
            grade="A",
            is_public=False,
            plan_json={},
        )
        db.add(signal)
        db.flush()

        loop = RobotLoop()
        loop.telegram_router = FailingTelegramRouter()

        ok = asyncio.run(loop._publish_new_signal_safely(
            db=db,
            sig=signal,
            signal_payload={
                "action": "long",
                "symbol": "BTC/USDT",
                "entry_zone": [100.0, 101.0],
                "stop_price": 99.0,
                "tp": {"tp1": 102.0, "tp2": 104.0},
                "reason": "test",
            },
            effective_confidence=80.0,
            grade="A",
            is_public=False,
        ))

        event = db.query(IntelligenceEvent).one()

        assert ok is False
        assert signal.status == "published"
        assert signal.plan_json["telegram_delivery"]["mode"] == "non_blocking_paper"
        assert signal.plan_json["telegram_delivery"]["vip_delivery_required"] is False
        assert event.status == "warning"
        assert event.decision == "telegram_delivery_failed_signal_kept_published"
    finally:
        settings.TRADING_MODE = old_mode
        settings.ENABLE_LIVE_ORDERS = old_live
        db.close()


def test_public_paper_signal_stays_published_when_vip_delivery_fails():
    db = _db_session()
    old_mode = settings.TRADING_MODE
    old_live = settings.ENABLE_LIVE_ORDERS

    try:
        settings.TRADING_MODE = "paper_trade"
        settings.ENABLE_LIVE_ORDERS = False

        user = User(email="owner@example.com", password_hash="hash")
        db.add(user)
        db.flush()
        bot = Bot(user_id=user.id, name="Main Robot", status="running", mode="paper", config_json={})
        db.add(bot)
        db.flush()
        signal = Signal(
            bot_id=bot.id,
            symbol="BTC/USDT",
            side="long",
            status="published",
            entry_zone_json={"from": 100.0, "to": 101.0},
            stop_price=99.0,
            tp_json={"tp1": 102.0, "tp2": 104.0},
            confidence=80.0,
            rationale="test",
            grade="A",
            is_public=True,
            plan_json={},
        )
        db.add(signal)
        db.flush()

        loop = RobotLoop()
        loop.telegram_router = FailingTelegramRouter()

        ok = asyncio.run(loop._publish_new_signal_safely(
            db=db,
            sig=signal,
            signal_payload={
                "action": "long",
                "symbol": "BTC/USDT",
                "entry_zone": [100.0, 101.0],
                "stop_price": 99.0,
                "tp": {"tp1": 102.0, "tp2": 104.0},
                "reason": "test",
            },
            effective_confidence=80.0,
            grade="A",
            is_public=True,
        ))

        event = db.query(IntelligenceEvent).one()

        assert ok is False
        assert signal.status == "published"
        assert signal.closed_reason is None
        assert signal.plan_json["telegram_delivery"]["mode"] == "non_blocking_paper"
        assert signal.plan_json["telegram_delivery"]["vip_delivery_required"] is True
        assert signal.plan_json["telegram_delivery"]["live_delivery_required"] is False
        assert event.status == "warning"
        assert event.decision == "telegram_delivery_failed_signal_kept_published"
    finally:
        settings.TRADING_MODE = old_mode
        settings.ENABLE_LIVE_ORDERS = old_live
        db.close()


def test_live_signal_marks_telegram_failed_without_raising():
    db = _db_session()
    old_mode = settings.TRADING_MODE
    old_live = settings.ENABLE_LIVE_ORDERS

    try:
        settings.TRADING_MODE = "live_signal"
        settings.ENABLE_LIVE_ORDERS = False

        user = User(email="owner@example.com", password_hash="hash")
        db.add(user)
        db.flush()
        bot = Bot(user_id=user.id, name="Main Robot", status="running", mode="paper", config_json={})
        db.add(bot)
        db.flush()
        signal = Signal(
            bot_id=bot.id,
            symbol="ETH/USDT",
            side="long",
            status="published",
            entry_zone_json={"from": 100.0, "to": 101.0},
            stop_price=99.0,
            tp_json={"tp1": 102.0, "tp2": 104.0},
            confidence=80.0,
            rationale="test",
            grade="A",
            is_public=True,
            plan_json={},
        )
        db.add(signal)
        db.flush()

        loop = RobotLoop()
        loop.telegram_router = FailingTelegramRouter()

        ok = asyncio.run(loop._publish_new_signal_safely(
            db=db,
            sig=signal,
            signal_payload={
                "action": "long",
                "symbol": "ETH/USDT",
                "entry_zone": [100.0, 101.0],
                "stop_price": 99.0,
                "tp": {"tp1": 102.0, "tp2": 104.0},
                "reason": "test",
            },
            effective_confidence=80.0,
            grade="A",
            is_public=True,
        ))

        event = db.query(IntelligenceEvent).one()

        assert ok is False
        assert signal.status == "telegram_failed"
        assert signal.closed_reason == "initial_vip_telegram_publish_failed"
        assert signal.plan_json["telegram_delivery"]["mode"] == "required_live"
        assert signal.plan_json["telegram_delivery"]["vip_delivery_required"] is True
        assert signal.plan_json["telegram_delivery"]["live_delivery_required"] is True
        assert event.status == "telegram_failed"
        assert event.decision == "initial_vip_telegram_publish_failed"
    finally:
        settings.TRADING_MODE = old_mode
        settings.ENABLE_LIVE_ORDERS = old_live
        db.close()
