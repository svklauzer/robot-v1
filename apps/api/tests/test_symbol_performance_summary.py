from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.signal import Signal
from services.symbol_performance_summary import SymbolPerformanceSummaryService


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[Signal.__table__])
    return sessionmaker(bind=engine)()


def _signal(symbol: str, pnl: float, reason: str, idx: int):
    return Signal(
        bot_id=1,
        symbol=symbol,
        side="short",
        status="closed",
        entry_zone_json={"from": 1.0, "to": 1.0},
        stop_price=1.1,
        tp_json={"tp1": 0.9},
        confidence=80,
        rationale="test",
        closed_net_pnl=pnl,
        closed_reason=reason,
        closed_at=datetime.now(timezone.utc),
        plan_json={"lifecycle": {"positive_then_negative": idx % 2 == 0}},
    )


def test_symbol_performance_summary_blocks_negative_expectancy(monkeypatch):
    db = _db_session()
    monkeypatch.setattr("services.symbol_performance_guard.settings.SYMBOL_PERF_BLOCK_MIN_HISTORY", 5)
    monkeypatch.setattr("services.symbol_performance_guard.settings.SYMBOL_PERF_BLOCK_MAX_WINRATE", 40.0)
    monkeypatch.setattr("services.symbol_performance_guard.settings.SYMBOL_PERF_COOLDOWN_FAILED_SETUPS", 99)

    try:
        for idx in range(5):
            db.add(_signal("ADA/USDT", -1.0, "failed_setup_exit", idx))
        db.commit()

        summary = SymbolPerformanceSummaryService().summarize(db, symbols=["ADA/USDT"], lookback=12)

        assert summary["status"] == "ok"
        assert summary["blocked_count"] == 1
        assert summary["items"][0]["classification"] == "blocked"
        assert summary["items"][0]["reason"] == "symbol_negative_expectancy_blocked"
        assert summary["items"][0]["policy_profile"]["profile"] == "blocked"
        assert summary["items"][0]["policy_profile"]["publish_allowed"] is False
        assert summary["items"][0]["policy_profile"]["side_restriction"] == "no_new_client_signals"
        assert "Исключить символ" in summary["items"][0]["action"]
    finally:
        db.close()


def test_symbol_performance_summary_resolves_distinct_closed_symbols():
    db = _db_session()

    try:
        db.add(_signal("BTC/USDT", 1.0, "protective_trailing_stop", 1))
        db.add(_signal("BTC/USDT", 0.5, "protective_trailing_stop", 2))
        db.add(_signal("ETH/USDT", 0.1, "protective_breakeven_profit_guard", 3))
        db.commit()

        summary = SymbolPerformanceSummaryService().summarize(db, lookback=3)

        assert summary["symbols_count"] == 2
        assert {item["symbol"] for item in summary["items"]} == {"BTC/USDT", "ETH/USDT"}
    finally:
        db.close()


def test_symbol_performance_summary_marks_giveback_symbols_as_watch_only(monkeypatch):
    db = _db_session()
    monkeypatch.setattr("services.symbol_performance_guard.settings.SYMBOL_PERF_GIVEBACK_TRIGGER", 3)

    try:
        for idx in range(6):
            signal = _signal("SOL/USDT", 0.5, "protective_breakeven_profit_guard", idx)
            signal.plan_json = {"lifecycle": {"positive_then_negative": idx < 3}}
            db.add(signal)
        db.commit()

        item = SymbolPerformanceSummaryService().summarize(db, symbols=["SOL/USDT"], lookback=6)["items"][0]

        assert item["classification"] == "reduced"
        assert item["reason"] == "symbol_gives_back_profit_reduce_risk"
        assert item["policy_profile"]["profile"] == "watch_only"
        assert item["policy_profile"]["publish_allowed"] is True
        assert item["policy_profile"]["min_confidence_delta"] == 10
        assert item["policy_profile"]["min_rr_delta"] == 0.25
        assert item["policy_profile"]["exit_bias"] == "earlier_mfe_capture"
    finally:
        db.close()
