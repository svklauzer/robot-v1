"""Замок после TP1 в реплее выходов (#replay-post-tp1-lock-2026-09-19).

В бою достижение TP1 переставляет стоп на долю пути до цели (`POST_TP1_LOCK_FRAC`,
сейчас 1.0 — на сам TP1) и не ниже безубытка с буфером комиссии. Это самая частая
причина закрытия с плюсом (`post_tp1_lock_stop`), а в модели её не было: сделка,
которую бой закрывал замком, здесь доживала до факта или её раньше выбивал
безубыток. Отсюда систематический разрыв, который `fidelity` видел, но объяснял
списком причин, устаревшим ещё до того.
"""
from __future__ import annotations

import pytest

from services import exit_replay as er

LADDER = dict(be_arm=0.3, be_floor=0.1, band_arm=0.4, band_give=0.25, band_floor=0.3,
              ride_arm=0.8, ride_trail=0.5, min_protective=0.4)


def _run(traj, *, final_pct=0.0, tp1=None, frac=0.0, cost=0.0, **over):
    return er._replay_trend_one(traj, final_pct=final_pct, cost_pct=cost,
                                tp1_pct=tp1, post_tp1_lock_frac=frac, **{**LADDER, **over})


def test_lock_closes_the_trade_when_price_comes_back_to_tp1():
    pct, reason = _run([[0, 0.0], [60, 1.2], [120, 0.8]], tp1=1.0, frac=1.0, cost=0.14)

    assert reason == "replay_post_tp1_lock"
    # Книжится по текущей точке, а не по уровню замка: стоп не исполняется
    # лучше рынка — тот же инвариант, что у остальных ярусов.
    assert pct == pytest.approx(0.8 - 0.14)


def test_lock_does_not_fire_on_the_bar_that_set_it():
    """При frac=1.0 уровень равен самому TP1, и проверка на том же баре закрыла
    бы сделку ровно в момент достижения цели — то есть отменила бы TP1."""
    pct, reason = _run([[0, 0.0], [60, 1.0]], final_pct=0.9, tp1=1.0, frac=1.0)

    assert reason == "actual_close" and pct == pytest.approx(0.9)


def test_breakeven_is_silent_under_the_lock():
    """В бою замок ВЫТЕСНЯЕТ безубыток: стоп один, и он выше."""
    traj = [[0, 0.0], [60, 1.2], [120, 0.05]]

    locked, locked_reason = _run(traj, tp1=1.0, frac=1.0)
    without, without_reason = _run(traj, tp1=1.0, frac=0.0)

    assert locked_reason == "replay_post_tp1_lock"
    assert without_reason == "replay_breakeven"
    assert locked == without == pytest.approx(0.05)


def test_lock_never_sits_below_breakeven_with_costs():
    """Малая доля пути дала бы уровень ниже безубытка — в бою он туда не
    опускается (`_post_tp1_lock_stop` не ухудшает уже стоящий стоп)."""
    traj = [[0, 0.0], [60, 1.2], [120, 0.12]]

    pct, reason = _run(traj, tp1=1.0, frac=0.1, cost=0.14)

    assert reason == "replay_post_tp1_lock"
    assert pct == pytest.approx(0.12 - 0.14)


def test_without_targets_the_ladder_behaves_as_before():
    """У сделок из файла логгера геометрии целей нет — замку не на чем стоять,
    и они обязаны считаться ровно как раньше."""
    traj = [[0, 0.0], [60, 1.2], [120, 0.05]]

    assert _run(traj, tp1=None, frac=1.0)[1] == "replay_breakeven"


def test_lock_share_is_live_and_reported(monkeypatch):
    """Доля берётся живая и попадает в отчёт: инструмент обязан говорить, какую
    машину он воспроизводил."""
    import inspect

    source = inspect.getsource(er.build_trend)
    assert 'getattr(settings, "POST_TP1_LOCK_FRAC"' in source
    assert '"post_tp1_lock_frac": partials["post_tp1_lock_frac"]' in source
    assert '"post_tp1_lock"' in source  # ladder в exit_model


def test_verdict_names_what_is_really_missing():
    """Прежний текст перечислял «стоп, tp1/tp2, adaptive-трейл» ещё долго после
    того, как их добавили, — вердикт врал про собственную модель."""
    verdict = er._fidelity_verdict(current_pct=-40.0, actual_pct=-34.0, best_pct=-31.0)

    assert verdict["trustworthy"] is False
    assert "tz_kama" in verdict["verdict"]
    assert "tp1/tp2" not in verdict["verdict"]
    for leg in er.UNMODELLED_TREND_LEGS:
        assert leg in verdict["verdict"]


def test_walk_forward_scores_the_same_ladder():
    """Проверка вне выборки обязана мерить ту же модель, что и перебор: иначе
    её «подтверждение» относится к другой лестнице."""
    import inspect

    source = inspect.getsource(er._score)
    assert "post_tp1_lock_frac=" in source and "tp1_pct=t.get" in source
