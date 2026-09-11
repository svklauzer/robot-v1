"""Где ставить TP1 (#tp1-distance-2026-09-12).

Мир проигрыша — нынешние правила: половина на TP1, стоп остатка на самом TP1.
Проверяется механика: недошедшая сделка не меняется, дошедшая до нового TP1
фиксирует долю там и закрывает остаток на первом возврате, стоп, до которого
цена успела пройти новый TP1, превращается в маленький выигрыш, а сравнение
идёт с тем же миром при прежнем TP1, а не с фактом.
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
from services.tp1_distance_curve import _replay, build


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


def _sig(db, *, traj, net, exit_pct, mfe, reason="breakeven_stop"):
    """Вход 100, qty 1, издержки 0.14 (0.14%), стоп 1.0% (−1.14 по плану),
    TP1 1.0%."""
    signal = Signal(
        bot_id=db.bot_id, symbol="X/USDT", side="long", status="closed",
        entry_zone_json={"from": 100.0, "to": 100.0}, stop_price=99.0,
        tp_json={"tp1": 101.0, "tp2": 103.0},
        confidence=70.0, rationale="t", grade="B", is_public=True,
        closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        closed_reason=reason, closed_net_pnl=net, closed_total_cost=0.14,
        closed_exit_price=100.0 * (1 + exit_pct / 100), qty=1.0, net_pnl_stop=-1.14,
        plan_json={"trade_mode": "trend",
                   "lifecycle": {"entry_price": 100.0, "mfe_pct": mfe, "traj": traj}},
    )
    db.add(signal)
    db.flush()
    return signal


def _row(out, j):
    return next(c for c in out["curve"] if c["tp1_frac"] == j)


def test_a_near_miss_that_was_stopped_becomes_a_small_win(db):
    """Дошла до +0.85 (85% пути к TP1) и ушла в стоп: −1.14. С TP1 на 0.8
    половина фиксируется на +0.8, стоп остатка там же, возврат закрывает его на
    +0.8 − 0.0 (недостача стопа по сделке 0): 0.8 − 0.14 = +0.66."""
    _sig(db, traj=[[0, 0.0], [10, 0.85], [20, -1.0]], net=-1.14, exit_pct=-1.0,
         mfe=0.85, reason="stop_loss")

    out = build(db)

    assert _row(out, 0.8)["stops_converted"] == 1
    assert _row(out, 0.8)["vs_same_rules_pct"] == pytest.approx(0.66 + 1.14)
    assert _row(out, 0.9)["vs_same_rules_pct"] == pytest.approx(0.0)


def test_a_closer_tp1_costs_the_trade_that_reached_the_old_one(db):
    """Дошла до TP1 (1.0), вернулась и закрылась в безубытке. При нынешних
    правилах: 0.5·1.0 + 0.5·1.0 − 0.14 = 0.86. С TP1 на 0.6: 0.6 − 0.14 = 0.46."""
    _sig(db, traj=[[0, 0.0], [10, 1.2], [20, 0.07]], net=0.07, exit_pct=0.07, mfe=1.2)

    row = _row(build(db), 0.6)

    assert row["vs_same_rules_pct"] == pytest.approx(0.46 - 0.86)


def test_a_trade_that_never_reached_the_new_tp1_is_untouched(db):
    _sig(db, traj=[[0, 0.0], [10, 0.3], [20, -1.0]], net=-1.14, exit_pct=-1.0,
         mfe=0.3, reason="stop_loss")

    out = build(db)

    assert all(c["vs_same_rules_pct"] == pytest.approx(0.0) for c in out["curve"])


def test_the_comparison_is_with_the_same_rules_not_with_history(db):
    """Факт прожит по старым правилам (безубыток после TP1). Строка 1.0
    проигрыша — стоп остатка на TP1 — от факта отличается, от себя — нет.
    Остаток, закрытый стопом на TP1, платит недостачу исполнения стопа (здесь
    из настройки, 0.05): 0.5·1.0 + 0.5·0.95 − 0.14 = 0.835 против 0.07."""
    _sig(db, traj=[[0, 0.0], [10, 1.2], [20, 0.07]], net=0.07, exit_pct=0.07, mfe=1.2)

    full = _row(build(db), 1.0)

    assert full["vs_same_rules_pct"] == 0.0
    assert full["vs_actual_pct"] == pytest.approx(0.835 - 0.07)


def test_a_runner_keeps_its_run_if_it_never_looks_back(db):
    """Прошла новый TP1 и ни разу не вернулась ниже — остаток доезжает до
    фактического выхода (не выше пика)."""
    reached, retest = _replay([0.0, 0.7, 1.4, 2.5], 0.6, 0.05, pessimistic=False)
    assert (reached, retest) == (True, False)


def test_the_pessimistic_bound_counts_the_cross_itself(db):
    """Пересекли 0.6 на 0.62 и ушли вверх: стоп на 0.6 стоит вплотную к цене,
    тик на 0.57 между записями его снимает."""
    assert _replay([0.0, 0.62, 1.4], 0.6, 0.05, pessimistic=False) == (True, False)
    assert _replay([0.0, 0.62, 1.4], 0.6, 0.05, pessimistic=True) == (True, True)
