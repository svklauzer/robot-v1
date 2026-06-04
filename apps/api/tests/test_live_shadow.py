from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.signal import Signal
from services.live_shadow import LiveShadowDriftService


class _Market:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self, symbol: str):
        return {"symbol": symbol, **self._snapshot}


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[Signal.__table__])
    return sessionmaker(bind=engine)()


def _signal(side: str = "long", status: str = "published") -> Signal:
    return Signal(
        bot_id=1,
        symbol="BTC/USDT",
        side=side,
        status=status,
        entry_zone_json={"from": 100.0, "to": 100.0},
        stop_price=99.0,
        tp_json={"tp1": 102.0, "tp2": 104.0},
        confidence=80.0,
        rationale="test",
    )


def test_live_shadow_drift_flags_long_entry_above_threshold(monkeypatch):
    monkeypatch.setattr("services.live_shadow.settings.LIVE_SHADOW_SLIPPAGE_PCT", 0.0)
    signal = _signal("long")
    service = LiveShadowDriftService(_Market({"last": 100.8, "bid": 100.7, "ask": 100.8, "source": "htx"}))

    result = service.evaluate_signal(signal, max_drift_pct=0.35)

    assert result["ok"] is False
    assert result["reason"] == "entry_drift_exceeds_threshold"
    assert result["entry_drift_pct"] == 0.8
    assert result["executable_entry"] == 100.8


def test_live_shadow_report_summarizes_active_signal_drift(monkeypatch):
    monkeypatch.setattr("services.live_shadow.settings.LIVE_SHADOW_SLIPPAGE_PCT", 0.0)
    db = _db_session()
    try:
        db.add(_signal("short"))
        db.commit()

        service = LiveShadowDriftService(_Market({"last": 99.8, "bid": 99.8, "ask": 99.9, "source": "htx"}))
        report = service.report(db, limit=5, max_drift_pct=0.35)

        assert report["status"] == "ok"
        assert report["signals_checked"] == 1
        assert report["drift_count"] == 0
        assert report["items"][0]["ok"] is True
        assert report["items"][0]["entry_drift_pct"] == 0.2
    finally:
        db.close()
