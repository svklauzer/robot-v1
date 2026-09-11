"""Что бывает со следующей сделкой после стопа (#after-stop-2026-09-12).

Разбор стопов 12.09: после стопа на символе следующая сделка уходила в стоп в
43.8% против 27.8% без истории. Отчёт проверяет это прямо — по прошлой сделке
на символе и паузе до следующей.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.bot import Bot
from models.signal import Signal
from models.user import User
from services.after_stop_report import build


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, Bot.__table__, Signal.__table__,
    ])
    session = sessionmaker(bind=engine)()
    user = User(email="o@e.com", password_hash="h")
    session.add(user)
    session.flush()
    bot = Bot(user_id=user.id, name="Main Robot", status="running",
              mode="paper", config_json={})
    session.add(bot)
    session.flush()
    session.bot_id = bot.id
    yield session
    session.close()


NOW = datetime.now(timezone.utc)


def _trade(db, *, symbol="X/USDT", side="long", opened_h_ago, closed_h_ago,
           reason="breakeven_stop", net=0.5):
    s = Signal(
        bot_id=db.bot_id, symbol=symbol, side=side, status="closed",
        entry_zone_json={"from": 100.0, "to": 100.0}, stop_price=99.0,
        tp_json={"tp1": 101.0, "tp2": 103.0}, confidence=70.0, rationale="t",
        grade="B", is_public=True,
        opened_at=NOW - timedelta(hours=opened_h_ago),
        closed_at=NOW - timedelta(hours=closed_h_ago),
        closed_reason=reason, closed_net_pnl=net, plan_json={},
    )
    db.add(s)
    db.flush()
    return s


def test_a_trade_right_after_a_stop_lands_in_the_first_bucket(db):
    _trade(db, opened_h_ago=50, closed_h_ago=40, reason="stop_loss", net=-2.0)
    _trade(db, opened_h_ago=38, closed_h_ago=30, reason="stop_loss", net=-2.0)   # через 2 ч

    out = build(db)

    first = out["buckets"]["after_stop_0-6h"]
    assert first["n"] == 1 and first["stops"] == 1
    assert out["buckets"]["no_previous"]["n"] == 1


def test_the_gap_is_measured_to_the_opening_of_the_next_trade(db):
    _trade(db, opened_h_ago=100, closed_h_ago=90, reason="stop_loss", net=-2.0)
    _trade(db, opened_h_ago=60, closed_h_ago=50)                                 # через 30 ч

    assert build(db)["buckets"]["after_stop_24-72h"]["n"] == 1


def test_a_trade_opened_before_the_previous_one_closed_does_not_follow_it(db):
    """Параллельные сделки: вторая открыта, пока первая ещё жила, — она не
    «после стопа», её исход с первой не связан."""
    _trade(db, opened_h_ago=50, closed_h_ago=20, reason="stop_loss", net=-2.0)
    _trade(db, opened_h_ago=40, closed_h_ago=10)

    out = build(db)

    assert out["buckets"]["no_previous"]["n"] == 2
    assert out["after_stop_any"]["n"] == 0


def test_other_symbols_do_not_count_as_history(db):
    _trade(db, symbol="A/USDT", opened_h_ago=50, closed_h_ago=40, reason="stop_loss", net=-2.0)
    _trade(db, symbol="B/USDT", opened_h_ago=38, closed_h_ago=30)

    assert build(db)["buckets"]["no_previous"]["n"] == 2


def test_same_side_only_looks_at_the_same_direction(db):
    _trade(db, side="short", opened_h_ago=50, closed_h_ago=40, reason="stop_loss", net=-2.0)
    _trade(db, side="long", opened_h_ago=38, closed_h_ago=30)

    assert build(db)["buckets"]["after_stop_0-6h"]["n"] == 1
    assert build(db, same_side=True)["buckets"]["no_previous"]["n"] == 2


def test_money_and_interval_are_reported(db):
    _trade(db, opened_h_ago=50, closed_h_ago=40, reason="stop_loss", net=-2.0)
    _trade(db, opened_h_ago=38, closed_h_ago=30, reason="stop_loss", net=-2.0)
    _trade(db, opened_h_ago=28, closed_h_ago=20, net=1.0)

    after = build(db)["after_stop_any"]
    assert after["n"] == 2 and after["stops"] == 1
    assert after["net_usdt"] == pytest.approx(-1.0)
    lo, hi = after["stop_rate_ci"]
    assert lo < 0.5 < hi
