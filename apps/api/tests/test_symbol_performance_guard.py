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
