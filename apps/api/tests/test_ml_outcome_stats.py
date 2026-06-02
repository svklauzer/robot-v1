import json

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
