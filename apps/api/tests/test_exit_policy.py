from services.exit_policy import ExitPolicyService
from core.config import settings


def test_before_tp1_failed_setup_does_not_fire_before_absolute_mfe_and_age():
    svc = ExitPolicyService()

    # (#tp1-partial-2026-07-09) Тест проверяет ГЕЙТЫ failed_setup — отключаем
    # breakeven-lock, который теперь (намеренно) срабатывает раньше на armed-MFE.
    old_lock = settings.BREAKEVEN_LOCK_ENABLED
    try:
        settings.BREAKEVEN_LOCK_ENABLED = False

        # No age — guard must not fire regardless of loss
        no_age = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=99.6,
            stop_price=98.0,
            mfe_pct=0.7,
            signal_age_sec=None,
            symbol=None,
        )
        # MFE too low (below absolute min 0.50) — guard must not fire
        low_mfe = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=99.6,
            stop_price=98.0,
            mfe_pct=0.1,
            signal_age_sec=600,
            symbol=None,
        )
        # Age below new threshold (599 < 600) — guard must not fire
        young_trade = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=99.6,
            stop_price=98.0,
            mfe_pct=0.55,
            signal_age_sec=599,
            symbol=None,
        )
    finally:
        settings.BREAKEVEN_LOCK_ENABLED = old_lock

    assert no_age.exit is False
    assert low_mfe.exit is False
    assert young_trade.exit is False, "Trade younger than 600s must not trigger failed_setup_exit"


def test_before_tp1_armed_breakeven_lock_fires_on_real_loss():
    """(#leak-be-lock-2026-07-09) Вооружённая сделка (MFE>=arm) в реальном минусе
    (глубже hard floor) обязана закрыться breakeven_lock, не дожидаясь flow."""
    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=99.6,
        stop_price=98.0,
        mfe_pct=0.7,
        signal_age_sec=None,
        symbol=None,
    )

    assert decision.exit is True
    assert decision.reason == "breakeven_lock"


def test_before_tp1_failed_setup_exit_triggers_after_strict_age_and_real_mfe():
    svc = ExitPolicyService()

    old_lock = settings.BREAKEVEN_LOCK_ENABLED
    try:
        settings.BREAKEVEN_LOCK_ENABLED = False
        decision = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=99.55,
            stop_price=98.0,
            mfe_pct=0.55,
            signal_age_sec=600,
            symbol=None,
            # soft/mid failed_setup — под вик-фильтром: нужен подтверждённый
            # разворот потока (EXIT_REQUIRE_FLOW_CONFIRM=True по умолчанию).
            flow_against=True,
        )
    finally:
        settings.BREAKEVEN_LOCK_ENABLED = old_lock

    assert decision.exit is True
    assert decision.reason == "failed_setup_exit"


def test_before_tp1_protective_breakeven_uses_v4_profit_floor():
    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=100.55,
        stop_price=98.5,
        mfe_pct=0.9,
        symbol=None,
    )

    assert decision.exit is True
    assert decision.reason == "protective_breakeven_profit_guard"
    assert decision.exit_price is not None
    # MIN_PROTECTIVE_EXIT_PCT raised to 1.80 — exit price must reflect the new floor
    assert decision.exit_price >= 101.8
    assert "protected" in (decision.note or "")


def test_before_tp1_no_exit_on_healthy_pullback():
    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=100.9,
        stop_price=98.5,
        mfe_pct=1.1,
        symbol=None,
    )

    assert decision.exit is False
    assert decision.reason is None


def test_before_tp1_adaptive_mfe_capture_triggers_before_deep_giveback():
    svc = ExitPolicyService()

    old_enabled = settings.MFE_CAPTURE_ENABLED
    old_drawdown = settings.MFE_CAPTURE_DRAWDOWN_PCT
    old_share = settings.MFE_CAPTURE_PROTECT_SHARE
    old_start = settings.MFE_CAPTURE_START_PCT
    try:
        settings.MFE_CAPTURE_ENABLED = True
        settings.MFE_CAPTURE_DRAWDOWN_PCT = 0.30
        settings.MFE_CAPTURE_PROTECT_SHARE = 0.40
        # Дефолт START_PCT поднят до 1.30 (#expectancy-cleanup) — тест проверяет
        # сам механизм capture, поэтому фиксируем старый порог явно.
        settings.MFE_CAPTURE_START_PCT = 0.90

        decision = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=100.7,
            stop_price=99.0,
            tp1_price=100.7,
            mfe_pct=1.2,
            symbol=None,
        )

        assert decision.exit is True
        assert decision.reason == "adaptive_mfe_capture"
        assert decision.exit_price is not None
        assert "protected" in (decision.note or "")
    finally:
        settings.MFE_CAPTURE_ENABLED = old_enabled
        settings.MFE_CAPTURE_DRAWDOWN_PCT = old_drawdown
        settings.MFE_CAPTURE_PROTECT_SHARE = old_share
        settings.MFE_CAPTURE_START_PCT = old_start


def test_before_tp1_adaptive_mfe_capture_can_be_disabled():
    svc = ExitPolicyService()

    old_enabled = settings.MFE_CAPTURE_ENABLED
    try:
        settings.MFE_CAPTURE_ENABLED = False

        decision = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=100.7,
            stop_price=99.0,
            mfe_pct=1.2,
            symbol=None,
        )

        assert decision.reason != "adaptive_mfe_capture"
    finally:
        settings.MFE_CAPTURE_ENABLED = old_enabled


def test_exit_policy_has_no_stale_exit_pct_reference():
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "services" / "exit_policy.py"

    assert re.search(r"(?<!protective_)\bexit_pct\b", source.read_text()) is None


def test_exit_policy_runtime_guard_reports_protected_pct_runtime():
    guard = ExitPolicyService.runtime_guard()

    assert guard["ok"] is True
    assert guard["runtime"] == "protected_pct_v4"
    assert guard["stale_exit_pct_reference"] is False
