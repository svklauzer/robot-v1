from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.payment import BillingPlan, Payment, PaymentEvent
from models.subscriber import Subscriber
from models.telegram_delivery import TelegramDelivery
from models.telegram_profile import TelegramProfile
from services.product_e2e_smoke import ProductE2ESmokeService


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            BillingPlan.__table__,
            Payment.__table__,
            PaymentEvent.__table__,
            Subscriber.__table__,
            TelegramProfile.__table__,
            TelegramDelivery.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def test_product_e2e_smoke_covers_telegram_payment_subscription_and_idempotency():
    db = _db_session()
    try:
        result = ProductE2ESmokeService().run(db, telegram_user_id="900001", plan_code="vip_30")

        assert result["status"] == "ok"
        assert result["ok"] is True
        assert result["checks"] == {
            "start_menu_ok": True,
            "profile_created": True,
            "checkout_created": True,
            "payment_event_created": True,
            "payment_paid": True,
            "subscriber_active": True,
            "vip_access_granted": True,
            "idempotent_event": True,
            "idempotent_expiry_unchanged": True,
            "customer_notification_queued": True,
            "subscription_status_ok": True,
        }
        assert result["counts_after"]["payments"] == result["counts_before"]["payments"] + 1
        assert result["counts_after"]["payment_events"] == result["counts_before"]["payment_events"] + 1
        assert result["counts_after"]["subscribers"] == result["counts_before"]["subscribers"] + 1
        assert result["counts_after"]["telegram_deliveries"] == result["counts_before"]["telegram_deliveries"] + 1
    finally:
        db.close()


def test_product_e2e_smoke_can_be_rolled_back_by_route_contract():
    db = _db_session()
    try:
        service = ProductE2ESmokeService()
        result = service.run(db, telegram_user_id="900002", plan_code="vip_30")
        assert result["ok"] is True

        db.rollback()

        assert db.query(Payment).count() == 0
        assert db.query(PaymentEvent).count() == 0
        assert db.query(Subscriber).count() == 0
        assert db.query(TelegramProfile).count() == 0
        assert db.query(TelegramDelivery).count() == 0
    finally:
        db.close()
