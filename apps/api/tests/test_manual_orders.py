"""Ручные ордера владельца на тех же биржах (#manual-orders-2026-09-16).

Владелец торгует руками на OKX/HTX, где работает робот. Требование: ручные
ордера и позиции робот не трогает, а для него капитал — свободная маржа (биржа
уже вычла ручные) плюс плечо из конфига. Здесь — опознавание ордеров робота и
капитал; стопы и закрытие — в test_exchange_stop / test_live_close_safety.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.config import settings
from services.exposure_guard import ExposureGuard
from services.htx_client import HTXClient
from services.live_executor import LiveExecutor
from services.okx_client import OKXClient
from services.robot_orders import (
    alnum_client_id, is_robot_client_id, is_robot_order, numeric_client_id, order_client_id,
)


# ── какие ордера — робота ──────────────────────────────────────────────────────
def test_robot_ids_are_recognised_on_both_exchanges():
    for _ in range(500):
        okx_id = OKXClient.make_client_order_id("trend_open")
        htx_id = HTXClient.make_client_order_id("trend_open")
        assert is_robot_client_id(okx_id) and okx_id.isalnum() and len(okx_id) <= 32
        assert is_robot_client_id(htx_id) and htx_id.isdigit()
        assert int(htx_id) < 2 ** 53 and float(int(htx_id)) == int(htx_id), \
            "номер HTX искажается при переводе ccxt через float"


@pytest.mark.parametrize("value", [None, "", "e847386590ce4dBC67a0fe4c", "1512501029504577536",
                                   "770", "7701234567890123", "12345678901234", "AA03022abc3a55e8"])
def test_manual_or_foreign_ids_are_not_the_robot(value):
    assert is_robot_client_id(value) is False


@pytest.mark.parametrize("order, expected", [
    ({"clientOrderId": alnum_client_id("x")}, True),
    ({"clientOrderId": None, "info": {"clOrdId": alnum_client_id("x")}}, True),
    ({"clientOrderId": None, "info": {"algoClOrdId": alnum_client_id("x")}}, True),
    ({"clientOrderId": None, "info": {"client_order_id": int(numeric_client_id())}}, True),
    ({"clientOrderId": None, "info": {"algo_client_order_id": numeric_client_id()}}, True),
    ({"clientOrderId": None, "info": {"clOrdId": ""}}, False),
    ({"id": "1", "info": {}}, False),
])
def test_robot_order_detection_reads_every_exchange_field(order, expected):
    assert is_robot_order(order) is expected
    if expected:
        assert order_client_id(order)


# ── капитал робота ─────────────────────────────────────────────────────────────
class _Balances:
    def __init__(self, free: dict[str, float], unified=False):
        self.free = free
        self.calls: list[str] = []
        if unified:
            self.UNIFIED_TRADING_ACCOUNT = True

    def fetch_balance(self, params=None):
        account = (params or {}).get("type")
        self.calls.append(account)
        return {"USDT": {"free": self.free[account], "total": self.free[account] + 500}}


def _executor(monkeypatch, client, *, futures_execution=True):
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    monkeypatch.setattr(settings, "LIVE_SIZE_FROM_BALANCE", True)
    monkeypatch.setattr(settings, "ENABLE_FUTURES", True)
    monkeypatch.setattr(settings, "ENABLE_FUTURES_EXECUTION", futures_execution)
    ex = LiveExecutor.__new__(LiveExecutor)
    ex.client = client
    ex._bal_cache = {}
    return ex


def test_capital_is_free_margin_plus_the_robot_positions(monkeypatch):
    """Свободно 600 (ручные позиции владельца уже вычтены биржей), позиции робота
    держат 400 — капитал робота 1000, а не 600."""
    ex = _executor(monkeypatch, _Balances({"swap": 600.0, "spot": 300.0}))
    assert ex.effective_equity_usdt(robot_margin_usdt=400.0) == pytest.approx(1000.0)


def test_swap_execution_does_not_count_the_htx_spot_account(monkeypatch):
    client = _Balances({"swap": 600.0, "spot": 300.0})
    ex = _executor(monkeypatch, client)
    assert ex.effective_equity_usdt() == pytest.approx(600.0)
    assert client.calls == ["swap"]


def test_okx_trading_account_is_not_summed_twice(monkeypatch):
    client = _Balances({"swap": 600.0, "spot": 600.0}, unified=True)
    ex = _executor(monkeypatch, client, futures_execution=False)
    assert ex.execution_accounts() == ["swap"]
    assert ex.effective_equity_usdt() == pytest.approx(600.0)


def test_htx_spot_longs_count_both_accounts(monkeypatch):
    ex = _executor(monkeypatch, _Balances({"swap": 600.0, "spot": 300.0}), futures_execution=False)
    assert ex.execution_accounts() == ["spot", "swap"]
    assert ex.effective_equity_usdt() == pytest.approx(900.0)


def test_paper_capital_is_unchanged(monkeypatch):
    ex = _executor(monkeypatch, _Balances({"swap": 600.0, "spot": 300.0}))
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "dry_run"))
    monkeypatch.setattr(settings, "RISK_EQUITY_USDT", 3000.0)
    assert ex.effective_equity_usdt(robot_margin_usdt=400.0) == 3000.0


def _signal(status, mode, margin, partial=None):
    plan = {"execution": {"mode": mode}} if mode else {}
    if partial:
        plan["tp1_partial"] = partial
    return SimpleNamespace(status=status, plan_json=plan, required_margin=margin)


def test_only_positions_opened_on_the_exchange_add_margin(monkeypatch):
    signals = [
        _signal("opened", "live", 200.0),
        _signal("tp1", "live", 100.0, partial={"closed_qty": 50, "remaining_qty": 50}),
        _signal("published", "live", 300.0),        # ещё не вошли — маржи на бирже нет
        _signal("opened", "paper", 250.0),          # из paper — на бирже не существует
        _signal("breakeven", None, 150.0),
    ]
    monkeypatch.setattr(ExposureGuard, "active_signals", lambda self, db, bot_id: signals)

    assert ExposureGuard().live_position_margin(None, 1) == pytest.approx(250.0)


def test_a_live_fill_drops_the_cached_balance(monkeypatch):
    from tests.test_live_margin_mode import SYMBOL, _Client

    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_PCT", 0.0)
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 0.0)
    ex = LiveExecutor.__new__(LiveExecutor)
    ex.client = _Client()
    ex._leverage_set = set()
    ex._account_state = None
    ex._bal_cache = {"swap": (600.0, 10 ** 12)}

    res = ex.place_market(SYMBOL, "sell", 150.0, market_type="swap", margin_mode="isolated",
                          leverage=1, reference_price=1.4, purpose="trend_open")

    assert res.ok and ex._bal_cache == {}


def test_the_loop_passes_the_robot_margin_into_capital():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert source.count("to_thread(effective_equity_usdt, robot_live_margin_usdt(db, bot))") == 2
    assert "to_thread(effective_equity_usdt)" not in source


# ── нет баланса в live — нет сделок (#no-paper-equity-in-live-2026-09-17) ───────
class _NoBalance(_Balances):
    def fetch_balance(self, params=None):
        raise TimeoutError("okx down")


def test_live_without_a_readable_balance_has_no_capital(monkeypatch):
    ex = _executor(monkeypatch, _NoBalance({}))
    monkeypatch.setattr(settings, "RISK_EQUITY_USDT", 3000.0)

    assert ex.effective_equity_usdt(robot_margin_usdt=50.0, strict=True) is None
    assert ex.effective_equity_usdt(strict=False) == 3000.0, "бумажные симуляции сохраняют fallback"


def test_live_empty_account_is_zero_not_paper_equity(monkeypatch):
    ex = _executor(monkeypatch, _Balances({"swap": 0.0}))
    monkeypatch.setattr(settings, "RISK_EQUITY_USDT", 3000.0)

    assert ex.effective_equity_usdt(strict=True) == 0.0


def test_loop_equity_is_none_in_live_without_balance(monkeypatch):
    import main
    from services import live_executor as live_module

    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    monkeypatch.setattr(live_module.LIVE_EXECUTOR, "effective_equity_usdt",
                        lambda **_k: None)
    assert main.effective_equity_usdt(0.0) is None

    def boom(**_k):
        raise RuntimeError("client broken")

    monkeypatch.setattr(live_module.LIVE_EXECUTOR, "effective_equity_usdt", boom)
    assert main.effective_equity_usdt(0.0) is None, "в live исключение не должно превращаться в бумажный капитал"


def test_paper_loop_equity_is_unchanged(monkeypatch):
    import main

    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "dry_run"))
    monkeypatch.setattr(settings, "RISK_EQUITY_USDT", 3000.0)
    assert main.effective_equity_usdt(0.0) == 3000.0


def test_lifecycle_does_not_open_without_live_balance(monkeypatch):
    from services.signal_lifecycle import SignalLifecycleManager
    from services import live_executor as live_module

    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    monkeypatch.setattr(ExposureGuard, "live_position_margin", lambda self, db, bot_id: 0.0)
    manager = SignalLifecycleManager.__new__(SignalLifecycleManager)
    for value in (None, 0.0):
        monkeypatch.setattr(live_module.LIVE_EXECUTOR, "effective_equity_usdt", lambda value=value, **_k: value)
        assert manager._equity_usdt(object(), SimpleNamespace(id=1)) is None


def test_the_robot_loop_skips_entries_before_live_safety_without_balance():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    loop = source.split("async def background_robot_loop", 1)[1].split("\nasync def ", 1)[0]
    skip = loop.index('reason="loop_skip_live_balance"')
    assert skip < loop.index("await loop.step(")
    assert "if equity_usdt else {}" in loop, "live-safety не должен считаться от пустого капитала"
