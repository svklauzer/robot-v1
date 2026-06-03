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
