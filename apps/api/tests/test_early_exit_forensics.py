"""Почему «безубыток» теряет деньги (#be-lock-leak-2026-09-12).

`breakeven_lock` за 500 сделок: 46 штук, −0.66 USDT на сделку. Отчёт отличает
две причины: выход далеко под полом замка (тик провалился) и выход у пола,
который не покрывает круг издержек рынка сделки.
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
from services.early_exit_forensics import build


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


def _sig(db, *, market, fee, exit_pct, cost_usdt, net, side="long"):
    """Вход 100, qty 1 — номинал 100 USDT."""
    exit_price = 100.0 * (1 + exit_pct / 100) if side == "long" else 100.0 * (1 - exit_pct / 100)
    signal = Signal(
        bot_id=db.bot_id, symbol="X/USDT", side=side, status="closed",
        entry_zone_json={"from": 100.0, "to": 100.0}, stop_price=99.0,
        tp_json={"tp1": 101.0, "tp2": 103.0},
        confidence=70.0, rationale="t", grade="B", is_public=True,
        closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        closed_reason="breakeven_lock", closed_exit_price=exit_price,
        closed_net_pnl=net, closed_total_cost=cost_usdt, qty=1.0,
        plan_json={
            "routing": {"market_type": market},
            "lifecycle": {"entry_price": 100.0, "mfe_pct": 0.8},
            "config": {
                "market": {"taker_fee": fee, "slippage_buffer_pct": 0.0002},
                "exit": {"breakeven_lock_floor_pct": 0.18,
                         "breakeven_lock_cost_buffer_pct": 0.05},
            },
        },
    )
    db.add(signal)
    db.flush()
    return signal


def test_spot_floor_covers_the_spot_round_trip(db):
    """Спот 0.2%: круг 0.42% + буфер 0.05 = пол 0.47%, а не 0.18."""
    _sig(db, market="spot", fee=0.002, exit_pct=0.47, cost_usdt=0.42, net=0.05)

    row = build(db)["trades"][0]

    assert row["expected_floor_pct"] == pytest.approx(0.47)
    assert row["below_floor_pct"] == pytest.approx(0.0, abs=1e-6)


def test_a_tick_that_fell_through_the_floor_shows_up(db):
    """Выход на +0.10 при поле 0.18 (своп): провал на 0.08 под пол."""
    _sig(db, market="swap", fee=0.0005, exit_pct=0.10, cost_usdt=0.12, net=-0.02)

    out = build(db)

    assert out["by_market_type"]["swap"]["median_below_floor_pct"] == pytest.approx(0.08)
    assert out["by_market_type"]["swap"]["net_negative"] == 1


def test_without_fee_in_the_snapshot_it_comes_from_the_trade_cost(db):
    """Снимки с 02.09 писались без ставки: круг 0.42 → ставка (0.0042−0.0002)/2."""
    _sig(db, market="spot", fee=None, exit_pct=0.47, cost_usdt=0.42, net=0.05)

    row = build(db)["trades"][0]

    assert row["fee_source"] == "derived_from_trade_cost"
    assert row["fee_rate"] == pytest.approx(0.002)


def test_markets_are_summarised_apart(db):
    _sig(db, market="spot", fee=0.002, exit_pct=0.30, cost_usdt=0.42, net=-0.12)
    _sig(db, market="swap", fee=0.0005, exit_pct=0.18, cost_usdt=0.12, net=0.06,
         side="short")

    out = build(db)

    assert set(out["by_market_type"]) == {"spot", "swap"}
    assert out["all"]["n"] == 2
    assert out["by_market_type"]["spot"]["net_usdt"] == pytest.approx(-0.12)
