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
