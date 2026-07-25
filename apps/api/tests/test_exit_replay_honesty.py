"""Честность exit-replay (#replay-honesty-2026-07-25).

Две методологические поломки, из-за которых вердикт «текущий конфиг №1 шесть
замеров подряд» недействителен:

1. Базис включал ФАНТОМНЫЕ филлы (result_pct > mfe_pct) — прибыль по цене,
   которой рынок не видел.
2. Fallback варианта брал ФАКТИЧЕСКИЙ результат, произведённый ТЕКУЩИМ
   конфигом → любой вариант, не сработавший раньше, наследовал исход текущего,
   и текущий конфиг структурно не мог проиграть.
"""
from services.exit_replay import _honest_final_pct, _replay_one


def test_phantom_actual_is_replaced_by_market_price():
    """TRX #281: записано +1.80% при MFE 0.9737% и последней точке 0.4802%."""
    traj = [[0, 0.0], [100, 0.5], [200, 0.9737], [300, 0.4802]]
    row = {"lifecycle": {"mfe_pct": 0.9737}}

    pct, is_phantom = _honest_final_pct(row, traj, 1.80)

    assert is_phantom is True
    assert pct == 0.4802, "фантом должен заменяться последней точкой траектории"


def test_normal_close_is_left_untouched():
    traj = [[0, 0.0], [100, 0.6], [200, 0.2]]
    row = {"lifecycle": {"mfe_pct": 0.6}}

    pct, is_phantom = _honest_final_pct(row, traj, 0.2)

    assert is_phantom is False
    assert pct == 0.2


def test_missing_mfe_is_fail_open():
    """Без mfe_pct судить не о чем — оставляем как есть, не выдумываем."""
    pct, is_phantom = _honest_final_pct({}, [[0, 0.0]], 1.5)

    assert is_phantom is False
    assert pct == 1.5


def test_variant_that_never_fires_does_not_inherit_current_config_result():
    """Ключевой инвариант: fallback = переданный честный базис, а не «как
    закрылось на самом деле». Правило с недостижимым arm не должно срабатывать
    и обязано вернуть ровно тот базис, который ему дали."""
    traj = [[0, 0.0], [100, 0.4], [200, 0.3]]
    honest_hold = 0.3

    pct, reason = _replay_one(traj, honest_hold, arm=99.0, give=0.4,
                              ts_min=None, hard_mult=2.0)

    assert reason == "actual_close"
    assert pct == honest_hold


def test_tight_variant_closes_earlier_than_the_hold_result():
    """Тугой вариант обязан закрыться РАНЬШЕ и дать другой (здесь — лучший)
    результат, иначе сравнение конфигов бессмысленно."""
    traj = [[0, 0.0], [100, 1.0], [200, 0.6], [300, -0.5]]

    pct, reason = _replay_one(traj, -0.5, arm=0.5, give=0.4,
                              ts_min=None, hard_mult=2.0)

    assert reason == "replay_breakeven_lock"
    assert pct == 0.6
    assert pct > -0.5
