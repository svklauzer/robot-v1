"""Целостность денежного контура: вебхук и подтверждение платежа.

POST /telegram/webhook — единственный маршрут API без owner-токена, и через
него проходит successful_payment. Пока подлинность апдейта не проверялась, а
confirm_payment доверял одному payment_id, подписка активировалась запросом
вида:

    {"message": {"chat": {"id": "1"},
                 "successful_payment": {"invoice_payload": "vip:1",
                                        "telegram_payment_charge_id": "x"}}}

id платежей последовательны, так что перебор активировал все висящие счета.
Здесь закрыты обе половины: подлинность апдейта и проверка самого платежа.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
from core.db import Base
from models.payment import BillingPlan, Payment
from models.subscriber import Subscriber
from models.telegram_profile import TelegramProfile
from services.billing_service import BillingService
from routers.telegram import verify_telegram_webhook


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Payment.__table__,
            BillingPlan.__table__,
            Subscriber.__table__,
            TelegramProfile.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _payment(db, **overrides) -> Payment:
    payload = dict(
        telegram_user_id="777",
        plan_code="vip_30",
        amount=49.0,
        currency="USDT",
        duration_days=30,
        provider="telegram_stars",
        provider_payment_id="manual-777-vip_30-abc",
        status="pending",
    )
    payload.update(overrides)
    payment = Payment(**payload)
    db.add(payment)
    db.flush()
    return payment


# ── подлинность апдейта ──────────────────────────────────────────────────────

def test_secret_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret-real")

    with pytest.raises(HTTPException) as err:
        verify_telegram_webhook(x_telegram_bot_api_secret_token="s3cret-forged")

    assert err.value.status_code == 401


def test_matching_secret_marks_update_verified(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret-real")

    assert verify_telegram_webhook(x_telegram_bot_api_secret_token="s3cret-real") is True


def test_update_is_unverified_when_secret_not_configured(monkeypatch):
    """Без секрета бот продолжает отвечать на команды, но апдейт непроверенный —
    денежная ветка такой не принимает."""
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")

    assert verify_telegram_webhook(x_telegram_bot_api_secret_token=None) is False


def _webhook_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers.telegram import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


_FORGED_PAYMENT = {
    "message": {
        "chat": {"id": "1"},
        "successful_payment": {
            "invoice_payload": "vip:1",
            "telegram_payment_charge_id": "forged",
            "total_amount": 1,
            "currency": "XTR",
        },
    }
}


def test_forged_payment_without_secret_is_refused(monkeypatch):
    """Секрет не настроен → апдейт непроверенный → деньги не принимаем."""
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")

    response = _webhook_client().post("/telegram/webhook", json=_FORGED_PAYMENT)

    assert response.status_code == 401


def test_forged_payment_with_wrong_secret_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret-real")

    response = _webhook_client().post(
        "/telegram/webhook",
        json=_FORGED_PAYMENT,
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret-forged"},
    )

    assert response.status_code == 401


# ── проверка самого платежа ──────────────────────────────────────────────────

def test_stars_amount_must_match_the_plan_price(monkeypatch):
    monkeypatch.setattr(settings, "VIP_STARS_PRICE_30", 4900)
    db = _db()
    try:
        payment = _payment(db)

        with pytest.raises(ValueError, match="payment_amount_mismatch"):
            BillingService().confirm_payment(
                db, payment.id, provider_event_id="charge-1",
                charged_amount=1, charged_currency="XTR",
            )

        assert payment.status == "pending"
        assert db.query(Subscriber).count() == 0
    finally:
        db.close()


def test_correct_stars_amount_activates_subscription(monkeypatch):
    monkeypatch.setattr(settings, "VIP_STARS_PRICE_30", 4900)
    db = _db()
    try:
        payment = _payment(db)

        _, subscriber, activated = BillingService().confirm_payment(
            db, payment.id, provider_event_id="charge-1",
            charged_amount=4900, charged_currency="XTR",
        )

        assert activated is True
        assert subscriber.status == "active"
        assert payment.status == "paid"
        # charge_id провайдера обязан вытеснить заглушку, выданную при создании
        # счёта, иначе уникальный индекс не защищает от повтора события.
        assert payment.provider_payment_id == "charge-1"
    finally:
        db.close()


def test_same_charge_cannot_pay_two_invoices(monkeypatch):
    monkeypatch.setattr(settings, "VIP_STARS_PRICE_30", 4900)
    db = _db()
    try:
        first = _payment(db, provider_payment_id="manual-a")
        second = _payment(db, provider_payment_id="manual-b")

        BillingService().confirm_payment(
            db, first.id, provider_event_id="charge-1",
            charged_amount=4900, charged_currency="XTR",
        )

        with pytest.raises(ValueError, match="provider_event_already_used"):
            BillingService().confirm_payment(
                db, second.id, provider_event_id="charge-1",
                charged_amount=4900, charged_currency="XTR",
            )

        assert second.status == "pending"
    finally:
        db.close()


@pytest.mark.parametrize("status", ["canceled", "refunded", "expired", "failed"])
def test_terminal_payment_cannot_be_revived(status, monkeypatch):
    monkeypatch.setattr(settings, "VIP_STARS_PRICE_30", 4900)
    db = _db()
    try:
        payment = _payment(db, status=status)

        with pytest.raises(ValueError, match="payment_not_confirmable"):
            BillingService().confirm_payment(
                db, payment.id, provider_event_id="charge-1",
                charged_amount=4900, charged_currency="XTR",
            )

        assert db.query(Subscriber).count() == 0
    finally:
        db.close()


def test_repeated_confirmation_does_not_extend_subscription(monkeypatch):
    """Повтор того же события не должен продлевать доступ ещё на срок тарифа."""
    monkeypatch.setattr(settings, "VIP_STARS_PRICE_30", 4900)
    db = _db()
    try:
        payment = _payment(db)
        _, subscriber, first = BillingService().confirm_payment(
            db, payment.id, provider_event_id="charge-1",
            charged_amount=4900, charged_currency="XTR",
        )
        expires_after_first = subscriber.expires_at

        _, subscriber, second = BillingService().confirm_payment(
            db, payment.id, provider_event_id="charge-1",
            charged_amount=4900, charged_currency="XTR",
        )

        assert first is True and second is False
        assert subscriber.expires_at == expires_after_first

        expires = subscriber.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        # Ровно один срок тарифа, а не два.
        assert expires - datetime.now(timezone.utc) < timedelta(days=31)
    finally:
        db.close()
