from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.payment import Payment
from models.subscriber import Subscriber
from services.revenue_metrics import RevenueMetricsService


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[Subscriber.__table__, Payment.__table__])
    return sessionmaker(bind=engine)()


def test_revenue_metrics_counts_mrr_trials_conversion_and_churn():
    db = _db_session()
    now = datetime.now(timezone.utc)

    try:
        active_paid = Subscriber(
            telegram_user_id="1",
            username="paid",
            plan="vip_30",
            status="active",
            starts_at=now,
            expires_at=now + timedelta(days=20),
            is_trial=False,
        )
        active_trial = Subscriber(
            telegram_user_id="2",
            username="trial",
            plan="vip_30",
            status="active",
            starts_at=now,
            expires_at=now + timedelta(days=5),
            is_trial=True,
        )
        expired_trial_paid = Subscriber(
            telegram_user_id="3",
            username="converted",
            plan="vip_90",
            status="expired",
            starts_at=now - timedelta(days=100),
            expires_at=now - timedelta(days=1),
            is_trial=True,
        )
        db.add_all([active_paid, active_trial, expired_trial_paid])
        db.flush()
        db.add_all([
            Payment(
                subscriber_id=active_paid.id,
                telegram_user_id="1",
                plan_code="vip_30",
                amount=49,
                currency="USDT",
                duration_days=30,
                provider="manual",
                provider_payment_id="p1",
                status="paid",
                paid_at=now,
            ),
            Payment(
                subscriber_id=expired_trial_paid.id,
                telegram_user_id="3",
                plan_code="vip_90",
                amount=129,
                currency="USDT",
                duration_days=90,
                provider="manual",
                provider_payment_id="p2",
                status="paid",
                paid_at=now - timedelta(days=10),
            ),
            Payment(
                telegram_user_id="4",
                plan_code="vip_30",
                amount=49,
                currency="USDT",
                duration_days=30,
                provider="manual",
                provider_payment_id="p3",
                status="pending",
            ),
        ])
        db.flush()

        summary = RevenueMetricsService().summary(db, window_days=30)

        assert summary["status"] == "ok"
        assert summary["cash_collected_total"] == 178
        assert summary["pending_amount"] == 49
        assert summary["active_paid_subscribers"] == 1
        assert summary["active_trials"] == 1
        assert summary["trial_users"] == 2
        assert summary["trial_to_paid_users"] == 1
        assert summary["trial_to_paid_conversion_pct"] == 50.0
        assert summary["churn_proxy_pct"] == 33.33
        assert summary["mrr_estimate"] == 92.0
    finally:
        db.close()
