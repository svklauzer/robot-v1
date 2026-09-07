"""Партнёрский льготный период на второй площадке (#okx-affiliate-2026-09-08).

Контур HTX существовал давно: ссылка → «я зарегистрировался» → бесплатный VIP,
маркер в notes, чтобы не выдать дважды. OKX добавляется тем же путём, но с двумя
отличиями, которые нельзя утерять:

  1. Автопроверки регистрации у OKX нет — у HTX она есть через affiliate-API.
     Предполагать её по аналогии нельзя.
  2. Площадок стало две, а льготный период по умолчанию ОДИН на человека.
     Иначе месяц за HTX и месяц за OKX достаются подряд любому желающему:
     без проверки «я зарегистрировался» — это просто нажатие кнопки.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
from core.db import Base
from models.subscriber import Subscriber
from models.telegram_profile import TelegramProfile
from services.affiliate_trial import AffiliateTrialService, configured_venues
from services.telegram_bot_menu import TelegramBotMenuService


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[
        Subscriber.__table__, TelegramProfile.__table__,
    ])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def links(monkeypatch):
    monkeypatch.setattr(settings, "HTX_AFFILIATE_LINK", "https://htx.example/ref", raising=False)
    monkeypatch.setattr(settings, "OKX_AFFILIATE_LINK", "https://okx.com/join/56943292", raising=False)
    monkeypatch.setattr(settings, "OKX_AFFILIATE_CODE", "56943292", raising=False)
    monkeypatch.setattr(settings, "AFFILIATE_FREE_VIP_DAYS", 30, raising=False)
    monkeypatch.setattr(settings, "HTX_AFFILIATE_VERIFY_ENABLED", False, raising=False)


def _callback(data: str) -> dict:
    return {
        "data": data,
        "message": {"chat": {"id": 777}},
        "from": {"id": 777, "username": "alice", "first_name": "Alice"},
    }


# ── выдача периода ──────────────────────────────────────────────────────────

def test_okx_registration_grants_the_trial(db):
    sub, activated, reason = AffiliateTrialService().activate_trial(
        db=db, venue="okx", telegram_user_id="777")

    assert activated and reason == "affiliate_trial_activated"
    assert sub.plan == "affiliate_okx_vip"
    assert sub.is_trial is True
    assert "affiliate_okx_trial" in sub.notes
    assert "56943292" in sub.notes, "ссылка не записана — источник периода не восстановить"


def test_an_unknown_venue_grants_nothing(db):
    sub, activated, reason = AffiliateTrialService().activate_trial(
        db=db, venue="binance", telegram_user_id="777")

    assert (sub, activated, reason) == (None, False, "unknown_affiliate_venue")


# ── один период на человека ─────────────────────────────────────────────────

def test_one_trial_per_person_across_venues(db, monkeypatch):
    """Суть правки. Без этого месяц за HTX и месяц за OKX берутся подряд."""
    monkeypatch.setattr(settings, "AFFILIATE_TRIAL_ONE_PER_USER", True, raising=False)
    svc = AffiliateTrialService()

    assert svc.activate_trial(db=db, venue="htx", telegram_user_id="777")[1] is True

    sub, activated, reason = svc.activate_trial(db=db, venue="okx", telegram_user_id="777")
    assert activated is False
    assert reason == "affiliate_trial_already_claimed"
    assert sub.plan == "affiliate_htx_vip", "второй период всё-таки переписал первый"


def test_per_venue_trials_are_possible_when_allowed(db, monkeypatch):
    """Настройка существует, потому что решение хозяйское, а не техническое:
    владелец может захотеть платить за каждую площадку отдельно."""
    monkeypatch.setattr(settings, "AFFILIATE_TRIAL_ONE_PER_USER", False, raising=False)
    svc = AffiliateTrialService()

    assert svc.activate_trial(db=db, venue="htx", telegram_user_id="777")[1] is True
    assert svc.activate_trial(db=db, venue="okx", telegram_user_id="777")[1] is True


def test_a_paid_subscription_is_never_downgraded_to_a_trial(db):
    now = datetime.now(timezone.utc)
    db.add(Subscriber(telegram_user_id="777", plan="vip", status="active",
                      is_trial=False, starts_at=now,
                      expires_at=now + timedelta(days=200)))
    db.flush()

    _, activated, reason = AffiliateTrialService().activate_trial(
        db=db, venue="okx", telegram_user_id="777")

    assert activated is False and reason == "paid_subscription_already_active"


# ── бот ─────────────────────────────────────────────────────────────────────

def test_the_okx_screen_shows_the_link_and_the_code(db):
    resp = TelegramBotMenuService().handle(db, message=None, callback_query=_callback("okx_affiliate"))

    assert resp.command == "/okx"
    assert "https://okx.com/join/56943292" in resp.text
    assert "56943292" in resp.text
    assert resp.reply_markup["inline_keyboard"][0][0]["url"] == "https://okx.com/join/56943292"
    assert resp.reply_markup["inline_keyboard"][1][0]["callback_data"] == "affiliate_registered:okx"


def test_the_claim_button_grants_the_venue_it_belongs_to(db):
    resp = TelegramBotMenuService().handle(
        db, message=None, callback_query=_callback("affiliate_registered:okx"))

    sub = db.query(Subscriber).filter(Subscriber.telegram_user_id == "777").one()
    assert sub.plan == "affiliate_okx_vip"
    assert "OKX" in resp.text


def test_the_old_bare_button_still_means_htx(db):
    """Кнопки в уже отправленных сообщениях несут прежний `affiliate_registered`
    без площадки. Сломать их значит оборвать воронку у тех, кто открыл бота
    вчера."""
    resp = TelegramBotMenuService().handle(
        db, message=None, callback_query=_callback("affiliate_registered"))

    sub = db.query(Subscriber).filter(Subscriber.telegram_user_id == "777").one()
    assert sub.plan == "affiliate_htx_vip"
    assert resp.command == "/affiliate-registered"


def test_the_menu_offers_only_venues_that_have_a_link(db, monkeypatch):
    """Кнопка «зарегистрируйтесь по ссылке» без ссылки — тупик."""
    monkeypatch.setattr(settings, "OKX_AFFILIATE_LINK", "", raising=False)
    assert configured_venues() == ["htx"]

    resp = TelegramBotMenuService().handle(db, message=None, callback_query=_callback("menu"))
    flat = [b["callback_data"] for row in resp.reply_markup["inline_keyboard"] for b in row]

    assert "htx_affiliate" in flat
    assert "okx_affiliate" not in flat
    assert "status" in flat, "кнопка статуса пропала вместе с площадкой"


def test_both_venues_appear_when_both_are_configured(db):
    resp = TelegramBotMenuService().handle(db, message=None, callback_query=_callback("menu"))
    flat = [b["callback_data"] for row in resp.reply_markup["inline_keyboard"] for b in row]

    assert "htx_affiliate" in flat and "okx_affiliate" in flat


# ── проверка регистрации ────────────────────────────────────────────────────

def test_okx_does_not_borrow_the_htx_verification(db, monkeypatch):
    """У HTX проверка UID есть, у OKX — нет. Включённый флаг HTX не должен
    заставлять OKX спрашивать несуществующую проверку: пользователь остался бы
    ждать ответа, которого никто не даст.
    """
    monkeypatch.setattr(settings, "HTX_AFFILIATE_VERIFY_ENABLED", True, raising=False)

    resp = TelegramBotMenuService().handle(
        db, message=None, callback_query=_callback("affiliate_registered:okx"))

    assert db.query(Subscriber).filter(Subscriber.telegram_user_id == "777").count() == 1
    assert "UID" not in resp.text

    htx = TelegramBotMenuService().handle(
        db, message=None, callback_query=_callback("affiliate_registered"))
    assert "UID" in htx.text, "проверка HTX перестала спрашивать UID"
