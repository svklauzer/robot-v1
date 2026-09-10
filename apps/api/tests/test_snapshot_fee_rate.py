"""Ставка в снимке конфига сделки (#snapshot-fee-2026-09-11).

С 02.09 (b12cd8a) у SignalLifecycleManager нет общего `exit_policy`, а снимок
продолжал его звать. AttributeError глотался, и у каждой сделки с 02.09 в
снимке не было ставки, а пол издержек записывался по споту даже для swap.
Обнаружилось только потому, что отчёт после TP1 не смог воспроизвести гейт.
"""
from __future__ import annotations

import inspect
import logging

import workers.robot_loop as robot_loop
from services import exit_policy


class _Client:
    def trading_fee_rates(self, symbol, market_type=None):
        return {"taker": 0.0005, "source": "stub"}


def test_the_fee_comes_from_the_exchange_the_signal_opens_on(monkeypatch):
    seen = {}

    def factory(exchange=None):
        seen["exchange"] = exchange
        return _Client()

    monkeypatch.setattr(exit_policy, "get_exchange_client", factory)

    assert robot_loop._snapshot_fee_rate("LTC/USDT", "swap", "okx") == 0.0005
    assert seen["exchange"] == "okx"


def test_a_failed_lookup_is_logged_not_swallowed(monkeypatch, caplog):
    def broken(exchange=None):
        raise RuntimeError("no client")

    monkeypatch.setattr(exit_policy, "get_exchange_client", broken)

    with caplog.at_level(logging.WARNING):
        assert robot_loop._snapshot_fee_rate("LTC/USDT", "swap", "okx") is None
    assert "config_snapshot_fee_unavailable" in caplog.text


def test_the_loop_does_not_reach_for_the_removed_attribute():
    """Ровно эта строка молча обнуляла ставку девять дней."""
    src = inspect.getsource(robot_loop.RobotLoop)
    assert "lifecycle.exit_policy" not in src
    assert "_snapshot_fee_rate(" in src
