"""Где внутри CRT деньги (#crt-quality-2026-09-20).

CRT — единственный убыточный движок на чистой неделе OKX (16 сделок, −15.48
против +21.66 у остальных), и пять его порогов в проде ослаблены против дефолта.
Выключать движок вслепую нельзя: «CRT теряет» и «теряет определённая часть CRT»
ведут к разным решениям.
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
from services.crt_quality_report import DEFAULT_MIN_SCORE, report


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__, Bot.__table__, Signal.__table__])
    db = sessionmaker(bind=engine)()
    db.add_all([User(email="owner@example.com", password_hash="x"),
                Bot(user_id=1, name="Main Robot", status="running", mode="paper")])
    db.commit()
    return db


def _crt(db, *, score=72.0, mss=False, fvg=True, sweep="CRL", depth=44.8,
         net_pnl=-2.0, mfe=1.0, mae=-0.9, ptn=False, symbol="CHIP/USDT",
         strategy="crt_3candle", qty=100.0, entry=2.0, hours_ago=1.0):
    db.add(Signal(bot_id=1, symbol=symbol, side="long", status="closed", exchange="okx",
                  entry_zone_json={"from": entry * 0.999, "to": entry * 1.001},
                  stop_price=entry * 0.98, tp_json={"tp1": entry * 1.02, "tp2": entry * 1.05},
                  qty=qty, closed_net_pnl=net_pnl, result_pct=0.1,
                  closed_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
                  plan_json={
                      "setup_quality": {"strategy": strategy, "final_score": score,
                                        "mss": mss, "fvg": fvg, "sweep": sweep,
                                        "sweep_depth_pct": depth},
                      "lifecycle": {"entry_price": entry, "mfe_pct": mfe, "mae_pct": mae,
                                    "positive_then_negative": ptn},
                  }))
    db.commit()


def _row(result, axis, key):
    return next(r for r in result[axis] if r["key"] == key)


def test_only_crt_trades_are_counted():
    """Разрез по геометрии CRT не должен разбавляться трендовыми сделками."""
    db = _db()
    _crt(db)
    _crt(db, strategy="mtf_trend")

    assert report(db)["sample_count"] == 1


def test_the_default_score_threshold_is_the_main_axis():
    """Прод пускает от 45, дефолт — от 55. Вопрос ровно в том, отличаются ли
    сделки между этими порогами от остальных."""
    db = _db()
    _crt(db, score=51.0, net_pnl=-3.0)     # прод пускает, дефолт нет
    _crt(db, score=72.0, net_pnl=+2.0)

    result = report(db)
    below = _row(result, "by_score", f"score < {DEFAULT_MIN_SCORE:.0f} (прод пускает, дефолт нет)")
    above = _row(result, "by_score", "score ≥ 70")

    assert below["net_pnl_usdt"] == -3.0 and above["net_pnl_usdt"] == 2.0
    assert result["live_thresholds"]["default_min_setup_score"] == DEFAULT_MIN_SCORE


def test_confirmations_are_split_because_they_are_what_cisd_would_gate():
    db = _db()
    _crt(db, mss=True, fvg=True, net_pnl=1.0)
    _crt(db, mss=False, fvg=True, net_pnl=-2.0)
    _crt(db, mss=False, fvg=False, net_pnl=-1.0)

    result = report(db)
    keys = {r["key"] for r in result["by_confirmation"]}

    assert keys == {"MSS+FVG", "только FVG", "без подтверждения"}
    assert _row(result, "by_confirmation", "MSS+FVG")["net_pnl_usdt"] == 1.0


def test_buckets_are_normalised_by_notional():
    """Бакеты могут отличаться средним размером сделки — в USDT их напрямую
    сравнивать нельзя."""
    db = _db()
    _crt(db, score=72.0, qty=100.0, entry=2.0, net_pnl=-2.0)   # номинал 200

    row = _row(report(db), "by_score", "score ≥ 70")

    assert row["net_pnl_per_notional_pct"] == -1.0


def test_edge_ratio_answers_whether_the_entry_itself_is_bad():
    db = _db()
    _crt(db, mfe=0.8, mae=-0.8, net_pnl=-1.0)

    assert report(db)["overall"]["edge_ratio"] == 1.0


def test_live_thresholds_are_reported_next_to_the_result():
    """Читать разрез без действующих порогов бессмысленно: непонятно, какие из
    этих сделок вообще прошли бы при дефолте."""
    result = report(_db())

    for key in ("min_setup_score", "min_rr_tp1", "require_cisd", "tp2_rr", "stop_buffer_pct"):
        assert key in result["live_thresholds"], key


def test_window_cuts_off_older_trades():
    db = _db()
    _crt(db, hours_ago=200.0)
    _crt(db, hours_ago=1.0)

    assert report(db)["sample_count"] == 2
    assert report(db, window_hours=168)["sample_count"] == 1


def test_endpoint_is_owner_only():
    router = (Path(__file__).resolve().parents[1] / "routers" / "analytics.py").read_text(encoding="utf-8")
    head = router.split('@router.get("/crt-quality"', 1)[1].split("\n", 1)[0]
    assert "require_owner_action" in head
