"""Итог сделки складывается из всех частичных закрытий (#tp2-partial-sum-2026-09-12).

Проверка издержек по этапам 12.09: комиссия входа делится пропорционально —
каждая часть (TP1, TP2, остаток) платит вход и выход на свой объём, вход
оплачивается ровно один раз. Но в итог сделки при закрытии складывалась только
часть с TP1: этап TP2 писал net и издержки в план, и они терялись.
"""
from __future__ import annotations

import inspect

import pytest

from services.signal_lifecycle import SignalLifecycleManager

_parts = SignalLifecycleManager._realized_parts


def test_both_stages_enter_the_total():
    plan = {
        "tp1_partial": {"net_pnl": 1.20, "total_cost": 0.14},
        "tp2_partial": {"net_pnl": 1.05, "total_cost": 0.07},
    }
    net, cost = _parts(plan)
    assert net == pytest.approx(2.25)
    assert cost == pytest.approx(0.21)


def test_only_tp1_is_the_old_behaviour():
    assert _parts({"tp1_partial": {"net_pnl": 1.2, "total_cost": 0.14}}) == (
        pytest.approx(1.2), pytest.approx(0.14))


def test_no_parts_means_nothing_to_add():
    assert _parts({}) == (None, None)


def test_a_broken_part_does_not_poison_the_rest():
    plan = {"tp1_partial": {"net_pnl": "n/a"}, "tp2_partial": {"net_pnl": 0.5}}
    net, _ = _parts(plan)
    assert net == pytest.approx(0.5)


def test_the_close_uses_the_sum_of_all_parts():
    """Ровно эта строка складывала только TP1."""
    src = inspect.getsource(SignalLifecycleManager._close_signal)
    assert "self._realized_parts(" in src
    assert 'get("tp1_partial") or {}' not in src
