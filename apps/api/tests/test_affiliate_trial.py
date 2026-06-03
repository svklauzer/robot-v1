from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
from core.db import Base
from models.payment import BillingPlan, Payment, PaymentEvent
from models.subscriber import Subscriber
from models.telegram_profile import TelegramProfile
from services.affiliate_trial import AffiliateTrialService
from services.billing_service import BillingService
from services.telegram_bot_menu import TelegramBotMenuService


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Subscriber.__table__,
            TelegramProfile.__table__,
            BillingPlan.__table__,
            Payment.__table__,
            PaymentEvent.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _callback(data: str):
    return {
        "data": data,
        "message": {"chat": {"id": 777}},
        "from": {"id": 777, "username": "alice", "first_name": "Alice"},
    }


def test_htx_affiliate_callback_shows_link_and_claim_button():
    db = _db_session()
    old_link = settings.HTX_AFFILIATE_LINK

    try:
        settings.HTX_AFFILIATE_LINK = "https://example.com/htx-affiliate"
        response = TelegramBotMenuService().handle(db, message=None, callback_query=_callback("htx_affiliate"))

        assert response.command == "/htx"
        assert "https://example.com/htx-affiliate" in response.text
        assert response.reply_markup["inline_keyboard"][0][0]["url"] == "https://example.com/htx-affiliate"
        assert response.reply_markup["inline_keyboard"][1][0]["callback_data"] == "affiliate_registered"
    finally:
        settings.HTX_AFFILIATE_LINK = old_link
        db.close()


def test_affiliate_registered_activates_free_vip_once():
    db = _db_session()
    old_days = settings.AFFILIATE_FREE_VIP_DAYS
    old_invite = settings.VIP_INVITE_LINK

    try:
        settings.AFFILIATE_FREE_VIP_DAYS = 30
        settings.VIP_INVITE_LINK = "https://t.me/vip"

        response = TelegramBotMenuService().handle(db, message=None, callback_query=_callback("affiliate_registered"))
        db.commit()
        subscriber = db.query(Subscriber).one()
        profile = db.query(TelegramProfile).one()

        assert response.command == "/affiliate-registered"
        assert "HTX affiliate VIP активирован" in response.text
        assert "https://t.me/vip" in response.text
        assert subscriber.status == "active"
        assert subscriber.plan == "affiliate_htx_vip"
        assert subscriber.is_trial is True
        assert "affiliate_htx_trial" in subscriber.notes
        assert profile.funnel_stage == "affiliate_trial_active"

        second = TelegramBotMenuService().handle(db, message=None, callback_query=_callback("affiliate_registered"))
        db.commit()
        assert "уже был активирован" in second.text
        assert db.query(Subscriber).count() == 1
    finally:
        settings.AFFILIATE_FREE_VIP_DAYS = old_days
        settings.VIP_INVITE_LINK = old_invite
        db.close()


def test_affiliate_trial_does_not_override_active_paid_subscription():
    db = _db_session()
    now = datetime.now(timezone.utc)

    try:
        subscriber = Subscriber(
            telegram_user_id="900",
            username="paid",
            plan="vip_30",
            status="active",
            starts_at=now,
            expires_at=now + timedelta(days=20),
            is_trial=False,
            notes="paid",
        )
        db.add(subscriber)
        db.flush()

        result_sub, activated, reason = AffiliateTrialService().activate_htx_trial(db, telegram_user_id="900")

        assert activated is False
        assert reason == "paid_subscription_already_active"
        assert result_sub.plan == "vip_30"
        assert result_sub.is_trial is False
    finally:
        db.close()


def test_affiliate_trial_paid_conversion_is_tracked_on_payment_confirm():
    db = _db_session()
    old_days = settings.AFFILIATE_FREE_VIP_DAYS

    try:
        settings.AFFILIATE_FREE_VIP_DAYS = 30
        menu = TelegramBotMenuService()
        menu.handle(db, message=None, callback_query=_callback("htx_affiliate"))
        menu.handle(db, message=None, callback_query=_callback("affiliate_registered"))
        db.commit()

        profile = db.query(TelegramProfile).one()
        trial_subscriber = db.query(Subscriber).one()
        trial_expiry = trial_subscriber.expires_at

        assert profile.funnel_stage == "affiliate_trial_active"
        assert trial_subscriber.is_trial is True

        billing = BillingService()
        payment = billing.create_checkout(
            db=db,
            telegram_user_id="777",
            username="alice",
            plan_code="vip_30",
        )
        paid, subscriber, activated = billing.confirm_payment(db, payment.id)
        db.commit()
        db.refresh(profile)
        db.refresh(subscriber)

        assert activated is True
        assert paid.status == "paid"
        assert subscriber.id == trial_subscriber.id
        assert subscriber.is_trial is False
        assert subscriber.plan == "vip_30"
        assert subscriber.expires_at > trial_expiry
        assert profile.funnel_stage == "affiliate_paid_conversion"
        assert profile.last_command == "paid_conversion"
    finally:
        settings.AFFILIATE_FREE_VIP_DAYS = old_days
        db.close()
