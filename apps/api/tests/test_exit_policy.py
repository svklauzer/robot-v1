from services.exit_policy import ExitPolicyService
from core.config import settings


def test_before_tp1_failed_setup_exit_triggers_on_soft_rule():
    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=99.7,
        mfe_pct=0.1,
        symbol=None,
    )

    assert decision.exit is True
    assert decision.reason == "failed_setup_exit"


def test_before_tp1_protective_breakeven_guard_triggers_after_good_mfe():
    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=100.44,
        mfe_pct=0.8,
        symbol=None,
    )

    assert decision.exit is True
    assert decision.reason == "protective_breakeven_profit_guard"
    assert decision.exit_price is not None
    assert "protected" in (decision.note or "")


def test_before_tp1_no_exit_on_healthy_pullback():
    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=100.9,
        mfe_pct=1.1,
        symbol=None,
    )

    assert decision.exit is False
    assert decision.reason is None


def test_before_tp1_protective_exit_respects_min_profit_floor_pct():
    svc = ExitPolicyService()

    old_floor = settings.MIN_PROTECTIVE_EXIT_PCT
    try:
        settings.MIN_PROTECTIVE_EXIT_PCT = 0.8

        decision = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=100.44,
            mfe_pct=0.8,
            symbol=None,
        )

        assert decision.exit is True
        assert decision.reason == "protective_breakeven_profit_guard"
        assert decision.exit_price is not None
        assert decision.exit_price >= 100.8
    finally:
        settings.MIN_PROTECTIVE_EXIT_PCT = old_floor


def test_before_tp1_adaptive_mfe_capture_triggers_before_deep_giveback():
    svc = ExitPolicyService()

    old_enabled = settings.MFE_CAPTURE_ENABLED
    old_start = settings.MFE_CAPTURE_START_PCT
    old_drawdown = settings.MFE_CAPTURE_DRAWDOWN_PCT
    old_share = settings.MFE_CAPTURE_PROTECT_SHARE
    try:
        settings.MFE_CAPTURE_ENABLED = True
        settings.MFE_CAPTURE_START_PCT = 0.65
        settings.MFE_CAPTURE_DRAWDOWN_PCT = 0.30
        settings.MFE_CAPTURE_PROTECT_SHARE = 0.35

        decision = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=100.55,
            mfe_pct=0.9,
            symbol=None,
        )

        assert decision.exit is True
        assert decision.reason == "adaptive_mfe_capture"
        assert decision.exit_price is not None
        assert "protected" in (decision.note or "")
    finally:
        settings.MFE_CAPTURE_ENABLED = old_enabled
        settings.MFE_CAPTURE_START_PCT = old_start
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
            current_price=100.55,
            mfe_pct=0.9,
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
    assert guard["runtime"] == "protected_pct_v2"
    assert guard["stale_exit_pct_reference"] is False
