"""Правка подписчика со страницы клиентов не портит оплаченное (#clients-audit-2026-09-12).

«Выдать доступ» уже существующему подписчику:
  * перезаписывал срок на now + days — оплаченные 60 дней срезались до 30;
  * перезаписывал заметки пустыми — а в них метки льготных периодов
    (affiliate_*_trial), по которым проверяется «один на человека»;
  * мог превратить действующую оплаченную подписку в пробную.
Статус принимал любую строку, дни — любое число, в том числе отрицательное.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.subscriber import Subscriber
import routers.subscribers as subs


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[Subscriber.__table__])
    session = sessionmaker(bind=engine)()
    monkeypatch.setattr(subs, "SessionLocal", lambda: session)
    monkeypatch.setattr(session, "close", lambda: None)

    async def quiet(self, *args, **kwargs):
        return None

    monkeypatch.setattr(subs.TelegramRouter, "owner_alert", quiet)
    yield session
    session.__class__.close(session)


def _paid(db, days=60, notes="affiliate_okx_trial via link"):
    now = datetime.now(timezone.utc)
    sub = Subscriber(telegram_user_id="777", username="u", full_name="U", plan="vip",
                     status="active", is_trial=False, starts_at=now,
                     expires_at=now + timedelta(days=days), notes=notes)
    db.add(sub)
    db.commit()
    return sub


def _create(**kw):
    payload = subs.CreateSubscriberRequest(telegram_user_id="777", **kw)
    return asyncio.run(subs.create_subscriber(payload))


def test_granting_access_never_shortens_a_paid_subscription(db):
    sub = _paid(db, days=60)
    out = _create(days=30)

    assert out["kept_longer_expiry"] is True
    left = (subs._aware(sub.expires_at) - datetime.now(timezone.utc)).days
    assert left >= 59


def test_notes_keep_the_trial_marker(db):
    sub = _paid(db)
    _create(days=30, notes=None)
    assert "affiliate_okx_trial" in sub.notes

    _create(days=30, notes="продлено вручную")
    assert "affiliate_okx_trial" in sub.notes and "продлено вручную" in sub.notes


def test_a_paid_active_subscription_does_not_become_a_trial(db):
    sub = _paid(db)
    _create(days=30, is_trial=True)
    assert sub.is_trial is False


def test_a_longer_grant_still_extends(db):
    sub = _paid(db, days=10)
    out = _create(days=30)
    assert out["kept_longer_expiry"] is False
    assert (subs._aware(sub.expires_at) - datetime.now(timezone.utc)).days >= 29


def test_status_must_be_one_the_system_knows():
    with pytest.raises(ValidationError):
        subs.UpdateSubscriberStatusRequest(status="activ")
    assert subs.UpdateSubscriberStatusRequest(status="blocked").status == "blocked"


def test_days_must_be_sane():
    for bad in (0, -30, 100000):
        with pytest.raises(ValidationError):
            subs.ExtendSubscriberRequest(days=bad)


def test_extend_works_with_naive_timestamps_from_sqlite(db):
    sub = _paid(db, days=5)
    out = asyncio.run(subs.extend_subscriber(sub.id, subs.ExtendSubscriberRequest(days=10)))
    assert out["status"] == "ok"
