from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_background_loops_emit_structured_log_events():
    main = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")

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
    """В теле цикла ровно одна пауза, и её задаёт SCAN_INTERVAL_SEC.

    Два `sleep` в теле — это удвоенный период сканирования, который никак себя
    не проявляет, кроме вдвое меньшего числа сделок. Литерал 60 здесь больше не
    ищем: интервал давно вынесен в настройку, и проверка на константу тихо
    отвалилась вместе с ней.
    """
    main = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
    # Режем строго до СЛЕДУЮЩЕЙ функции: за scan-циклом идут остальные воркеры,
    # и слишком широкий срез считает их паузы как свои.
    robot_loop = main.split("async def background_robot_loop():", 1)[1].split("\nasync def ", 1)[0]

    body = robot_loop.split("while robot_loop_enabled:", 1)[1]

    assert body.count("await asyncio.sleep(") == 1, "в теле scan-цикла должна быть ровно одна пауза"
    assert "SCAN_INTERVAL_SEC" in body, "период сканирования должен приходить из настройки"


def test_robot_loop_checks_validation_gates_before_live_safety():
    main = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
    robot_loop = main.split("async def background_robot_loop():", 1)[1].split("def initialize_database_schema", 1)[0]

    assert robot_loop.index("ValidationGateService().live_blockers") < robot_loop.index("LiveSafetyService().enforce")
    assert "robot_loop_validation_skip" in robot_loop
    assert "validation_gates_blocked" in main
    assert "bot_start_blocked_by_validation_gates" in main


def test_http_request_middleware_adds_request_ids_and_structured_logs():
    main = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")

    assert '@app.middleware("http")' in main
    assert 'request.headers.get("X-Request-ID") or uuid4().hex' in main
    assert 'response.headers["X-Request-ID"] = request_id' in main
    assert '"request_completed"' in main
    assert '"request_error"' in main
    assert 'duration_ms' in main
