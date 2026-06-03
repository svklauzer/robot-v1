from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.bot import Bot
from models.signal import Signal
from models.user import User
from services.outcome_diagnostics import OutcomeDiagnosticsService


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__, Bot.__table__, Signal.__table__])
    Session = sessionmaker(bind=engine)
    return Session()


def _signal(bot_id, symbol, side, reason, net, grade="A", result_pct=-0.7, lifecycle=None):
    return Signal(
        bot_id=bot_id,
        symbol=symbol,
        side=side,
        status="closed",
        entry_zone_json={"from": 100.0, "to": 101.0},
        stop_price=99.0,
        tp_json={"tp1": 102.0, "tp2": 104.0},
        confidence=80.0,
        rationale="test",
        grade=grade,
        result_pct=result_pct,
        closed_reason=reason,
        closed_net_pnl=net,
        closed_total_cost=0.25,
        plan_json={"lifecycle": lifecycle or {}},
    )


def test_failed_setup_root_cause_groups_by_symbol_and_recommends_guards():
    db = _db_session()
    try:
        user = User(email="owner@example.com", password_hash="hash")
        db.add(user)
        db.flush()
        bot = Bot(user_id=user.id, name="Main Robot", status="running", mode="paper", config_json={})
        db.add(bot)
        db.flush()

        db.add_all([
            _signal(bot.id, "SOL/USDT", "short", "failed_setup_exit", -1.2, lifecycle={
                "positive_then_negative": True,
                "mfe_pct": 0.7,
                "mae_pct": -0.3,
                "missed_profit_pct": 0.9,
            }),
            _signal(bot.id, "SOL/USDT", "short", "failed_setup_exit", -0.8, lifecycle={
                "positive_then_negative": True,
                "mfe_pct": 0.5,
                "mae_pct": -0.2,
                "missed_profit_pct": 0.7,
            }),
            _signal(bot.id, "BTC/USDT", "long", "protective_breakeven_profit_guard", 0.2, result_pct=0.05),
        ])
        db.flush()

        report = OutcomeDiagnosticsService().root_cause(db, reason="failed_setup_exit", limit=50)

        assert report["target_count"] == 2
        assert report["target_share_pct"] == 66.67
        assert report["worst_symbols"][0]["key"] == "SOL/USDT"
        assert report["metrics"]["positive_then_negative_rate"] == 100.0
        assert any("failed_setup_exit" in item for item in report["recommendations"])
        assert any("partial/trailing" in item for item in report["recommendations"])
    finally:
        db.close()
