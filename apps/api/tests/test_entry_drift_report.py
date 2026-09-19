"""Фора лимитного входа (#entry-drift-2026-09-19).

Зона входа переносит сделку к цене ЛУЧШЕ рынка, бумага книжит её как
исполненную, а live шлёт рыночный ордер и пишет фактический филл. Отчёт должен
показывать размер этой разницы — иначе бумажный результат переносят на live как
есть.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.bot import Bot
from models.signal import Signal
from models.user import User
from services.entry_drift_report import report


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__, Bot.__table__, Signal.__table__])
    db = sessionmaker(bind=engine)()
    db.add_all([User(email="owner@example.com", password_hash="x"),
                Bot(user_id=1, name="Main Robot", status="running", mode="paper")])
    db.commit()
    return db


def _signal(db, *, mode="limit_wall", drift=0.2, qty=100.0, entry=2.0, net_pnl=1.0,
            execution="paper", round_trip=0.12, closed_hours_ago=1.0):
    plan = {
        "entry_zone_plan": {"mode": mode, "drift_pct": drift},
        "execution": {"mode": execution},
        "lifecycle": {"entry_price": entry},
        "config": {"market": {"round_trip_pct": round_trip}},
    }
    db.add(Signal(bot_id=1, symbol="XRP/USDT", side="long", status="closed", exchange="okx",
                  entry_zone_json={"from": entry * 0.999, "to": entry * 1.001},
                  stop_price=entry * 0.98, tp_json={"tp1": entry * 1.02, "tp2": entry * 1.05},
                  qty=qty, closed_net_pnl=net_pnl, result_pct=0.5,
                  closed_at=datetime.now(timezone.utc) - timedelta(hours=closed_hours_ago),
                  plan_json=plan))
    db.commit()


def test_limit_entry_edge_is_counted_in_usdt():
    db = _db()
    _signal(db, mode="limit_wall", drift=0.2, qty=100.0, entry=2.0, net_pnl=1.0)

    overall = report(db)["overall"]

    # 200 USDT номинала × 0.2% = 0.4 USDT форы, которой в live не будет.
    assert overall["edge_usdt"] == 0.4
    assert overall["net_pnl_usdt"] == 1.0
    assert overall["net_pnl_without_edge_usdt"] == 0.6
    assert overall["limit_trades"] == 1 and overall["limit_share_pct"] == 100.0


def test_market_entry_has_no_edge():
    db = _db()
    _signal(db, mode="market", drift=0.0, net_pnl=1.0)

    overall = report(db)["overall"]

    assert overall["edge_usdt"] == 0.0
    assert overall["market_trades"] == 1 and overall["limit_trades"] == 0
    assert overall["net_pnl_without_edge_usdt"] == overall["net_pnl_usdt"]


def test_trade_executed_live_keeps_its_real_fill():
    """В live цена в учёте — фактический филл, форы там нет и вычитать нечего."""
    db = _db()
    _signal(db, mode="limit_wall", drift=0.5, execution="live", net_pnl=2.0)

    overall = report(db)["overall"]

    assert overall["edge_usdt"] == 0.0 and overall["live_trades"] == 1
    assert overall["net_pnl_without_edge_usdt"] == 2.0


def test_edge_is_split_by_entry_mode():
    db = _db()
    _signal(db, mode="limit_wall", drift=0.3, qty=100.0, entry=1.0, net_pnl=1.0)
    _signal(db, mode="limit_vwap", drift=0.1, qty=100.0, entry=1.0, net_pnl=1.0)
    _signal(db, mode="market", drift=0.0, qty=100.0, entry=1.0, net_pnl=1.0)

    result = report(db)

    assert result["by_mode"]["limit_wall"]["edge_usdt"] == 0.3
    assert result["by_mode"]["limit_vwap"]["edge_usdt"] == 0.1
    assert result["by_mode"]["market"]["edge_usdt"] == 0.0
    # Средняя фора считается по ВСЕМ сделкам: рыночные входы разбавляют её так
    # же, как разбавляют реальный портфель.
    assert result["overall"]["avg_drift_pct_all_trades"] == round(0.4 / 3, 4)


def test_edge_is_comparable_with_the_round_trip():
    """Фора и стоимость оборота меряются одним и тем же — долей номинала."""
    db = _db()
    _signal(db, mode="limit_wall", drift=0.12, round_trip=0.12)

    result = report(db)

    assert result["overall"]["avg_round_trip_pct"] == 0.12
    assert "round-trip" in result["note"] and "лимитным ордером" in result["note"]


def test_window_cuts_off_older_trades():
    db = _db()
    _signal(db, drift=0.2, closed_hours_ago=200.0)
    _signal(db, drift=0.2, closed_hours_ago=1.0)

    assert report(db)["sample_count"] == 2
    assert report(db, window_hours=168)["sample_count"] == 1


def test_endpoint_is_owner_only():
    router = (Path(__file__).resolve().parents[1] / "routers" / "analytics.py").read_text(encoding="utf-8")
    block = router.split('@router.get("/entry-drift"', 1)[1].split("\n@router.", 1)[0]
    assert "require_owner_action" in router.split('@router.get("/entry-drift"', 1)[1].split("\n", 1)[0]
    assert "entry_drift_report" in block


def test_analytics_page_shows_the_gap():
    page = (Path(__file__).resolve().parents[2] / "web" / "app" / "analytics" / "page.tsx").read_text(encoding="utf-8")
    assert "/analytics/entry-drift" in page
    for field in ("edge_usdt", "net_pnl_without_edge_usdt", "limit_share_pct", "avg_drift_pct_all_trades"):
        assert field in page, field
