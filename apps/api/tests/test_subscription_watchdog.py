import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.subscriber import Subscriber
from services.subscription_watchdog import SubscriptionWatchdog


class FakeTelegramRouter:
    def __init__(self):
        self.alerts = []

    async def owner_alert(self, title: str, body: str):
        self.alerts.append({"title": title, "body": body})


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[Subscriber.__table__])
    return sessionmaker(bind=engine)()


def test_subscription_watchdog_expires_naive_datetime_and_records_marker():
    db = _db_session()
    try:
        expired = Subscriber(
            telegram_user_id="100",
            username="expired",
            full_name="Expired User",
            plan="vip_30",
            status="active",
            expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None),
            notes="manual_checkout",
        )
        db.add(expired)
        db.flush()

        watchdog = SubscriptionWatchdog()
        watchdog.telegram = FakeTelegramRouter()

        result = asyncio.run(watchdog.check_subscriptions(db))

        assert result["checked"] == 1
        assert result["expired"] == 1
        assert result["expired_ids"] == [expired.id]
        assert expired.status == "expired"
        assert "subscription_watchdog_expired_at=" in expired.notes
        assert watchdog.telegram.alerts[0]["title"] == "SUBSCRIPTION EXPIRED"
    finally:
        db.close()


def test_subscription_watchdog_warns_before_expiry_without_expiring():
    db = _db_session()
    try:
        now = datetime.now(timezone.utc)
        warning_1d = Subscriber(
            telegram_user_id="101",
            username="one_day",
            full_name="One Day",
            plan="vip_30",
            status="active",
            expires_at=now + timedelta(days=1, hours=1),
        )
        warning_3d = Subscriber(
            telegram_user_id="103",
            username="three_day",
            full_name="Three Day",
            plan="vip_90",
            status="trial",
            expires_at=now + timedelta(days=3, hours=1),
        )
        db.add_all([warning_1d, warning_3d])
        db.flush()

        watchdog = SubscriptionWatchdog()
        watchdog.telegram = FakeTelegramRouter()

        result = asyncio.run(watchdog.check_subscriptions(db))

        assert result["expired"] == 0
        assert result["warning_1d"] == 1
        assert result["warning_3d"] == 1
        assert warning_1d.status == "active"
        assert warning_3d.status == "trial"
        assert [alert["title"] for alert in watchdog.telegram.alerts] == [
            "SUBSCRIPTIONS EXPIRE IN 3 DAYS",
            "SUBSCRIPTIONS EXPIRE TOMORROW",
        ]
    finally:
        db.close()
