from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.payment import Payment
from models.subscriber import Subscriber
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


def callback_query(data: str) -> dict:
    return {
        "data": data,
        "message": {"chat": {"id": 777}},
        "from": {"id": 777, "username": "alice", "first_name": "Alice", "last_name": "Trader"},
    }


def test_required_customer_commands_return_operator_ready_responses():
    db = make_session()
    now = datetime.now(timezone.utc)
    db.add(
        Subscriber(
            telegram_user_id="777",
            username="alice",
            full_name="Alice Trader",
            plan="vip_30",
            status="active",
            starts_at=now,
            expires_at=now + timedelta(days=14),
            is_trial=False,
            notes="telegram_menu_contract",
        )
    )
    db.commit()

    service = TelegramBotMenuService()
    cases = [
        ("/start", "/start", "Finmt Robot", "plans"),
        ("/menu", "/menu", "Finmt Robot", "plans"),
        ("/plans", "/plans", "VIP планы", "pay:vip_30"),
        ("/pay", "/pay", "Выберите тариф", "pay:vip_30"),
        ("/status", "/status", "Статус подписки", "status"),
        ("/help", "/help", "FAQ и риски", "plans"),
        ("/support", "/support", "Поддержка", "plans"),
    ]

    for raw_command, normalized_command, expected_text, expected_callback in cases:
        response = service.handle(db, message=telegram_message(raw_command), callback_query=None)

        assert response.command == normalized_command
        assert response.chat_id == "777"
        assert expected_text in response.text
        assert response.reply_markup is not None
        flattened_buttons = [
            button
            for row in response.reply_markup["inline_keyboard"]
            for button in row
        ]
        assert any(button.get("callback_data") == expected_callback for button in flattened_buttons)


def test_customer_menu_callbacks_map_to_supported_commands():
    db = make_session()
    service = TelegramBotMenuService()

    callback_cases = [
        ("plans", "/plans", "VIP планы"),
        ("pay", "/pay", "Выберите тариф"),
        ("status", "/status", "подписка не найдена"),
        ("faq_risks", "/help", "FAQ и риски"),
        ("contact_support", "/support", "Поддержка"),
    ]

    for callback_data, expected_command, expected_text in callback_cases:
        response = service.handle(db, message=None, callback_query=callback_query(callback_data))

        assert response.command == expected_command
        assert response.chat_id == "777"
        assert expected_text in response.text
