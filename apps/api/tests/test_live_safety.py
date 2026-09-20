from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
from core.db import Base
from models.audit_event import AuditEvent
from models.bot import Bot
from models.position import Position
from models.signal import Signal
from models.user import User
from services.live_safety import LiveSafetyService


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__, Bot.__table__, Signal.__table__,
                                                  AuditEvent.__table__, Position.__table__])
    Session = sessionmaker(bind=engine)
    return Session()


def _create_running_bot(db):
    user = User(email="owner@example.com", password_hash="hash")
    db.add(user)
    db.flush()
    bot = Bot(user_id=user.id, name="Main Robot", status="running", mode="paper", config_json={})
    db.add(bot)
    db.flush()
    return bot


def _closed_signal(bot_id: int, net_pnl: float):
    return Signal(
        bot_id=bot_id,
        symbol="BTC/USDT",
        side="long",
        status="closed",
        entry_zone_json={"min": 100, "max": 101},
        stop_price=99,
        tp_json={"tp1": 102, "tp2": 103},
        confidence=80,
        rationale="test",
        closed_at=datetime.now(timezone.utc),
        closed_net_pnl=net_pnl,
    )


def test_daily_loss_circuit_breaker_stops_running_bot_and_audits():
    db = _db_session()
    old_equity = settings.RISK_EQUITY_USDT
    old_max_loss = settings.MAX_DAILY_LOSS_PCT

    try:
        settings.RISK_EQUITY_USDT = 1000
        settings.MAX_DAILY_LOSS_PCT = 3
        bot = _create_running_bot(db)
        db.add(_closed_signal(bot.id, net_pnl=-50))
        db.flush()

        state = LiveSafetyService().enforce(db, bot)
        db.commit()

        assert state["daily_loss_blocked"] is True
        assert state["daily_loss_pct"] == 5.0
        assert state["action_taken"] == "bot_stopped_by_risk"
        assert bot.status == "stopped_by_risk"
        assert db.query(AuditEvent).filter(AuditEvent.action == "daily_loss_circuit_breaker").count() == 1
    finally:
        settings.RISK_EQUITY_USDT = old_equity
        settings.MAX_DAILY_LOSS_PCT = old_max_loss
        db.close()


def test_owner_kill_switch_updates_bot_config_and_blocks_snapshot():
    db = _db_session()

    try:
        bot = _create_running_bot(db)
        state = LiveSafetyService().set_kill_switch(db, bot, enabled=True, reason="maintenance")
        db.commit()

        assert state["blocked"] is True
        assert state["kill_switch_enabled"] is True
        assert state["kill_switch_reason"] == "maintenance"
        assert bot.status == "stopped_by_owner"
        assert bot.config_json["kill_switch_enabled"] is True
        assert db.query(AuditEvent).filter(AuditEvent.action == "kill_switch_enabled").count() == 1
    finally:
        db.close()


def test_kill_switch_smoke_exercises_enable_disable_and_can_be_rolled_back():
    db = _db_session()

    try:
        bot = _create_running_bot(db)
        db.commit()
        result = LiveSafetyService().kill_switch_smoke(db, bot, reason="smoke_test")

        assert result["status"] == "ok"
        assert result["dry_run"] is True
        assert result["checks"]["enabled_blocks"] is True
        assert result["checks"]["enabled_sets_flag"] is True
        assert result["checks"]["running_bot_stopped"] is True
        assert result["checks"]["disabled_clears_flag"] is True
        assert result["checks"]["disabled_clears_blocker"] is True
        assert db.query(AuditEvent).filter(AuditEvent.action == "kill_switch_enabled").count() == 1
        assert db.query(AuditEvent).filter(AuditEvent.action == "kill_switch_disabled").count() == 1

        db.rollback()
        db.refresh(bot)
        assert bot.status == "running"
        assert bot.config_json == {}
        assert db.query(AuditEvent).count() == 0
    finally:
        db.close()


# ── плавающий убыток входит в лимит (#breaker-sees-only-closed-2026-09-20) ──
def _open_position(db, *, pnl: float, bot_id: int = 1):
    """bot_id по умолчанию совпадает с ботом из _create_running_bot."""
    db.add(Position(bot_id=bot_id, symbol="XRP/USDT", side="long", qty=100.0,
                    entry_price=1.4, mark_price=1.3, unrealized_pnl=pnl, status="open"))
    db.commit()


def test_the_breaker_counts_the_floating_loss_too(monkeypatch):
    """Предохранитель видел только закрытые сделки, и открытый минус копился
    молча: 20.09 лимит 3% поймал факт на 5.97%. Порог обещал одно, а стоил
    «лимит + весь открытый риск»."""
    monkeypatch.setattr(settings, "MAX_DAILY_LOSS_PCT", 6)
    monkeypatch.setattr(settings, "RISK_EQUITY_USDT", 300.0)
    db = _db_session()
    bot = _create_running_bot(db)
    _open_position(db, pnl=-20.0)   # 6.67% от 300 — ещё ни одной закрытой сделки

    state = LiveSafetyService().snapshot(db, bot)

    assert state["unrealized_net_pnl_usdt"] == -20.0
    assert state["realized_net_pnl_usdt"] == 0.0
    assert state["daily_loss_blocked"] is True


def test_a_floating_profit_does_not_mask_a_realized_loss(monkeypatch):
    """Обратная сторона: плавающий плюс не должен отменять уже понесённый
    убыток — но и вычитать его из лимита нечестно, пока он не зафиксирован.
    Считаем суммарно: это ответ на вопрос «сколько потеряю, закрывшись сейчас»."""
    monkeypatch.setattr(settings, "MAX_DAILY_LOSS_PCT", 6)
    monkeypatch.setattr(settings, "RISK_EQUITY_USDT", 300.0)
    db = _db_session()
    bot = _create_running_bot(db)
    _open_position(db, pnl=+5.0)

    state = LiveSafetyService().snapshot(db, bot)

    assert state["daily_net_pnl_usdt"] == 5.0
    assert state["daily_loss_blocked"] is False


def test_positions_of_another_bot_are_not_counted(monkeypatch):
    monkeypatch.setattr(settings, "MAX_DAILY_LOSS_PCT", 6)
    monkeypatch.setattr(settings, "RISK_EQUITY_USDT", 300.0)
    db = _db_session()
    bot = _create_running_bot(db)
    _open_position(db, pnl=-50.0, bot_id=99)

    state = LiveSafetyService().snapshot(db, bot)

    assert state["unrealized_net_pnl_usdt"] == 0.0
