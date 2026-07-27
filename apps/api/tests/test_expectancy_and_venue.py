"""Ожидание вместо win-rate и разрез по площадке
(#expectancy-2026-07-27, #venue-expectancy-2026-07-27).

Почему win-rate плохой критерий — на наших же числах, боевые #264–282:

    win-rate  67%     (выше эталонного копи-трейдера с 65%)
    payoff    0.11    (победа +0.091% против убытка −0.822%)
    ожидание  −0.251% на сделку

Гвард судил по win-rate (`winrate < block_max_winrate`) и такой символ
пропускал: 67 > 40.
"""
from __future__ import annotations

import pytest

from services.expectancy import _row, entry_reason_of
from services.venue_expectancy import fill_audit, market_type_of


class _Sig:
    def __init__(self, sid=1, symbol="ADA/USDT", result_pct=0.0, net=0.0,
                 cost=0.0, mfe=None, traj=None, ptn=False, plan=None):
        self.id = sid
        self.symbol = symbol
        self.result_pct = result_pct
        self.closed_net_pnl = net
        self.closed_total_cost = cost
        lifecycle = {}
        if mfe is not None:
            lifecycle["mfe_pct"] = mfe
        if traj is not None:
            lifecycle["traj"] = traj
        if ptn:
            lifecycle["positive_then_negative"] = True
        self.plan_json = {**(plan or {}), "lifecycle": lifecycle}


def test_high_winrate_can_hide_negative_expectancy():
    """Ровно наша ситуация: много мелких побед, редкие крупные убытки."""
    rows = [_Sig(i, result_pct=0.09, net=0.09) for i in range(8)]
    rows += [_Sig(100 + i, result_pct=-0.82, net=-0.82) for i in range(4)]

    m = _row(rows)

    assert m["winrate_pct"] == pytest.approx(66.67, abs=0.1), "win-rate выглядит хорошо"
    assert m["expectancy_usdt"] < 0, "а ожидание отрицательное"
    assert m["payoff_ratio"] < 0.2, "потому что победы вчетверо мельче убытков"


def test_payoff_ratio_is_what_makes_winrate_readable():
    good = _row([_Sig(1, net=1.0), _Sig(2, net=1.0), _Sig(3, net=-1.0)])
    bad = _row([_Sig(1, net=0.09), _Sig(2, net=0.09), _Sig(3, net=-0.82)])

    assert good["winrate_pct"] == bad["winrate_pct"], "win-rate одинаковый"
    assert good["expectancy_usdt"] > 0 > bad["expectancy_usdt"], "а исход разный"


def test_legacy_entry_reason_is_marked_not_guessed():
    """Старые сделки без причины входа помечаются явно.

    Иначе разрез по причинам выглядел бы заполненным, хотя данных в нём нет.
    """
    old = _Sig(plan={"regime": "trend"})
    new = _Sig(plan={"entry_reason": "trend_volume_breakout_v2"})

    assert entry_reason_of(old) == "legacy_trend"
    assert entry_reason_of(new) == "trend_volume_breakout_v2"


def test_fill_audit_flags_a_result_better_than_the_market_gave():
    """Инвариант, отсутствие которого и породило фантомные филлы."""
    phantom = _Sig(result_pct=1.80, net=8.18, mfe=0.97,
                   traj=[[0, 0.0], [60, 0.97], [120, -0.40]])

    audit = fill_audit(phantom)

    assert audit["invariant_violated"] is True
    assert audit["execution_gap_pct"] > 0, "положительный разрыв = запись лучше рынка"
    assert "ошибка УЧЁТА" in audit["note"]


def test_normal_slippage_is_not_flagged_as_a_bug():
    """Отрицательный разрыв — обычное проскальзывание, это не баг."""
    ok = _Sig(result_pct=0.55, net=0.5, mfe=0.70,
              traj=[[0, 0.0], [60, 0.70], [120, 0.55]])

    audit = fill_audit(ok)

    assert audit["invariant_violated"] is False
    assert audit["execution_gap_pct"] < 0


def test_market_type_is_unknown_rather_than_guessed():
    """Честное «unknown» лучше догадки: swap и spot различаются вчетверо по
    издержкам, и ошибка отнесения искажает вывод сильнее, чем пропуск."""
    assert market_type_of(_Sig(plan={})) == "unknown"
    assert market_type_of(_Sig(plan={"market_type": "swap"})) == "swap"
    assert market_type_of(_Sig(plan={"execution": {"market_type": "spot"}})) == "spot"


def test_cost_share_shows_when_the_problem_is_turnover_not_direction():
    """Если издержки сопоставимы с валовым результатом, проблема в частоте."""
    rows = [_Sig(i, result_pct=0.10, net=0.02, cost=0.08) for i in range(10)]
    m = _row(rows)

    assert m["expectancy_usdt"] > 0
    assert m["cost_share_of_gross_pct"] > 70, "оборот съедает большую часть хода"
