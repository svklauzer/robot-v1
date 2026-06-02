from services.candidate_funnel import build_candidate_funnel_diagnosis


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
