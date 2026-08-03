"""Контракт условий входа по ТЗ в режиме наблюдения (#tz-shadow-2026-08-03).

Проверяется то, ради чего это добавлено: ADX ловит отсутствие движения,
Stoch RSI ловит вход не в откате, OBV ловит поток против. И то, что механизм
безопасен: он ничего не блокирует и не путает «нет данных» с «не прошёл».
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import tz_entry_shadow as tz


def _tf(adx=30.0, plus_di=25.0, minus_di=10.0, k=20.0, d=15.0,
        obv=100.0, obv_ema=50.0):
    return {
        "adx14": adx, "plus_di": plus_di, "minus_di": minus_di,
        "stoch_rsi_k": k, "stoch_rsi_d": d,
        "obv": obv, "obv_ema20": obv_ema,
    }


def _tfs(**kw):
    ctx = _tf(**kw)
    return {"1h": ctx, "15m": ctx, "4h": ctx, "5m": ctx}


def _long(**kw):
    return tz.evaluate(_tfs(**kw), regime="trend_up_candidate", side="long")


def _short(**kw):
    return tz.evaluate(_tfs(**kw), regime="trend_down_candidate", side="short")


# ── ADX: есть ли движение вообще ────────────────────────────────────────────
def test_strong_trend_in_pullback_passes():
    assert _long().would_pass is True


def test_weak_trend_is_flagged():
    """Порог ТЗ: ниже 23 движения нет, входить не во что.

    Раньше меры силы в системе не было вовсе — `price > ema20 > ema50`
    одинаково истинно и в импульсе, и в вялом дрейфе.
    """
    result = _long(adx=15.0)
    assert result.would_pass is False
    assert any(f.startswith("adx_below_min") for f in result.failed)


def test_di_against_side_is_flagged():
    result = _long(plus_di=10.0, minus_di=25.0)
    assert result.would_pass is False
    assert any(f.startswith("di_against_side") for f in result.failed)


def test_short_uses_mirrored_di():
    assert _short(plus_di=10.0, minus_di=25.0, k=80.0, d=85.0,
                  obv=50.0, obv_ema=100.0).would_pass is True


# ── Stoch RSI: точка входа ──────────────────────────────────────────────────
def test_long_outside_pullback_zone_is_flagged():
    """Вход в растяжении: %K высоко, отката не было.

    Это и есть отсутствующая точка входа — сейчас зона строится как
    `last ±0.3%`, то есть цена в момент прихода скана.
    """
    result = _long(k=75.0, d=70.0)
    assert result.would_pass is False
    assert any(f.startswith("stoch_not_in_pullback") for f in result.failed)


def test_long_requires_k_above_d():
    result = _long(k=20.0, d=25.0)
    assert result.would_pass is False
    assert "stoch_k_below_d" in result.failed


def test_short_zone_is_mirrored():
    assert any(f.startswith("stoch_not_in_pullback")
               for f in _short(k=25.0, d=30.0, plus_di=10.0, minus_di=25.0).failed)


# ── OBV: направление объёма ─────────────────────────────────────────────────
def test_obv_below_ema_blocks_long():
    result = _long(obv=10.0, obv_ema=50.0)
    assert result.would_pass is False
    assert "obv_below_ema" in result.failed


def test_missing_obv_is_not_a_failure():
    """Отсутствие индикатора — не то же самое, что «условие не выполнено»."""
    ctx = _tf()
    ctx.pop("obv")
    ctx.pop("obv_ema20")
    result = tz.evaluate({"1h": ctx, "15m": ctx}, regime="trend_up_candidate", side="long")
    assert result.would_pass is True
    assert result.obv_vs_ema is None


# ── границы применимости и безопасность ─────────────────────────────────────
@pytest.mark.parametrize("regime", ["crt", "scalp", "range", "reversal_long_candidate"])
def test_non_trend_regimes_are_not_evaluated(regime):
    result = tz.evaluate(_tfs(), regime=regime, side="long")
    assert result.evaluated is False
    assert result.would_pass is None


@pytest.mark.parametrize("timeframes", [None, {}, {"1h": {}}, {"15m": _tf()}])
def test_missing_data_is_not_a_verdict(timeframes):
    """Нет данных → evaluated=False, would_pass=None. Не False."""
    result = tz.evaluate(timeframes, regime="trend_up_candidate", side="long")
    assert result.would_pass is None


def test_result_is_serialisable_for_the_plan():
    payload = _long(adx=15.0).as_dict()
    assert isinstance(payload["failed"], list)
    assert payload["evaluated"] is True
    assert payload["would_pass"] is False
    assert set(payload) == {"regime", "side", "evaluated", "would_pass", "failed",
                            "adx", "di_spread", "stoch_k", "stoch_d", "obv_vs_ema"}


def test_thresholds_come_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "TZ_ADX_MIN", 40.0, raising=False)
    assert _long(adx=30.0).would_pass is False
    monkeypatch.setattr(settings, "TZ_ADX_MIN", 10.0, raising=False)
    assert _long(adx=30.0).would_pass is True
