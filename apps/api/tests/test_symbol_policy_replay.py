import json

from services.symbol_policy_replay import SymbolPolicyReplayService


def _closed(signal_id: int, pnl: float, reason: str = "failed_setup_exit", symbol: str = "AVAX/USDT") -> dict:
    return {
        "signal_id": signal_id,
        "bot_id": 1,
        "status": "closed",
        "symbol": symbol,
        "side": "short",
        "closed_net_pnl": pnl,
        "closed_reason": reason,
        "lifecycle": {"positive_then_negative": pnl < 0},
    }


def test_symbol_policy_replay_uses_prior_history_to_skip_blocked_symbol(monkeypatch):
    monkeypatch.setattr("services.symbol_performance_guard.settings.SYMBOL_PERF_BLOCK_MIN_HISTORY", 5)
    monkeypatch.setattr("services.symbol_performance_guard.settings.SYMBOL_PERF_BLOCK_MAX_WINRATE", 40.0)
    monkeypatch.setattr("services.symbol_performance_guard.settings.SYMBOL_PERF_COOLDOWN_FAILED_SETUPS", 99)
    # Probe OFF → guard жёстко скипает убыточный символ (семантика этого теста).
    monkeypatch.setattr("services.symbol_performance_guard.settings.SYMBOL_PERF_PROBE_MULTIPLIER", 0.0)
    rows = [_closed(idx, -1.0) for idx in range(1, 7)]

    report = SymbolPolicyReplayService().replay_rows(rows, lookback=12, sample_limit=10)

    assert report["status"] == "ok"
    assert report["baseline"]["net_pnl"] == -6.0
    assert report["replay"]["published_count"] == 5
    assert report["replay"]["skipped_count"] == 1
    assert report["replay"]["net_pnl"] == -5.0
    assert report["replay"]["avoided_loss_usdt"] == 1.0
    assert report["replay"]["net_pnl_delta"] == 1.0
    assert report["samples"][-1]["would_publish"] is False
    assert report["samples"][-1]["profile"] == "blocked"
    assert report["skipped_by_reason"] == {"symbol_negative_expectancy_blocked": 1}


def test_symbol_policy_replay_path_counts_parse_errors(tmp_path):
    path = tmp_path / "trade_outcomes.jsonl"
    path.write_text(json.dumps(_closed(1, 1.5, reason="adaptive_mfe_capture")) + "\nnot-json\n", encoding="utf-8")

    report = SymbolPolicyReplayService().replay_path(path)

    assert report["status"] == "ok"
    assert report["source_path"] == str(path)
    assert report["total_rows"] == 1
    assert report["closed_rows"] == 1
    assert report["parse_errors"] == 1
    assert report["replay"]["net_pnl"] == 1.5
