"""Оценка занятой маржи после TP1-partial (#tp1-partial-margin-2026-07-19).

После частичной фиксации 50% позиции половина маржи реально свободна —
exposure_guard обязан это учитывать, НЕ трогая Signal.required_margin
(от него честно считается result_pct закрытия).
"""

import pytest

from services.exposure_guard import ExposureGuard


class _Sig:
    def __init__(self, required_margin=None, plan_json=None):
        self.required_margin = required_margin
        self.plan_json = plan_json


def test_full_margin_without_partial():
    guard = ExposureGuard()
    assert guard.estimate_signal_margin(_Sig(required_margin=267.5118)) == pytest.approx(267.5118)


def test_margin_halved_after_tp1_partial():
    guard = ExposureGuard()
    sig = _Sig(
        required_margin=267.5118,
        plan_json={"tp1_partial": {"closed_qty": 414.1, "remaining_qty": 414.11, "net_pnl": 1.4077}},
    )
    # Остаток ≈ 50.0006% → занято ≈ половина маржи.
    assert guard.estimate_signal_margin(sig) == pytest.approx(267.5118 * 414.11 / (414.1 + 414.11), rel=1e-6)


def test_broken_partial_falls_back_to_full_margin():
    guard = ExposureGuard()
    # Нет remaining/closed или мусор → консервативно полная маржа.
    for partial in ({}, {"closed_qty": "x"}, {"closed_qty": 1.0, "remaining_qty": 0}, None):
        sig = _Sig(required_margin=100.0, plan_json={"tp1_partial": partial})
        assert guard.estimate_signal_margin(sig) == pytest.approx(100.0)


def test_margin_from_plan_json_with_partial():
    guard = ExposureGuard()
    sig = _Sig(
        required_margin=None,
        plan_json={"required_margin": 200.0, "tp1_partial": {"closed_qty": 1.0, "remaining_qty": 1.0}},
    )
    assert guard.estimate_signal_margin(sig) == pytest.approx(100.0)


def test_fallback_constant_without_any_margin():
    guard = ExposureGuard()
    assert ExposureGuard().estimate_signal_margin(_Sig()) == pytest.approx(325.0)
