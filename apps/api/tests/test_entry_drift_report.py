"""Цена входа: цель против факта (#entry-drift-2026-09-19).

Первая версия этого отчёта считала, что бумага книжит вход по цене зоны и
получает фору, которой не будет в live. Проверка по коду это опровергла:
`signal_lifecycle` держит сигнал в `published`, ждёт, пока ТЕКУЩАЯ цена попадёт
в коридор зоны, и открывает позицию по ней же — одинаково в бумаге и в live.

Что осталось верным и важным: цена входит в коридор с одной стороны, поэтому
факт входа систематически оказывается у дальней от цели границы. Ровно это и
забрал бы лимитный ордер по цене цели.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
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


def _signal(db, *, side="long", mode="limit_wall", target=1.4097, mid=1.4110,
            entry=1.4110, qty=100.0, net_pnl=1.0, closed_hours_ago=1.0, round_trip=0.12,
            order_type=None):
    zone_base = target or entry
    db.add(Signal(bot_id=1, symbol="XRP/USDT", side=side, status="closed", exchange="okx",
                  entry_zone_json={"from": zone_base * 0.999, "to": zone_base * 1.001},
                  stop_price=entry * 0.98, tp_json={"tp1": entry * 1.02, "tp2": entry * 1.05},
                  qty=qty, closed_net_pnl=net_pnl, result_pct=0.5,
                  closed_at=datetime.now(timezone.utc) - timedelta(hours=closed_hours_ago),
                  plan_json={
                      "entry_zone_plan": ({"mode": mode, "entry_price": target,
                                           **({"order_type": order_type} if order_type else {})}
                                          if mode else None),
                      "entry_depth": {"mid": mid},
                      "lifecycle": {"entry_price": entry},
                      "config": {"market": {"round_trip_pct": round_trip}},
                  }))
    db.commit()


def test_long_entering_above_the_target_is_reported_as_worse():
    """Лонг падает к коридору сверху и входит у верхней границы — хуже цели."""
    db = _db()
    _signal(db, side="long", target=1.4097, entry=1.4110)

    overall = report(db)["overall"]

    # (1.4097 − 1.4110) / 1.4097 = −0.092%
    assert overall["entry_vs_target_pct"] == pytest.approx(-0.0922, abs=1e-3)


def test_short_entering_below_the_target_is_worse_too():
    """У шорта выгода противоположна по знаку цены, но смысл тот же."""
    db = _db()
    _signal(db, side="short", target=1.4110, entry=1.4097)

    assert report(db)["overall"]["entry_vs_target_pct"] == pytest.approx(-0.0921, abs=1e-3)


def test_limit_gain_is_what_the_target_price_would_have_added():
    db = _db()
    # Номинал 141.1; вход хуже цели на 0.0922% → выигрыш ≈ 0.130 USDT.
    _signal(db, side="long", target=1.4097, entry=1.4110, qty=100.0)

    overall = report(db)["overall"]

    assert overall["limit_gain_usdt"] == pytest.approx(0.130, abs=0.005)
    assert overall["limit_gain_per_trade_usdt"] == overall["limit_gain_usdt"]


def test_waiting_for_the_corridor_is_measured_against_the_planning_mid():
    """Отдельная величина: что уже даёт ожидание коридора. Она достаётся и
    бумаге, и live, и с переходом на лимит никуда не денется."""
    db = _db()
    _signal(db, side="long", mid=1.4200, entry=1.4110, target=1.4097)

    assert report(db)["overall"]["entry_vs_mid_pct"] == pytest.approx(0.634, abs=1e-2)


def test_modes_are_split_and_market_entries_have_no_target():
    db = _db()
    _signal(db, mode="limit_wall", target=1.4097, entry=1.4110)
    _signal(db, mode="market", target=None, entry=1.4110)

    result = report(db)

    assert result["by_mode"]["limit_wall"]["entry_vs_target_pct"] is not None
    assert result["by_mode"]["market"]["entry_vs_target_pct"] is None
    assert result["overall"]["zone_market_trades"] == 1
    assert result["overall"]["zone_moved_trades"] == 1


def test_maker_saving_is_shown_next_to_the_gain():
    """Переход на лимит даёт две вещи сразу: цену цели и мейкерскую ставку."""
    db = _db()
    _signal(db)

    overall = report(db)["overall"]

    assert overall["maker_saving_pct"] == pytest.approx(0.03, abs=1e-6)
    assert overall["avg_round_trip_pct"] == 0.12


def test_note_does_not_claim_a_paper_bonus():
    """Прежний текст утверждал, что у бумаги есть фора. Её нет: вход
    открывается по текущей цене и в бумаге, и в live."""
    db = _db()
    _signal(db)

    note = report(db)["note"]

    assert "фора" not in note.lower()
    assert "в бумаге, и в live" in note


def test_window_cuts_off_older_trades():
    db = _db()
    _signal(db, closed_hours_ago=200.0)
    _signal(db, closed_hours_ago=1.0)

    assert report(db)["sample_count"] == 2
    assert report(db, window_hours=168)["sample_count"] == 1


def test_endpoint_is_owner_only():
    router = (Path(__file__).resolve().parents[1] / "routers" / "analytics.py").read_text(encoding="utf-8")
    head = router.split('@router.get("/entry-drift"', 1)[1].split("\n", 1)[0]
    block = router.split('@router.get("/entry-drift"', 1)[1].split("\n@router.", 1)[0]
    assert "require_owner_action" in head
    assert "entry_drift_report" in block


def test_analytics_page_shows_the_gap():
    page = (Path(__file__).resolve().parents[2] / "web" / "app" / "analytics" / "page.tsx").read_text(encoding="utf-8")
    assert "/analytics/entry-drift" in page
    for field in ("entry_vs_target_pct", "entry_vs_mid_pct", "limit_gain_usdt", "maker_saving_pct"):
        assert field in page, field


# ── доля исполнений: чем лимит платит за лучшую цену ────────────────────────
def _published(db, *, status="published", reason=None, hours_ago=1.0):
    db.add(Signal(bot_id=1, symbol="XRP/USDT", side="long", status=status, exchange="okx",
                  entry_zone_json={"from": 1.40, "to": 1.41}, stop_price=1.38,
                  tp_json={"tp1": 1.43, "tp2": 1.45}, qty=100.0, closed_reason=reason,
                  created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
                  plan_json={"entry_zone_plan": {"mode": "limit_wall", "entry_price": 1.4097}}))
    db.commit()


def test_fill_rate_separates_unfilled_limits_from_other_expiries():
    """Лимит платит за цену тем, что исполняется не всегда. Без этой доли
    переход на лимит нечем оценивать: выигрыш на цене может не покрыть
    потерянные сделки."""
    db = _db()
    _signal(db)                                              # дошла до сделки
    _published(db, status="expired", reason="limit_not_filled")
    _published(db, status="expired", reason="entry_zone_not_reached_before_expiry")
    _published(db, status="published")                       # ещё ждёт

    fill = report(db)["fill_rate"]

    assert fill["signals"] == 4 and fill["reached_entry"] == 1
    assert fill["limit_not_filled"] == 1 and fill["expired_other"] == 1
    assert fill["still_waiting"] == 1
    assert fill["fill_rate_pct"] == 25.0


def test_report_says_which_entry_type_produced_the_numbers():
    """Цифры отчёта относятся к способу входа, который стоял в тот момент."""
    db = _db()
    _signal(db)

    assert report(db)["entry_order_type"] in ("market", "limit")


# ── настройка «limit» и вход лимитом — разные вещи (#sync-reverted-the-entry-type-2026-09-20) ──
def test_a_moved_target_is_not_the_same_as_a_limit_order(monkeypatch):
    """19–20.09 отчёт показал 85% «лимитных» сделок, войдя по рынку все 27:
    имена режимов зоны начинаются с limit_, и их приняли за тип ордера."""
    monkeypatch.setattr(settings, "ENTRY_ORDER_TYPE", "market")
    db = _db()
    _signal(db, mode="limit_wall", target=1.4097, entry=1.4110, order_type="market")

    overall = report(db)["overall"]

    assert overall["zone_moved_trades"] == 1      # цель зона перенесла
    assert overall["entered_by_limit_trades"] == 0  # а вошли по рынку
    assert overall["market_order_trades"] == 1


def test_trades_planned_before_the_deploy_are_not_evidence(monkeypatch):
    """Признак типа входа пишется при СОЗДАНИИ сигнала: у всего, что было
    запланировано до деплоя, его нет. Это «судить не по чему», а не «лимит не
    работает» — путать одно с другим значит чинить исправное."""
    monkeypatch.setattr(settings, "ENTRY_ORDER_TYPE", "limit")
    db = _db()
    _signal(db, mode="limit_wall", target=1.4097, entry=1.4110)   # без order_type

    result = report(db)

    assert result["overall"]["order_type_unknown_trades"] == 1
    assert "планировались" in result["warning"]
    assert "не доехала" not in result["warning"]


def test_the_config_says_limit_but_nothing_entered_by_limit(monkeypatch):
    """Ровно та тишина, которую нечем было заметить: настройка включена, а до
    процесса не доехала — sync blueprint вернул ключ к записанному."""
    monkeypatch.setattr(settings, "ENTRY_ORDER_TYPE", "limit")
    db = _db()
    _signal(db, mode="limit_wall", target=1.4097, entry=1.4110, order_type="market")

    result = report(db)

    assert "не вошла лимитом" in result.get("warning", "")
    assert "не доехала" in result["warning"]


def test_a_real_limit_entry_is_counted_as_one(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_ORDER_TYPE", "limit")
    db = _db()
    _signal(db, mode="limit_wall", target=1.4097, entry=1.4097, order_type="limit")

    overall = report(db)["overall"]

    assert overall["entered_by_limit_trades"] == 1
    # Вошли ровно по цели — признак того, что лимит действительно работал.
    assert overall["entry_vs_target_pct"] == 0.0


def test_a_market_zone_never_counts_as_a_limit_entry(monkeypatch):
    """При mode == market цели нет, и ордер уходит рыночным при любой настройке."""
    monkeypatch.setattr(settings, "ENTRY_ORDER_TYPE", "limit")
    db = _db()
    _signal(db, mode="market", target=None, entry=1.4110, order_type="limit")

    assert report(db)["overall"]["entered_by_limit_trades"] == 0
