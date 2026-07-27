"""Трендовый replay: инварианты честности и защита от подгонки
(#backtest-trend-2026-07-27).

Инструмент существовал с #audit-traj, но брал только `trade_mode in (scalp,
range)` — то есть молча пропускал ВЕСЬ трендовый контур. На боевой выборке
#264–282 это 16 сделок из 18: течь искали там, где её не видно.

Замер, ради которого профиль и добавлен (боевые траектории #264–282):

    MIN_PROTECTIVE 1.80 (было)  -> −2.88% суммарно
    MIN_PROTECTIVE 0.40 (стало) -> −0.10% суммарно

Порог 1.80% не давал сработать ярусам 2–3, и сделка с пиком 1.38% сваливалась
на замок безубытка с фиксацией +0.07%.
"""
from __future__ import annotations

import pytest

from services.exit_replay import _replay_trend_one, _split_check

BASE = dict(
    be_arm=0.35, be_floor=0.10,
    band_arm=0.40, band_give=0.25, band_floor=0.30,
    ride_arm=0.80, ride_trail=0.50,
    min_protective=0.40,
)


def traj(*pcts):
    """Траектория [[сек, %от входа], ...] с шагом в минуту."""
    return [[i * 60, p] for i, p in enumerate(pcts)]


def test_exit_is_booked_at_market_not_at_protective_level():
    """Главный инвариант: replay не может исполниться лучше рынка.

    Тот же принцип, что и в #phantom-fill — именно его нарушение делало
    историю прибыльной на бумаге.
    """
    t = traj(0.0, 0.5, 1.2, 0.55)
    pct, reason = _replay_trend_one(t, -9.9, **BASE)

    assert reason == "replay_trend_trail"
    assert pct == 0.55, "выход книжится по текущей точке траектории"
    assert pct <= max(p[1] for p in t), "результат не выше пика — стоп не лучше рынка"


def test_replay_can_only_close_earlier_than_fact():
    """Правило не сработало — берём результат удержания, а не выдуманный."""
    t = traj(0.0, 0.05, 0.08, 0.10)
    pct, reason = _replay_trend_one(t, 0.10, **BASE)

    assert reason == "actual_close"
    assert pct == 0.10


def test_min_protective_180_drops_trade_onto_breakeven():
    """Воспроизведение боевого механизма потолка прибыли.

    Пик 1.38%, порог защиты 1.80% — ярусы 2–3 молчат, остаётся замок.
    """
    t = traj(0.0, 0.5, 1.38, 0.9, 0.4, 0.05)

    blocked, reason_blocked = _replay_trend_one(t, -0.2, **{**BASE, "min_protective": 1.80})
    allowed, reason_allowed = _replay_trend_one(t, -0.2, **{**BASE, "min_protective": 0.40})

    assert reason_blocked == "replay_breakeven"
    assert blocked <= 0.10, f"при пороге 1.80 сделка падает на безубыток: {blocked}"

    assert reason_allowed == "replay_trend_trail"
    assert allowed > blocked, "снятие порога обязано дать больше на той же траектории"
    # Трейл 50% от пика 1.38 → уровень защиты 0.69. Точка 0.9 ещё выше него,
    # первая точка на/ниже — 0.4, по ней и книжится выход.
    assert allowed == pytest.approx(0.4)


def test_late_breakeven_arm_lets_the_trade_live():
    """Замок на 0.35% убивает сделку до того, как ярус 2 успеет сработать."""
    t = traj(0.0, 0.36, 0.05, 0.60, 0.44)

    early, r_early = _replay_trend_one(t, 0.44, **{**BASE, "be_arm": 0.35})
    late, r_late = _replay_trend_one(t, 0.44, **{**BASE, "be_arm": 1.00})

    assert r_early == "replay_breakeven" and early == pytest.approx(0.05)
    assert r_late != "replay_breakeven"
    assert late > early


def test_losing_trade_is_not_rescued_by_replay():
    """Сделка, которая не была в плюсе, обязана остаться убытком."""
    t = traj(0.0, -0.3, -0.7, -1.05)
    pct, reason = _replay_trend_one(t, -1.05, **BASE)

    assert reason == "actual_close"
    assert pct == -1.05


def test_split_check_flags_a_variant_that_wins_only_on_one_half():
    """Лидер, выигрывающий за счёт одной половины, должен быть помечен."""
    trades = [{"i": i} for i in range(40)]

    def run(subset):
        # «Вариант A» выигрывает только на второй половине выборки.
        first = subset[0]["i"] < 20
        return [
            {"name": "A", "total_pct": 1.0 if not first else -5.0},
            {"name": "B", "total_pct": 0.0},
            {"name": "C", "total_pct": 0.5},
        ]

    res = _split_check(trades, run, {"name": "A"}, lambda v: v["name"])

    assert res["checked"] is True
    assert res["robust"] is False
    assert "подгонка" in res["verdict"]
    assert res["first_half"]["leader_rank"] == 3
    assert res["second_half"]["leader_rank"] == 1


def test_split_check_confirms_a_stable_leader():
    trades = [{"i": i} for i in range(40)]

    def run(subset):
        return [{"name": "A", "total_pct": 2.0}, {"name": "B", "total_pct": 0.1}]

    res = _split_check(trades, run, {"name": "A"}, lambda v: v["name"])

    assert res["robust"] is True
    assert res["first_half"]["leader_rank"] == 1
    assert res["second_half"]["leader_rank"] == 1


def test_split_check_refuses_to_judge_a_tiny_sample():
    """Честнее отказаться от вердикта, чем выдать его на 8 сделках."""
    res = _split_check([{"i": i} for i in range(8)], lambda s: [], {}, lambda v: v)

    assert res["checked"] is False
    assert "бессмысленно" in res["reason"]
