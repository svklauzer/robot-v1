"""Не слишком ли далеко стоит стоп (#stop-width-2026-09-12).

Лучшая когорта доходит до TP1 в 38% и всё равно в минусе: недошедшая теряет
больше, чем приносит дошедшая. Здесь проверяется механика замера ширины стопа:
восстановление исходного стопа, первое событие до TP1, пессимистичная граница и
самопроверка по сделкам, закрытым по стопу.
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
from services.stop_width_curve import _pre_tp1_low, build


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


def _sig(db, *, traj, net, reason="breakeven_stop", tp1_pct=1.0, mode="trend"):
    """Вход 100, qty 1 (номинал 100 USDT), издержки 0.14 USDT (0.14%).
    Плановая потеря на стопе −1.14: стоп в 1.0% от входа."""
    signal = Signal(
        bot_id=db.bot_id, symbol="X/USDT", side="long", status="closed",
        entry_zone_json={"from": 100.0, "to": 100.0}, stop_price=99.0,
        tp_json={"tp1": 100.0 * (1 + tp1_pct / 100), "tp2": 103.0},
        confidence=70.0, rationale="t", grade="B", is_public=True,
        closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        closed_reason=reason, closed_net_pnl=net, closed_total_cost=0.14,
        qty=1.0, net_pnl_stop=-1.14,
        plan_json={"trade_mode": mode,
                   "lifecycle": {"entry_price": 100.0, "traj": traj}},
    )
    db.add(signal)
    db.flush()
    return signal


def _row(out, k):
    return next(c for c in out["curve"] if c["stop_frac"] == k)


def test_the_initial_stop_is_recovered_from_the_planned_loss(db):
    _sig(db, traj=[[0, 0.0], [10, -1.0]], net=-1.14, reason="stop_loss")

    out = build(db)

    assert out["median_stop_dist_pct"] == pytest.approx(1.0)
    assert out["consistency"]["share"] == 1.0


def test_a_closer_stop_trims_a_loser(db):
    """Стоп на половине: −0.5 − 0.14 = −0.64 вместо −1.14 — экономия 0.5."""
    _sig(db, traj=[[0, 0.0], [10, -0.6], [20, -1.0]], net=-1.14, reason="stop_loss")

    half = _row(build(db), 0.5)

    assert half["vs_actual_pct"] == pytest.approx(0.5)
    assert half["losers_trimmed"] == 1 and half["winners_cut"] == 0


def test_a_closer_stop_kills_a_winner_that_dipped_first(db):
    """Цена ушла на −0.6, потом дошла до TP1 и дала +0.9. Стоп на 0.5 закрыл бы
    её на −0.64: −1.54 против факта. Стоп на 0.7 её пропускает."""
    _sig(db, traj=[[0, 0.0], [10, -0.6], [20, 1.5]], net=0.9)

    out = build(db)

    assert _row(out, 0.5)["vs_actual_pct"] == pytest.approx(-1.54)
    assert _row(out, 0.5)["winners_cut"] == 1
    assert _row(out, 0.7)["vs_actual_pct"] == pytest.approx(0.0)


def test_only_the_path_before_tp1_matters(db):
    """После TP1 стоп переносится на уровень после TP1 — провал после него
    исходный стоп уже не касается."""
    low, crossed = _pre_tp1_low([[0, 0.0], [10, 1.2], [20, -0.8]], 1.0)
    assert (low, crossed) == (0.0, True)


def test_the_pessimistic_bound_counts_a_near_touch(db):
    """Минимум −0.47 при стопе на −0.5 и шаге 0.05: запись могла пропустить
    тик на −0.52. Оптимистично не задет, пессимистично — задет."""
    _sig(db, traj=[[0, 0.0], [10, -0.47], [20, 1.5]], net=0.9)

    half = _row(build(db), 0.5)

    assert half["vs_actual_pct"] == pytest.approx(0.0)
    assert half["vs_actual_pessimistic_pct"] == pytest.approx(-1.54)


def test_the_real_stop_is_the_anchor(db):
    _sig(db, traj=[[0, 0.0], [10, -1.0]], net=-1.14, reason="stop_loss")
    _sig(db, traj=[[0, 0.0], [10, -0.95], [20, 1.5]], net=0.9)

    full = _row(build(db), 1.0)

    assert full["vs_actual_pct"] == 0.0
    assert full["vs_actual_pessimistic_pct"] == 0.0


def test_winners_depth_is_reported_in_stops(db):
    _sig(db, traj=[[0, 0.0], [10, -0.2], [20, 1.5]], net=0.9)
    _sig(db, traj=[[0, 0.0], [10, -0.6], [20, 1.5]], net=0.9)

    depth = build(db)["winners_depth_in_stops"]

    assert depth["n"] == 2
    assert depth["max"] == pytest.approx(0.6)


def test_a_wrongly_recovered_stop_shows_up_in_consistency(db):
    """Сделка по стопу, чья траектория до восстановленного стопа не дошла, —
    признак, что восстановление врёт, и кривой верить нельзя."""
    _sig(db, traj=[[0, 0.0], [10, -0.4]], net=-1.14, reason="stop_loss")

    assert build(db)["consistency"]["share"] == 0.0


def test_trade_mode_filter(db):
    _sig(db, traj=[[0, 0.0], [10, -1.0]], net=-1.14, reason="stop_loss", mode="scalp")
    _sig(db, traj=[[0, 0.0], [10, -1.0]], net=-1.14, reason="stop_loss")

    assert build(db, trade_mode="trend")["analysed"] == 1
