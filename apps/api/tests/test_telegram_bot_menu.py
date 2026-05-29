from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.payment import Payment
from models.telegram_profile import TelegramProfile
from services.telegram_bot_menu import TelegramBotMenuService


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def telegram_message(text: str = "/start") -> dict:
    return {
        "text": text,
        "chat": {"id": 777},
        "from": {
            "id": 777,
            "username": "alice",
            "first_name": "Alice",
            "last_name": "Trader",
        },
    }


def test_start_creates_profile_and_inline_menu():
    db = make_session()
    response = TelegramBotMenuService().handle(db, message=telegram_message("/start"), callback_query=None)
    db.commit()

    profile = db.query(TelegramProfile).one()

    assert response.command == "/start"
    assert response.chat_id == "777"
    assert response.reply_markup["inline_keyboard"][0][0]["callback_data"] == "plans"
    assert profile.telegram_user_id == "777"
    assert profile.funnel_stage == "started"
    assert profile.username == "alice"


def test_pay_callback_creates_pending_checkout_and_updates_funnel_stage():
    db = make_session()
    callback = {
        "data": "pay:vip_30",
        "message": {"chat": {"id": 777}},
        "from": {"id": 777, "username": "alice", "first_name": "Alice"},
    }

    response = TelegramBotMenuService().handle(db, message=None, callback_query=callback)
    db.commit()

    payment = db.query(Payment).one()
    profile = db.query(TelegramProfile).one()

    assert response.command == "/pay"
    assert "Checkout создан" in response.text
    assert payment.status == "pending"
    assert payment.plan_code == "vip_30"
    assert profile.funnel_stage == "checkout_pending"
