from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_background_loops_emit_structured_log_events():
    main = (ROOT / "apps/api/main.py").read_text()

    for event in [
        "subscription_watchdog_check",
        "telegram_delivery_retry",
        "payment_reconciliation",
        "funding_arb_scan",
        "robot_loop_step_completed",
        "robot_loop_safety_skip",
    ]:
        assert event in main

    assert main.count("log_event(") >= 10


def test_robot_loop_has_single_sleep_interval():
    main = (ROOT / "apps/api/main.py").read_text()
    robot_loop = main.split("async def background_robot_loop():", 1)[1].split("def initialize_database_schema", 1)[0]

    assert robot_loop.count("await asyncio.sleep(60)") == 1


def test_robot_loop_checks_validation_gates_before_live_safety():
    main = (ROOT / "apps/api/main.py").read_text()
    robot_loop = main.split("async def background_robot_loop():", 1)[1].split("def initialize_database_schema", 1)[0]

    assert robot_loop.index("ValidationGateService().live_blockers") < robot_loop.index("LiveSafetyService().enforce")
    assert "robot_loop_validation_skip" in robot_loop
    assert "validation_gates_blocked" in main
    assert "bot_start_blocked_by_validation_gates" in main
