import json

import pytest

from services.ml_outcome_stats import MLOutcomeStatsService


def test_ml_outcome_stats_summary_reads_jsonl_and_counts_parse_errors(tmp_path):
    path = tmp_path / "trade_outcomes.jsonl"
    rows = [
        {
            "status": "closed",
            "symbol": "BTC/USDT",
            "side": "long",
            "closed_net_pnl": 2.5,
            "closed_total_cost": 0.2,
            "closed_reason": "protective_trailing_stop",
            "labels": {"protected_profit": True},
            "lifecycle": {"mfe_pct": 0.8, "mae_pct": -0.1},
        },
        {
            "status": "closed",
            "symbol": "BTC/USDT",
            "side": "long",
            "closed_net_pnl": -1.0,
            "closed_total_cost": 0.1,
            "closed_reason": "failed_setup_exit",
            "labels": {"positive_then_negative": True},
            "lifecycle": {"mfe_pct": 0.2, "mae_pct": -0.5},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\nnot-json\n", encoding="utf-8")

    summary = MLOutcomeStatsService(path=path).summary()

    assert summary["status"] == "ok"
    assert summary["total"] == 2
    assert summary["parse_errors"] == 1
    assert summary["groups"][0]["key"] == "BTC/USDT:long"
    assert summary["groups"][0]["net_pnl"] == 1.5


def test_ml_outcome_stats_safe_summary_degrades_on_unreadable_path(tmp_path):
    summary = MLOutcomeStatsService(path=tmp_path).safe_summary()

    assert summary["status"] == "degraded"
    assert summary["reason"] == "ml_outcome_stats_failed"


def test_ml_trade_logger_labels_adaptive_mfe_capture_as_protected_profit(tmp_path):
    from types import SimpleNamespace

    from services.ml_trade_logger import MLTradeLogger

    path = tmp_path / "trade_outcomes.jsonl"
    signal = SimpleNamespace(
        id=1,
        bot_id=1,
        symbol="BTC/USDT",
        side="long",
        grade="A",
        confidence=80.0,
        rationale="adaptive capture",
        status="closed",
        closed_reason="adaptive_mfe_capture",
        result_pct=0.35,
        closed_net_pnl=1.2,
        closed_total_cost=0.1,
        created_at=None,
        closed_at=None,
        entry_zone_json={"from": 100.0, "to": 101.0},
        stop_price=99.0,
        tp_json={"tp1": 102.0, "tp2": 104.0},
        qty=1.0,
        required_margin=100.0,
        net_rr_tp1=1.2,
        net_rr_tp2=2.0,
        net_pnl_tp1=2.0,
        net_pnl_tp2=4.0,
        net_pnl_stop=-1.0,
        closed_exit_price=100.35,
        opened_at=None,
        plan_json={"lifecycle": {"mfe_pct": 0.9, "positive_then_negative": False}},
    )

    result = MLTradeLogger(path=str(path)).log_closed_signal(signal)
    row = json.loads(path.read_text(encoding="utf-8").strip())

    assert result["status"] == "logged"
    assert row["closed_reason"] == "adaptive_mfe_capture"
    assert row["labels"]["protected_profit"] is True
    assert row["lifecycle"]["close_reason"] is None


def test_ml_outcome_stats_marks_old_logged_at_as_stale(tmp_path):
    path = tmp_path / "trade_outcomes.jsonl"
    path.write_text(
        json.dumps(
            {
                "status": "closed",
                "symbol": "BTC/USDT",
                "side": "long",
                "closed_net_pnl": 1.0,
                "logged_at": "2026-05-26T05:15:06.601062+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = MLOutcomeStatsService(path=path, stale_hours=72).summary()

    assert summary["status"] == "stale"
    assert summary["freshness_status"] == "stale"
    assert summary["stale"] is True
    assert summary["is_stale"] is True
    assert summary["latest_logged_at"] == "2026-05-26T05:15:06.601062+00:00"
    assert summary["latest_age_hours"] > 72
    assert summary["latest_age_days"] > 3


def test_ml_outcome_stats_keeps_recent_logged_at_fresh(tmp_path):
    from datetime import datetime, timezone

    path = tmp_path / "trade_outcomes.jsonl"
    path.write_text(
        json.dumps(
            {
                "status": "closed",
                "symbol": "ETH/USDT",
                "side": "short",
                "closed_net_pnl": 1.0,
                "logged_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = MLOutcomeStatsService(path=path, stale_hours=72).summary()

    assert summary["status"] == "ok"
    assert summary["freshness_status"] == "fresh"
    assert summary["stale"] is False
    assert summary["latest_age_hours"] <= 1


def test_ml_trade_logger_backfills_unlogged_closed_signals(tmp_path):
    from datetime import datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.db import Base
    from models.signal import Signal
    from services.ml_trade_logger import MLTradeLogger

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[Signal.__table__])
    db = sessionmaker(bind=engine)()
    path = tmp_path / "trade_outcomes.jsonl"

    try:
        db.add_all([
            Signal(
                bot_id=1,
                symbol="TON/USDT",
                side="long",
                status="closed",
                entry_zone_json={"from": 2.0, "to": 2.01},
                stop_price=1.98,
                tp_json={"tp1": 2.04, "tp2": 2.08},
                confidence=80.0,
                rationale="closed paper outcome",
                result_pct=-0.8,
                closed_reason="failed_setup_exit",
                closed_net_pnl=-0.5,
                closed_total_cost=0.1,
                closed_at=datetime.now(timezone.utc),
            ),
            Signal(
                bot_id=1,
                symbol="ETH/USDT",
                side="short",
                status="published",
                entry_zone_json={"from": 100.0, "to": 101.0},
                stop_price=102.0,
                tp_json={"tp1": 98.0, "tp2": 96.0},
                confidence=80.0,
                rationale="not closed yet",
            ),
        ])
        db.commit()

        result = MLTradeLogger(path=path).log_unlogged_closed_signals(db)
        second = MLTradeLogger(path=path).log_unlogged_closed_signals(db)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        assert result["logged"] == 1
        assert result["skipped"] == 0
        assert second["logged"] == 0
        assert second["skipped"] == 1
        assert len(rows) == 1
        assert rows[0]["symbol"] == "TON/USDT"
        assert rows[0]["closed_reason"] == "failed_setup_exit"
    finally:
        db.close()


def _shadow_row(*, score, pnl, net_pnl_stop=-2.0):
    return {
        "status": "closed", "symbol": "BTC/USDT", "side": "long",
        "ml_score": score, "closed_net_pnl": pnl, "net_pnl_stop": net_pnl_stop,
    }


def test_shadow_report_auc_is_measured_against_train_label_not_is_win(tmp_path, monkeypatch):
    """(#audit-2026-08-27) Модель обучена на ML_LABEL_KIND (по умолчанию
    beats_costs: pnl/risk >= ML_LABEL_MIN_R), не на is_win (pnl > 0). Раньше
    shadow_report() мерил AUC только против is_win — целиком другой вопрос.
    Здесь: B — маленький "царапина"-плюс (+0.1 при риске 2.0 → r=0.05 < 0.3)
    с САМЫМ высоким score. Против is_win это "верно предсказанный win" —
    is_win-AUC получается идеальным (1.0). Против beats_costs (реальной цели
    обучения) это ложноположительный высокий score на сделке, которая риск
    не отбила — auc_vs_train_label должен быть заметно ниже и не равен 1.0.
    """
    from core.config import settings
    monkeypatch.setattr(settings, "ML_LABEL_KIND", "beats_costs", raising=False)
    monkeypatch.setattr(settings, "ML_LABEL_MIN_R", 0.3, raising=False)

    path = tmp_path / "trade_outcomes.jsonl"
    rows = [
        _shadow_row(score=0.6, pnl=5.0),    # beats_costs=1, is_win=1
        _shadow_row(score=0.95, pnl=0.1),   # beats_costs=0, is_win=1 (scratch win, highest score)
        _shadow_row(score=0.2, pnl=-3.0),   # beats_costs=0, is_win=0
        _shadow_row(score=0.3, pnl=-1.0),   # beats_costs=0, is_win=0
        _shadow_row(score=0.7, pnl=4.0),    # beats_costs=1, is_win=1
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    report = MLOutcomeStatsService(path=path).shadow_report()

    assert report["status"] == "ok"
    assert report["live_auc"] == pytest.approx(1.0)  # is_win: perfectly separable by construction
    assert report["live_auc_vs_train_label"] is not None
    assert report["live_auc_vs_train_label"] < 1.0  # beats_costs: B corrupts the ranking
    assert report["live_auc_vs_train_label"] == pytest.approx(2 / 3, abs=0.01)
    assert report["train_label_kind"] == "beats_costs"


def test_shadow_report_net_pnl_and_gate_benefit_are_real_dollars(tmp_path):
    """(#audit-2026-08-27) net_pnl_usdt в бакетах был TODO-заглушкой (всегда
    0.0), ml_gate_benefit_usdt — формулой `(winrate_delta/100)*count*10`, не
    связанной с реальными деньгами. Теперь оба — настоящие суммы closed_net_pnl."""
    path = tmp_path / "trade_outcomes.jsonl"
    rows = [
        _shadow_row(score=0.6, pnl=5.0),
        _shadow_row(score=0.95, pnl=0.1),
        _shadow_row(score=0.2, pnl=-3.0),
        _shadow_row(score=0.3, pnl=-1.0),
        _shadow_row(score=0.7, pnl=4.0),
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    report = MLOutcomeStatsService(path=path).shadow_report()

    # threshold defaults to ML_MIN_SCORE_TO_TRADE=0.45: taken={.6,.95,.7}=9.1, skipped={.2,.3}=-4.0
    assert report["threshold_impact"]["taken_net_usdt"] == pytest.approx(9.1)
    assert report["threshold_impact"]["skipped_net_usdt"] == pytest.approx(-4.0)
    # gate benefit = taken_net - total_net(5.1) = 4.0, not the old fabricated formula
    assert report["threshold_impact"]["ml_gate_benefit_usdt"] == pytest.approx(4.0)
    total_bucket_net = sum(b["net_pnl_usdt"] for b in report["buckets"])
    assert total_bucket_net == pytest.approx(5.1)
