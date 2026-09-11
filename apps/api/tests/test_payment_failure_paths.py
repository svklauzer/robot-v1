"""Оплата не пропадает молча (#stars-price-drift / #payment-not-applied, 12.09).

Три дыры денежного пути, где человек платит и не получает доступ, а владелец
об этом не знает:
  1. Цена тарифа сменилась после выставления счёта — звёзды списывались по
     старой, зачисление падало на сверке.
  2. Сбой зачисления отвечал Telegram 200 с ошибкой внутри — повтора не было.
  3. Ссылку в VIP выдать не удалось совсем — владелец не узнавал.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.config import settings
from core.db import Base
from models.payment import BillingPlan, Payment
from models.subscriber import Subscriber
from models.telegram_profile import TelegramProfile
import routers.telegram as tg


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[
        Payment.__table__, BillingPlan.__table__, Subscriber.__table__,
        TelegramProfile.__table__,
    ])
    make = sessionmaker(bind=engine)
    monkeypatch.setattr(tg, "SessionLocal", make)
    monkeypatch.setattr(settings, "VIP_STARS_PRICE_30", 4900)
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    return make


@pytest.fixture
def sent(monkeypatch):
    log = {"owner": [], "user": []}

    async def owner_alert(self, title, body):
        log["owner"].append((title, body))

    async def send_message(self, chat_id, text, **kwargs):
        log["user"].append((chat_id, text))

    monkeypatch.setattr(tg.SignalBroadcaster, "send_owner_alert", owner_alert)
    monkeypatch.setattr(tg.SignalBroadcaster, "send_message", send_message)
    return log


def _pending(make, **kw) -> int:
    db = make()
    payment = Payment(telegram_user_id="777", plan_code="vip_30", amount=49.0,
                      currency="USDT", duration_days=30, provider="telegram_stars",
                      provider_payment_id="manual-777", status="pending", **kw)
    db.add(payment)
    db.commit()
    pid = payment.id
    db.close()
    return pid


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(tg.router)
    return TestClient(app, raise_server_exceptions=False)


def _paid(pid, amount=4900):
    return {"message": {"chat": {"id": "777"}, "successful_payment": {
        "invoice_payload": f"vip:{pid}", "telegram_payment_charge_id": f"ch-{pid}",
        "total_amount": amount, "currency": "XTR"}}}


# ── 1. цена сменилась после счёта ───────────────────────────────────────────

def test_a_stale_price_is_refused_before_the_stars_are_taken(factory):
    pid = _pending(factory)
    ok, err = tg._validate_pre_checkout(
        {"invoice_payload": f"vip:{pid}", "currency": "XTR", "total_amount": 3900})
    assert ok is False and "изменилась" in err


def test_the_current_price_passes_the_pre_checkout(factory):
    pid = _pending(factory)
    assert tg._validate_pre_checkout(
        {"invoice_payload": f"vip:{pid}", "currency": "XTR", "total_amount": 4900}) == (True, None)


# ── 2. сбой зачисления ──────────────────────────────────────────────────────

def test_a_rejected_charge_alerts_the_owner_and_answers_the_payer(factory, sent, monkeypatch):
    pid = _pending(factory)
    response = _client().post("/telegram/webhook", json=_paid(pid, amount=1),
                              headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})

    assert response.status_code == 200
    assert response.json()["owner_alerted"] is True
    assert sent["owner"] and "НЕ ЗАЧИСЛЕНА" in sent["owner"][0][0]
    assert sent["user"] and "вручную" in sent["user"][0][1]


def test_a_technical_failure_asks_telegram_to_retry(factory, sent, monkeypatch):
    pid = _pending(factory)

    def boom(self, db, **kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr(tg.BillingService, "confirm_payment", boom)
    response = _client().post("/telegram/webhook", json=_paid(pid),
                              headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})

    assert response.status_code == 500, "200 с ошибкой внутри — Telegram не повторит"
    assert sent["owner"] and "ПОВТОР" in sent["owner"][0][0]


# ── 3. ссылки в VIP нет ─────────────────────────────────────────────────────

def test_a_paid_member_without_any_link_is_reported_to_the_owner(factory, sent, monkeypatch):
    pid = _pending(factory)

    async def no_invite(self, **kwargs):
        raise RuntimeError("bot is not admin of the channel")

    monkeypatch.setattr(tg.TelegramPaymentsService, "create_single_use_invite", no_invite)
    monkeypatch.setattr(settings, "VIP_INVITE_LINK", "")
    response = _client().post("/telegram/webhook", json=_paid(pid),
                              headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})

    assert response.status_code == 200 and response.json()["activated"] is True
    assert any("ССЫЛКИ НЕТ" in title for title, _ in sent["owner"])
