"""Стоп остатка после TP1 на уровне, а не в безубытке (#post-tp1-lock-2026-09-11).

Замер /analytics/tp1-overshoot (90 дней, 103 сделки до TP1, первое касание
закрывает): стоп остатка на доле дистанции TP1 против безубытка — 0.75 → +5.4,
1.0 → +10.5 п.п.; трейл 0.4·MFE на тех же сделках −1.1. Здесь — механика: куда
встаёт стоп, когда не встаёт, и что закрытие по нему не выдаёт себя за
безубыток. Значение по умолчанию — выключено: решение владельца.
"""
from __future__ import annotations

import inspect

import pytest

from core.config import settings
from services.decision_config import snapshot
from services.signal_lifecycle import SignalLifecycleManager

_lock = SignalLifecycleManager._post_tp1_lock_stop


def test_the_chosen_level_and_the_trail_it_replaces():
    """Решение 11.09: стоп остатка на самом TP1 (лучшая пессимистичная оценка,
    +9.3 п.п.), трейл 0.4·MFE выключен (−1.1 п.п.). Одно без другого не
    ставится: включённый трейл на высоком пике книжит +0.30% — ниже стопа."""
    assert float(settings.POST_TP1_LOCK_FRAC) == 1.0
    assert settings.POST_TP1_TRAIL_ENABLED is False


def test_long_stop_goes_to_the_share_of_the_tp1_distance():
    # Вход 100, TP1 101, безубыток с комиссией 100.12 → 0.75 даёт 100.75.
    assert _lock("long", 100.0, 101.0, 100.12, 0.75) == pytest.approx(100.75)


def test_short_is_mirrored():
    assert _lock("short", 100.0, 99.0, 99.88, 0.75) == pytest.approx(99.25)


def test_the_full_share_puts_the_stop_on_tp1_itself():
    assert _lock("long", 100.0, 101.0, 100.12, 1.0) == pytest.approx(101.0)


def test_never_past_tp1_even_if_configured_so():
    """Стоп выше TP1 на лонге сработал бы вместе с TP1 — бессмыслица."""
    assert _lock("long", 100.0, 101.0, 100.12, 1.7) == pytest.approx(101.0)


def test_a_small_share_does_not_loosen_the_breakeven():
    """0.05·1% = 100.05 ниже безубытка с комиссией 100.12 — стоп не трогаем."""
    assert _lock("long", 100.0, 101.0, 100.12, 0.05) is None
    assert _lock("short", 100.0, 99.0, 99.88, 0.05) is None


def test_zero_means_the_old_breakeven():
    assert _lock("long", 100.0, 101.0, 100.12, 0.0) is None


# ── связь с ведением ────────────────────────────────────────────────────────

def _src() -> str:
    return inspect.getsource(SignalLifecycleManager)


def test_the_tp1_branch_applies_the_lock():
    assert "self._post_tp1_lock_stop(" in _src()
    assert "POST_TP1_LOCK_FRAC" in _src()


def test_a_lock_close_does_not_pass_for_a_breakeven():
    """Закрытие по уровню — прибыль, безубыток — ноль (его нетто ведение и
    вовсе подставляет 0.0). Смешать их значит спрятать эффект правки."""
    src = _src()
    assert '"post_tp1_lock_stop" if (signal.plan_json or {}).get("post_tp1_lock")' in src


def test_the_rule_is_recorded_in_the_trade_snapshot():
    cfg = snapshot(market_type="swap", fee_rate=0.0005)
    assert "post_tp1_lock_frac" in cfg["exit"]
