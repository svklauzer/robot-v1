"""Качество входа в разрезе способа входа (#entry-mode-quality-2026-09-19).

Замер форы 19.09 показал, что рыночные входы теряют (−22.31 USDT на 34 сделках),
а перенесённые зарабатывают. По одному PnL нельзя отличить «вход хуже по
существу» от «цена исполнения хуже»: перенесённый вход получает фору, которой у
рыночного нет. Ответ дают MFE/MAE — они про ход, который рынок дал ПОСЛЕ входа,
и от цены подарка не зависят.
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
from routers import analytics as an


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__, Bot.__table__, Signal.__table__])
    maker = sessionmaker(bind=engine)
    session = maker()
    session.add_all([User(email="owner@example.com", password_hash="x"),
                     Bot(user_id=1, name="Main Robot", status="running", mode="paper")])
    session.commit()

    # Эндпоинт открывает и закрывает свою сессию — отдаём ту же, не закрывая.
    class _Session:
        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            pass

    monkeypatch.setattr(an, "SessionLocal", lambda: _Session())
    return session


def _signal(db, *, mode="limit_wall", drift=0.2, regime="trend_up_candidate",
            mfe=1.0, mae=-0.5, result=0.3, qty=100.0, entry=2.0, net_pnl=1.0):
    db.add(Signal(bot_id=1, symbol="XRP/USDT", side="long", status="closed", exchange="okx",
                  entry_zone_json={"from": entry * 0.999, "to": entry * 1.001},
                  stop_price=entry * 0.98, tp_json={"tp1": entry * 1.02, "tp2": entry * 1.05},
                  qty=qty, closed_net_pnl=net_pnl, result_pct=result,
                  closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
                  plan_json={
                      "regime": regime,
                      "entry_zone_plan": ({"mode": mode, "drift_pct": drift} if mode else None),
                      "execution": {"mode": "paper"},
                      "lifecycle": {"entry_price": entry, "mfe_pct": mfe, "mae_pct": mae},
                  }))
    db.commit()


def _mode(result, name):
    return next(item for item in result["by_entry_mode"] if item["entry_mode"] == name)


def test_entry_modes_are_reported_with_mfe_and_mae(db_factory):
    """Главное в разрезе — edge_ratio: он отвечает, хуже ли сам вход."""
    _signal(db_factory, mode="market", drift=0.0, mfe=0.4, mae=-0.8)
    _signal(db_factory, mode="limit_wall", drift=0.2, mfe=1.2, mae=-0.4)

    result = an.analytics_mfe_mae(limit=500)

    assert _mode(result, "market")["edge_ratio"] == 0.5
    assert _mode(result, "limit_wall")["edge_ratio"] == 3.0


def test_result_is_reported_per_bucket(db_factory):
    """Сравнивать режимы входа надо по ходу и по доле номинала. Прежняя версия
    вычитала отсюда «фору бумаги» — величину, которой не существует: вход
    открывается по текущей цене и в бумаге, и в live."""
    _signal(db_factory, mode="limit_wall", drift=0.5, qty=100.0, entry=2.0, net_pnl=1.0)

    bucket = _mode(an.analytics_mfe_mae(limit=500), "limit_wall")

    assert bucket["net_pnl_usdt"] == 1.0
    assert bucket["avg_notional_usdt"] == 200.0
    assert "edge_usdt" not in bucket and "net_pnl_without_edge_usdt" not in bucket


def test_result_is_normalised_by_notional(db_factory):
    """Рыночные сделки крупнее, и сравнивать их в USDT напрямую нельзя."""
    _signal(db_factory, mode="market", drift=0.0, qty=100.0, entry=2.0, net_pnl=-1.0)

    bucket = _mode(an.analytics_mfe_mae(limit=500), "market")

    assert bucket["avg_notional_usdt"] == 200.0
    assert bucket["net_pnl_per_notional_pct"] == -0.5


def test_trades_without_an_entry_plan_are_not_called_market(db_factory):
    """Сделки старше зоны входа плана не несут. Записать их в рыночные значило
    бы приписать им способ входа, которого тогда не существовало."""
    _signal(db_factory, mode=None, drift=0.0)

    modes = {item["entry_mode"] for item in an.analytics_mfe_mae(limit=500)["by_entry_mode"]}

    assert modes == {"unknown"}


def test_entry_mode_is_crossed_with_regime(db_factory):
    """Разрыв может объясняться составом: market чаще в одном режиме. Кросс
    показывает, хуже ли он ВЕЗДЕ."""
    _signal(db_factory, mode="market", drift=0.0, regime="crt", net_pnl=-2.0)
    _signal(db_factory, mode="market", drift=0.0, regime="trend_up_candidate", net_pnl=-1.0)
    _signal(db_factory, mode="limit_wall", drift=0.1, regime="crt", net_pnl=1.0)

    items = an.analytics_mfe_mae(limit=500)["by_entry_mode_regime"]
    keys = {(i["entry_mode"], i["regime"]) for i in items}

    assert keys == {("market", "crt"), ("market", "trend_up_candidate"), ("limit_wall", "crt")}


def test_analytics_page_shows_the_entry_mode_split():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "web" / "app" / "analytics" / "page.tsx").read_text(encoding="utf-8")
    assert "by_entry_mode" in page
    for field in ("entry_mode", "edge_ratio", "net_pnl_per_notional_pct"):
        assert field in page, field
