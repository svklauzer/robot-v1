from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.payment import BillingPlan, Payment, PaymentEvent
from models.subscriber import Subscriber
from models.telegram_delivery import TelegramDelivery
from services.billing_service import BillingService
from services.customer_notifications import CustomerNotificationService


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[Subscriber.__table__, BillingPlan.__table__, Payment.__table__, PaymentEvent.__table__, TelegramDelivery.__table__],
    )
    return sessionmaker(bind=engine)()


def test_payment_success_notification_is_queued_once_for_activated_payment():
    db = _db_session()

    try:
        service = BillingService()
        payment = service.create_checkout(db, telegram_user_id="777", username="alice", plan_code="vip_30")
        payment, subscriber, activated = service.confirm_payment(db, payment.id, provider_event_id="evt-777")

        result = CustomerNotificationService().queue_payment_success(db, payment, subscriber, activated)
        db.commit()

        delivery = db.query(TelegramDelivery).one()
        assert result["queued"] is True
        assert delivery.chat_id == "777"
        assert delivery.status == "queued"
        assert delivery.message_type == "customer_payment_success"
        assert "VIP активирован" in delivery.text
        assert "Payment:" in delivery.text
        assert delivery.max_attempts == 5
        assert delivery.reply_markup_json is not None
    finally:
        db.close()


def test_payment_success_notification_skips_idempotent_duplicate_event():
    db = _db_session()

    try:
        service = BillingService()
        payment = service.create_checkout(db, telegram_user_id="888", plan_code="vip_30")
        first_payment, first_subscriber, first_activated, _event = service.process_payment_event(
            db=db,
            payment_id=payment.id,
            provider="manual",
            provider_event_id="evt-1",
            status="paid",
            raw_payload=None,
        )
        CustomerNotificationService().queue_payment_success(db, first_payment, first_subscriber, first_activated)

        second_payment, second_subscriber, second_activated, _event_again = service.process_payment_event(
            db=db,
            payment_id=payment.id,
            provider="manual",
            provider_event_id="evt-1",
            status="paid",
            raw_payload=None,
        )
        second_result = CustomerNotificationService().queue_payment_success(db, second_payment, second_subscriber, second_activated)
        db.commit()

        assert first_activated is True
        assert second_activated is False
        assert second_result["queued"] is False
        assert db.query(TelegramDelivery).count() == 1
    finally:
        db.close()
