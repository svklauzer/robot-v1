"""Гейт межбиржевого арбитража судит по консервативной ставке
(#cross-farb-conservative-2026-08-03).

Замер, из-за которого правка: решение принималось по мгновенному спреду, а он
возвращается к среднему за часы. Реализованная ставка выходила 2–10% от той,
по которой входили (XRP: вход 21.93% годовых, реализовано 0.51%), окупаемость
0.2% комиссий уезжала на 40–150 дней при лимите удержания 14. Результат —
11 закрытых сделок из 11 в минусе.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import cross_funding_arb as cfa


def _item(ann=25.0, direction="short_htx_long_kraken"):
    return {"spread": {"spread_annualized_pct": ann, "direction": direction}}


def _history(conservative=20.0, stability=90.0, avg=22.0,
             dominant="short_htx_long_kraken"):
    return {
        "conservative_ann_pct": conservative,
        "direction_stability_pct": stability,
        "avg_spread_ann_pct": avg,
        "dominant_direction": dominant,
    }


# ── окупаемость ─────────────────────────────────────────────────────────────
def test_payback_does_not_depend_on_notional():
    """Комиссии и carry оба пропорциональны нотионалу — он сокращается.

    Отсюда практический вывод: увеличением размера этот арбитраж не спасти.
    """
    assert cfa.payback_days(20.0, 100.0) == pytest.approx(cfa.payback_days(20.0, 5000.0))


def test_payback_matches_arithmetic():
    # комиссия 0.2% нотионала, carry 20% годовых → 0.2 / (20/365) ≈ 3.65 дня
    assert cfa.payback_days(20.0) == pytest.approx(0.002 / (0.20 / 365), rel=1e-6)


def test_zero_rate_has_no_payback():
    assert cfa.payback_days(0.0) is None


# ── главный сценарий ────────────────────────────────────────────────────────
def test_good_conservative_rate_passes():
    ok, reason = cfa.entry_allowed(_item(), _history(conservative=20.0))
    assert ok
    assert "cons=20.00%" in reason


def test_high_spot_rate_with_weak_history_is_rejected():
    """Тот самый случай XRP: мгновенный спред 21.93%, а по наблюдениям 0.5%."""
    ok, reason = cfa.entry_allowed(_item(ann=21.93), _history(conservative=0.51))
    assert not ok
    assert reason.startswith("conservative_below_min")


def test_negative_conservative_quantile_is_rejected():
    """Отрицательный квантиль = в четверти наблюдений carry шёл ПРОТИВ позиции."""
    ok, reason = cfa.entry_allowed(_item(), _history(conservative=-5.0))
    assert not ok
    assert reason.startswith("conservative_below_min")


def test_payback_must_fit_into_max_hold_with_margin(monkeypatch):
    """Окупиться ровно к принудительному закрытию — выйти в ноль.

    При ставке 9% окупаемость 8.1 дня; с двукратным запасом это 16 дней
    против лимита 14 — вход запрещён, хотя порог ставки пройден.
    """
    monkeypatch.setattr(settings, "CROSS_FARB_MIN_CONSERVATIVE_ANN_PCT", 8.0, raising=False)
    monkeypatch.setattr(settings, "CROSS_FARB_MAX_HOLD_DAYS", 14.0, raising=False)
    monkeypatch.setattr(settings, "CROSS_FARB_PAYBACK_MARGIN", 2.0, raising=False)
    ok, reason = cfa.entry_allowed(_item(), _history(conservative=9.0))
    assert not ok
    assert reason.startswith("payback_too_slow")


def test_longer_hold_admits_slower_payback(monkeypatch):
    monkeypatch.setattr(settings, "CROSS_FARB_MAX_HOLD_DAYS", 30.0, raising=False)
    ok, _reason = cfa.entry_allowed(_item(), _history(conservative=9.0))
    assert ok


# ── прежние условия сохранены ───────────────────────────────────────────────
def test_instant_spread_still_required():
    """Входить в уже схлопнувшийся спред нельзя, даже если история хороша."""
    ok, reason = cfa.entry_allowed(_item(ann=3.0), _history(conservative=20.0))
    assert not ok
    assert reason.startswith("spread_below_min")


def test_unstable_direction_still_rejected():
    ok, reason = cfa.entry_allowed(_item(), _history(stability=59.2))
    assert not ok
    assert reason.startswith("stability_below_min")


def test_direction_mismatch_still_rejected():
    ok, reason = cfa.entry_allowed(_item(direction="short_kraken_long_htx"), _history())
    assert not ok
    assert reason == "current_direction_vs_dominant_mismatch"


def test_no_history_still_rejected():
    ok, reason = cfa.entry_allowed(_item(), None)
    assert not ok
    assert reason == "no_history_for_symbol"


def test_old_aggregate_without_quantile_is_refused_not_guessed():
    """Агрегат без квантиля — отказ, а не молчаливый проход по старой логике."""
    row = _history()
    row.pop("conservative_ann_pct")
    ok, reason = cfa.entry_allowed(_item(), row)
    assert not ok
    assert reason == "no_conservative_estimate"
