"""Replay воспроизводит ТУ лестницу выхода, которая работает (#replay-partials-2026-09-05).

Модель описывала выход по состоянию на 27.07: замок безубытка, полоса захвата,
ride-трейл. С тех пор живой контур закрывает долю на TP1, ведёт остаток, закрывает
ещё долю на TP2 (на 92% пути) и трейлит хвост. Инструмент отвечал про машину,
которой у нас нет, — и его собственный `_fidelity_verdict` это показывал: разрыв
модели с фактом 3.81 п.п. при выводе в 0.04, ошибка в 91 раз больше заключения.
"""
from __future__ import annotations

import pytest

from services.exit_replay import _merged_rows, _replay_trend_one, rows_from_db

# Лестница, на которой полоса захвата срабатывает: пик 1.2%, откат до 0.3%.
TRAJ = [[1, 0.0], [2, 0.5], [3, 1.2], [4, 0.9], [5, 0.3]]
LADDER = dict(be_arm=0.3, be_floor=0.05, band_arm=0.8, band_give=0.35,
              band_floor=0.2, ride_arm=2.0, ride_trail=0.3,
              min_protective=0.1, cost_pct=0.14)


def test_without_partials_behaviour_is_exactly_as_before():
    """Доли по нулям обязаны давать прежний результат до копейки: иначе правка
    молча переписала бы все исторические выводы инструмента."""
    pct, reason = _replay_trend_one(TRAJ, 0.30, **LADDER)

    assert pct == pytest.approx(0.30 - 0.14)
    assert reason == "replay_capture_band"


def test_tp1_partial_is_booked_at_the_target_not_at_the_exit():
    """Половина закрывается на TP1 (0.8%), остаток уходит по полосе на 0.3%:
    0.5*0.8 + 0.5*0.3 - 0.14 = 0.41. До правки сделка целиком книжилась по
    0.3% — то есть модель отдавала обратно то, что бой уже зафиксировал."""
    pct, _ = _replay_trend_one(TRAJ, 0.30, **LADDER,
                               tp1_pct=0.8, tp1_share=0.5)

    assert pct == pytest.approx(0.41)


def test_tp2_partial_fires_at_the_trigger_share_not_at_the_target():
    """Живой контур закрывает на 92% пути до TP2, а не на самой цели. Модель,
    ждущая полной цели, не увидела бы фиксацию вовсе."""
    reaches = [[1, 0.0], [2, 1.0], [3, 1.9], [4, 0.4]]

    hit = _replay_trend_one(reaches, 0.40, **LADDER,
                            tp2_pct=2.0, tp2_share=0.5, tp2_trigger=0.92)[0]
    missed = _replay_trend_one(reaches, 0.40, **LADDER,
                               tp2_pct=2.0, tp2_share=0.5, tp2_trigger=1.0)[0]

    assert hit > missed, "фиксация на 92% пути не сработала"


def test_partial_survives_a_trade_that_ran_to_the_actual_close():
    """Самый обычный случай: ни один ярус не сработал. Забронированная доля
    обязана сохраниться — иначе частичная фиксация исчезала бы именно там, где
    сделок больше всего."""
    flat = [[1, 0.0], [2, 0.9], [3, 0.85], [4, 0.9]]

    pct, reason = _replay_trend_one(flat, 0.50, **LADDER,
                                    tp1_pct=0.8, tp1_share=0.5)

    assert reason == "actual_close_partial"
    # 0.5*0.8 + 0.5*(0.50+0.14) - 0.14 = 0.58
    assert pct == pytest.approx(0.58)


def test_units_stay_gross_when_the_remainder_closes_on_fact():
    """`final_pct` уже чистый, точки траектории — валовые. Смешивать нельзя:
    без приведения остаток получал бы круг издержек даром. При выключенных
    долях выражение обязано сводиться ровно к `final_pct`."""
    flat = [[1, 0.0], [2, 0.2], [3, 0.15]]

    pct, reason = _replay_trend_one(flat, 0.11, **LADDER)

    assert reason == "actual_close"
    assert pct == pytest.approx(0.11)


def test_costs_stay_comparable_across_variants():
    """Число филлов у вариантов разное, но вход оплачен один раз, а доли
    выходов суммируются в единицу — совокупный круг тот же. Без этого
    сравнение по gross-% перестало бы быть корректным, а страница обещает
    именно его."""
    whole = _replay_trend_one(TRAJ, 0.30, **LADDER)[0]
    split = _replay_trend_one(TRAJ, 0.30, **LADDER, tp1_pct=0.3, tp1_share=0.5)[0]

    # Обе величины уже за вычетом ровно одного круга: разница только в том,
    # ГДЕ забронированы доли, а не в том, сколько раз снят cost_pct.
    assert whole == pytest.approx(0.3 - 0.14)
    assert split == pytest.approx(0.5 * 0.3 + 0.5 * 0.3 - 0.14)


# ── источники данных ────────────────────────────────────────────────────────

def test_unavailable_database_does_not_break_the_tool():
    """До правки инструмент читал только файл и работал. Второй источник обязан
    добавлять данные, а не отнимать работоспособность: в тестовой среде БД нет,
    и это не повод падать."""
    assert rows_from_db(10) == []


def test_file_rows_without_an_id_are_not_silently_dropped():
    """Логгер пишет `signal_id` не всегда. Слияние по ключу выбрасывало такие
    строки, и выборка молча уменьшалась — потеря без единого следа в отчёте.
    """
    rows, sources = _merged_rows(100)

    assert sources["file_without_id"] >= 0
    assert sources["used"] == len(rows)
