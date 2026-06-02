from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.signal import Signal
from services.validation_gates import ValidationGateService


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[Signal.__table__])
    return sessionmaker(bind=engine)()


def _signal(idx: int, *, pnl: float, reason: str = "tp2_reached", positive_then_negative: bool = False) -> Signal:
    return Signal(
        id=idx,
        bot_id=1,
        symbol="BTC/USDT",
        side="long",
        status="closed",
        entry_zone_json={"from": 100, "to": 101},
        stop_price=99,
        tp_json={"tp1": 102, "tp2": 104},
        closed_reason=reason,
        closed_net_pnl=pnl,
        plan_json={"lifecycle": {"positive_then_negative": positive_then_negative}},
    )


def test_validation_gates_pass_when_profit_quality_and_sample_are_met():
    db = _db_session()
    try:
        for idx in range(1, 6):
            db.add(_signal(idx, pnl=2.0, reason="tp2_reached", positive_then_negative=False))
        db.flush()

        result = ValidationGateService(min_closed=5, failed_setup_max_pct=35, positive_then_negative_max_pct=25).evaluate(db)

        assert result["ready"] is True
        assert result["blockers"] == []
        assert result["closed_count"] == 5
        assert result["net_pnl_usdt"] == 10.0
        assert result["gates"]["rolling_net_pnl_positive"] is True
        assert result["gates"]["failed_setup_below_threshold"] is True
        assert result["gates"]["positive_then_negative_below_threshold"] is True
        assert result["gates"]["min_closed_outcomes"] is True
    finally:
        db.close()


def test_validation_gates_block_negative_pnl_bad_reasons_and_small_sample():
    db = _db_session()
    try:
        db.add(_signal(1, pnl=-5.0, reason="failed_setup_exit", positive_then_negative=True))
        db.add(_signal(2, pnl=1.0, reason="tp2_reached", positive_then_negative=False))
        db.flush()

        result = ValidationGateService(min_closed=5, failed_setup_max_pct=35, positive_then_negative_max_pct=25).evaluate(db)

        assert result["ready"] is False
        assert result["closed_count"] == 2
        assert result["net_pnl_usdt"] == -4.0
        assert result["failed_setup_share_pct"] == 50.0
        assert result["positive_then_negative_rate_pct"] == 50.0
        assert "validation rolling net PnL is not positive after costs" in result["blockers"]
        assert "validation failed_setup_exit share is above threshold" in result["blockers"]
        assert "validation positive_then_negative rate is above threshold or missing lifecycle sample" in result["blockers"]
        assert "validation requires at least 200 closed paper/live_shadow outcomes" in result["blockers"]
    finally:
        db.close()
