from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.payment import BillingPlan, Payment
from models.subscriber import Subscriber
from services.billing_service import BillingService


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[Subscriber.__table__, BillingPlan.__table__, Payment.__table__])
    Session = sessionmaker(bind=engine)
    return Session()


def test_create_checkout_and_manual_confirm_activates_subscriber():
    db = _db_session()
    service = BillingService()

    try:
        payment = service.create_checkout(
            db=db,
            telegram_user_id="42",
            username="tester",
            full_name="Test User",
            plan_code="vip_30",
        )

        assert payment.status == "pending"
        assert payment.amount > 0
        assert payment.duration_days == 30

        paid, subscriber, activated = service.confirm_payment(db, payment.id)

        assert activated is True
        assert paid.status == "paid"
        assert paid.subscriber_id == subscriber.id
        assert subscriber.telegram_user_id == "42"
        assert subscriber.status == "active"
        assert subscriber.plan == "vip_30"
        assert subscriber.expires_at is not None

        paid_again, subscriber_again, activated_again = service.confirm_payment(db, payment.id)

        assert activated_again is False
        assert paid_again.id == paid.id
        assert subscriber_again.id == subscriber.id
    finally:
        db.close()


def test_payment_summary_counts_pending_and_paid_cash():
    db = _db_session()
    service = BillingService()

    try:
        pending = service.create_checkout(db=db, telegram_user_id="1", plan_code="vip_30")
        paid = service.create_checkout(db=db, telegram_user_id="2", plan_code="vip_90")
        service.confirm_payment(db, paid.id)

        summary = service.summary(db)

        assert summary["total"] == 2
        assert summary["pending"] == 1
        assert summary["paid"] == 1
        assert summary["cash_collected"] == paid.amount
        assert pending.status == "pending"
    finally:
        db.close()
