"""Подтверждение ставки перед входом в funding-arb (#funding-confirm-2026-07-27).

Мотив измерен нашей же историей: TRX 16.5% годовых → −1.65%, XRP 14.2% → 3.65%.
Ставка mean-reverts, а вход открывался по ОДНОМУ замеру и закладывался на
десятки часов удержания. Отсюда и завышение дохода, которое снималось 27.07:
причина глубже формулы начисления — ставка входа принималась за прогноз.
"""
from __future__ import annotations

import json
import time

import pytest

from core.config import settings
from services import funding_rate_history as frh


@pytest.fixture()
def rate_log(tmp_path, monkeypatch):
    path = tmp_path / "funding_rates.jsonl"
    monkeypatch.setattr(settings, "FUNDING_RATE_LOG_PATH", str(path), raising=False)
    return path


def _seed(path, symbol, rates, *, basis=0.0, spacing_sec=3600):
    now = time.time()
    with path.open("w", encoding="utf-8") as f:
        for i, r in enumerate(reversed(rates)):
            f.write(json.dumps({
                "ts": now - i * spacing_sec, "s": symbol, "r": r, "b": basis,
            }) + "\n")


def test_no_history_blocks_the_entry(rate_log):
    """Fail-closed по данным: цена ошибки — позиция на 80 часов под ставку,
    которой уже нет."""
    res = frh.confirm("BTC/USDT", current_rate_pct=0.08, basis_pct=0.02,
                      fee_round_trip_pct=0.5)

    assert res["ok"] is False
    assert "наблюдений 0" in res["reason"]


def test_stable_positive_rate_is_confirmed(rate_log):
    _seed(rate_log, "BTC/USDT", [0.09, 0.10, 0.11, 0.10, 0.09, 0.12, 0.10])

    res = frh.confirm("BTC/USDT", current_rate_pct=0.10, basis_pct=0.02,
                      fee_round_trip_pct=0.5)

    assert res["ok"] is True
    assert res["stability"]["sign_consistency"] == 1.0
    assert res["expected_net_carry_pct"] > 0
    assert res["stressed_net_carry_pct"] > 0


def test_sign_flipping_rate_is_rejected(rate_log):
    """Ставка, менявшая знак, carry не даёт: часть периодов платим мы."""
    _seed(rate_log, "BTC/USDT", [0.12, -0.03, 0.10, -0.05, 0.11, 0.09, -0.02])

    res = frh.confirm("BTC/USDT", current_rate_pct=0.12, basis_pct=0.02,
                      fee_round_trip_pct=0.5)

    assert res["ok"] is False
    assert "знак ставки менялся" in res["reason"]


def test_single_spike_does_not_pass_on_its_own(rate_log):
    """Ровно тот случай, ради которого гейт и вводился.

    Текущий замер прекрасный (0.30%), но история говорит, что это всплеск:
    консервативная ставка по нижнему квартилю издержки не отбивает.
    """
    _seed(rate_log, "BTC/USDT", [0.30, 0.02, 0.01, 0.02, 0.03, 0.01, 0.02])

    res = frh.confirm("BTC/USDT", current_rate_pct=0.30, basis_pct=0.02,
                      fee_round_trip_pct=0.5)

    assert res["ok"] is False
    # Ошибка может быть либо о том, что carry не покрывает издержки,
    # либо о том, что стресс базиса съедает весь carry
    assert ("не покрывает" in res["reason"] or "стресс базиса" in res["reason"]), \
        f"Ожидалась ошибка о недостаточном carry, получено: {res['reason']}"
    assert res["stability"]["max_rate_pct"] == 0.30, "всплеск в истории есть"
    assert res["stability"]["conservative_rate_pct"] <= 0.02, "но решает не он"


def test_confirm_horizon_does_not_collide_with_the_exit_constraint():
    """Регресс на коллизию имён.

    `FUNDING_ARB_MIN_HOLD_PERIODS` уже существует и означает ПРОТИВОПОЛОЖНОЕ —
    «не закрывать раньше N периодов». Первая версия правки переопределила его,
    и проверка молча считала по 3 периодам вместо заданных.
    """
    assert hasattr(settings, "FUNDING_ARB_CONFIRM_HOLD_PERIODS")
    assert settings.FUNDING_ARB_CONFIRM_HOLD_PERIODS != settings.FUNDING_ARB_MIN_HOLD_PERIODS
    # Горизонт окупаемости обязан быть не меньше breakeven: round-trip 0.5% при
    # ставке ~0.05%/период — это ~10 периодов. Иначе гейт неисполним в принципе.
    assert settings.FUNDING_ARB_CONFIRM_HOLD_PERIODS >= 8


def test_basis_stress_can_veto_a_thin_carry(rate_log):
    """Carry положителен, но расширение базиса его съедает."""
    _seed(rate_log, "BTC/USDT", [0.09] * 7)
    old = settings.FUNDING_ARB_BASIS_STRESS_PCT
    try:
        settings.FUNDING_ARB_BASIS_STRESS_PCT = 5.0
        res = frh.confirm("BTC/USDT", current_rate_pct=0.09, basis_pct=0.02,
                          fee_round_trip_pct=0.5)
        assert res["ok"] is False
        assert "стресс базиса" in res["reason"]
    finally:
        settings.FUNDING_ARB_BASIS_STRESS_PCT = old


def test_conservative_rate_is_not_the_mean(rate_log):
    """Считаем по нижнему квартилю, а не по средней: среднюю задирает всплеск."""
    _seed(rate_log, "BTC/USDT", [0.50, 0.05, 0.05, 0.06, 0.05, 0.04, 0.05])
    st = frh.stability("BTC/USDT")

    assert st["mean_rate_pct"] > 0.10, "средняя задрана всплеском"
    assert st["conservative_rate_pct"] <= 0.05, "консервативная — нет"


def test_disabled_flag_restores_previous_behaviour(rate_log):
    old = settings.FUNDING_ARB_CONFIRM_ENABLED
    try:
        settings.FUNDING_ARB_CONFIRM_ENABLED = False
        res = frh.confirm("BTC/USDT", current_rate_pct=0.08, basis_pct=0.02,
                          fee_round_trip_pct=0.5)
        assert res["ok"] is True
    finally:
        settings.FUNDING_ARB_CONFIRM_ENABLED = old
