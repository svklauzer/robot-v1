from services.exit_policy import ExitPolicyService
from core.config import settings


def test_before_tp1_failed_setup_does_not_fire_before_absolute_mfe_and_age():
    svc = ExitPolicyService()

    no_age = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=99.6,
        stop_price=98.0,
        mfe_pct=0.7,
        signal_age_sec=None,
        symbol=None,
    )
    low_mfe = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=99.6,
        stop_price=98.0,
        mfe_pct=0.1,
        signal_age_sec=600,
        symbol=None,
    )

    assert no_age.exit is False
    assert low_mfe.exit is False


def test_before_tp1_failed_setup_exit_triggers_after_strict_age_and_real_mfe():
    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=99.55,
        stop_price=98.0,
        mfe_pct=0.55,
        signal_age_sec=600,
        symbol=None,
    )

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
    assert decision.exit_price >= 101.2
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
    try:
        settings.MFE_CAPTURE_ENABLED = True
        settings.MFE_CAPTURE_DRAWDOWN_PCT = 0.30
        settings.MFE_CAPTURE_PROTECT_SHARE = 0.40

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
