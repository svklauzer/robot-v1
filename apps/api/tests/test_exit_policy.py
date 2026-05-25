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
