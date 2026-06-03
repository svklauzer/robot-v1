from types import SimpleNamespace

from services.symbol_performance_guard import SymbolPerformanceGuard


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return _FakeQuery(self._rows)


def _row(pnl: float, reason: str):
    return SimpleNamespace(closed_net_pnl=pnl, closed_reason=reason, plan_json={"lifecycle": {}})


def test_cooldown_on_failed_setup_streak(monkeypatch):
    monkeypatch.setattr("services.symbol_performance_guard.settings.SYMBOL_PERF_COOLDOWN_STREAK", 3)
    monkeypatch.setattr("services.symbol_performance_guard.settings.SYMBOL_PERF_COOLDOWN_FAILED_SETUPS", 4)

    rows = [
        _row(-1.0, "failed_setup_exit"),
        _row(-1.2, "failed_setup_exit"),
        _row(-0.8, "failed_setup_exit"),
        _row(-0.7, "failed_setup_exit"),
        _row(0.3, "protective_breakeven_profit_guard"),
    ]

    decision = SymbolPerformanceGuard().analyze(_FakeDB(rows), bot_id=1, symbol="TON/USDT", lookback=12)

    assert decision.allowed is False
    assert decision.reason == "symbol_cooldown_failed_setup_streak"
    assert decision.failed_setup_count == 4


def test_robot_loop_applies_symbol_performance_risk_multiplier_to_trade_plan():
    from workers.robot_loop import RobotLoop

    plan = SimpleNamespace(
        qty=10.0,
        required_margin=100.0,
        net_pnl_tp1=12.0,
        net_pnl_tp2=24.0,
        net_pnl_stop=-8.0,
    )
    performance = SimpleNamespace(
        allowed=True,
        reason="symbol_gives_back_profit_reduce_risk",
        risk_multiplier=0.5,
        symbol="TON/USDT",
    )

    adjustment = RobotLoop()._apply_symbol_performance_adjustment(plan, performance)

    assert plan.qty == 5.0
    assert plan.required_margin == 50.0
    assert plan.net_pnl_tp1 == 6.0
    assert plan.net_pnl_tp2 == 12.0
    assert plan.net_pnl_stop == -4.0
    assert adjustment["classification"] == "reduced"
    assert adjustment["risk_multiplier"] == 0.5
    assert adjustment["original"]["qty"] == 10.0
    assert adjustment["adjusted"]["qty"] == 5.0


def test_symbol_performance_guard_exposes_watch_only_policy_profile():
    performance = SimpleNamespace(
        allowed=True,
        reason="symbol_gives_back_profit_reduce_risk",
        risk_multiplier=0.6,
        symbol="SOL/USDT",
        closed_count=6,
        wins=6,
        losses=0,
        winrate=100.0,
        total_net_pnl=3.0,
        stop_loss_count=0,
        failed_setup_count=0,
        positive_then_negative_count=3,
        last_closed_reason="protective_breakeven_profit_guard",
        losing_streak=0,
    )

    profile = SymbolPerformanceGuard().policy_profile(performance)

    assert profile["profile"] == "watch_only"
    assert profile["publish_allowed"] is True
    assert profile["risk_multiplier"] == 0.6
    assert profile["min_confidence_delta"] == 10
    assert profile["min_rr_delta"] == 0.25
    assert profile["exit_bias"] == "earlier_mfe_capture"


def test_robot_loop_symbol_policy_requires_watch_only_extra_confidence_and_rr():
    from workers.robot_loop import RobotLoop

    policy_profile = {
        "profile": "watch_only",
        "publish_allowed": True,
        "risk_multiplier": 0.6,
        "min_confidence_delta": 10,
        "min_rr_delta": 0.25,
        "side_restriction": "both_sides_reduced_risk",
        "exit_bias": "earlier_mfe_capture",
    }
    gate_payload = {
        "effective_confidence": 76.0,
        "net_rr_tp1": 1.1,
        "net_rr_tp2": 1.7,
        "thresholds": {
            "min_confidence": 70.0,
            "min_rr_tp1": 0.9,
            "min_rr_tp2": 1.35,
        },
    }

    decision = RobotLoop()._check_symbol_policy_profile(policy_profile, gate_payload)

    assert decision["allowed"] is False
    assert decision["reason"] == "symbol_policy_confidence_too_low"
    assert decision["required_confidence"] == 80.0

    gate_payload["effective_confidence"] = 82.0
    gate_payload["net_rr_tp1"] = 1.2
    gate_payload["net_rr_tp2"] = 1.7
    decision = RobotLoop()._check_symbol_policy_profile(policy_profile, gate_payload)

    assert decision["allowed"] is True
    assert decision["required_rr_tp1"] == 1.15
    assert decision["required_rr_tp2"] == 1.6
