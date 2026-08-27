from services.candidate_funnel import KNOWN_BLOCKING_DECISIONS, build_candidate_funnel_diagnosis


def test_candidate_funnel_explains_readonly_scan_and_stopped_bot():
    diagnosis = build_candidate_funnel_diagnosis(
        readonly_scan_hits=20,
        bot_running=False,
        ready_candidates=0,
        published_recent=0,
        telegram_failed_signals=0,
        telegram_failed_deliveries=0,
        latest_event_newer_than_signal=True,
        top_blockers=[],
    )

    assert any("readonly" in reason for reason in diagnosis["reasons"])
    assert any("running" in reason for reason in diagnosis["reasons"])
    assert any("POST /intelligence/scan/run" in action for action in diagnosis["actions"])


def test_candidate_funnel_prioritizes_telegram_and_gate_blockers():
    diagnosis = build_candidate_funnel_diagnosis(
        readonly_scan_hits=0,
        bot_running=True,
        ready_candidates=2,
        published_recent=0,
        telegram_failed_signals=1,
        telegram_failed_deliveries=3,
        latest_event_newer_than_signal=False,
        top_blockers=[{"decision": "a_plus_rr_tp1_too_low", "count": 4}],
    )

    assert any("Telegram failures" in reason for reason in diagnosis["reasons"])
    assert any("a_plus_rr_tp1_too_low" in reason for reason in diagnosis["reasons"])
    assert any("ready_to_publish" in reason for reason in diagnosis["reasons"])


def test_known_blocking_decisions_includes_live_gate_codes():
    """(#audit-2026-08-27) top_blockers на /intelligence/funnel фильтрует
    decision_counts через KNOWN_BLOCKING_DECISIONS — список не обновлялся с
    момента добавления (29.05) и молча прятал новые причины блокировок
    (depth-гейт 12.06, переписанный tp_reachability 24.08, trend_trigger's
    extended_from_ema20, symbol_policy_* гейт), из-за чего дашборд показывал
    древний net_rr_too_low вместо реально доминирующих причин. Регрессия:
    эти коды обязаны быть в списке."""
    for decision in (
        "blocked_depth_gate",
        "tp2_reached_too_rarely",
        "extended_from_ema20",
        "extended_from_ema20_shadow",
        "symbol_policy_confidence_too_low",
        "symbol_policy_rr_tp1_too_low",
        "symbol_policy_rr_tp2_too_low",
        "symbol_policy_publish_blocked",
    ):
        assert decision in KNOWN_BLOCKING_DECISIONS, f"{decision} must be in KNOWN_BLOCKING_DECISIONS"
