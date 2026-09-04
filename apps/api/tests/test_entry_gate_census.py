"""Перепись блокировок входа (#entry-gate-census-2026-09-04).

04.09 при активном рынке за час не прошёл ни один вход. Ни одна блокировка не
была связана с уверенностью — значит правки оценки поток не резали. Держал
`adx_rising` со строгим условием `adx <= adx_prev` и нулевым допуском, на
дельтах вроде −0.0955 при ADX 29.5.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.intelligence_event import IntelligenceEvent
from services.entry_gate_census import build


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[IntelligenceEvent.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _tz(db, *, delta: float, blocked_by: str = "adx_rising",
        failed: tuple[str, ...] = ("adx_not_rising:29.5->29.4",),
        decision: str = "tz_entry_conditions", evaluated: bool = True):
    db.add(IntelligenceEvent(
        symbol="SOL/USDT", status="blocked", decision=decision, action="long",
        regime="trend_up_candidate", confidence_hint=57.0,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        payload_json={
            "evaluated": evaluated, "would_pass": False, "failed": list(failed),
            "adx": 29.4, "adx_delta": delta,
            "enforce_reason": f"blocked_by:{blocked_by}" if blocked_by else "",
        },
    ))
    db.flush()


def test_census_names_the_gate_that_holds_the_flow(db):
    for _ in range(7):
        _tz(db, delta=-0.1)
    for _ in range(3):
        _tz(db, delta=0.0, decision="tp2_reached_too_rarely")

    out = build(db)

    assert out["by_decision"]["tz_entry_conditions"] == 7
    assert out["by_decision"]["tp2_reached_too_rarely"] == 3


def test_only_the_sole_blocker_can_be_opened_by_a_tolerance(db):
    """Ключевое различение отчёта. Там, где параллельно не прошли DI или OBV,
    допуск по ADX не откроет вход — считать их значило бы обещать поток,
    которого не будет."""
    _tz(db, delta=-0.1, blocked_by="adx_rising")
    _tz(db, delta=-0.1, blocked_by="adx_rising,obv")
    _tz(db, delta=-0.1, blocked_by="adx_rising,di,kama")

    adx = build(db)["adx_rising"]

    assert adx["adx_enforced_block"] == 3
    assert adx["sole_enforce_blocker"] == 1


def test_tolerance_counts_only_deltas_inside_the_band(db):
    _tz(db, delta=-0.05)      # шум
    _tz(db, delta=-0.25)      # шум пошире
    _tz(db, delta=-2.0)       # реальное затухание

    band = build(db)["adx_rising"]["would_pass_at_tolerance"]

    assert band["0.1"] == 1
    assert band["0.3"] == 2
    assert band["0.5"] == 2   # −2.0 не проходит ни при каком разумном допуске


def test_observation_only_failures_do_not_count_as_blocks(db):
    """`failed` содержит и наблюдательные условия. Если ADX не прошёл, но в
    enforce его нет, вход держит что-то другое, и допуск ничего не решит."""
    _tz(db, delta=-0.1, blocked_by="obv",
        failed=("adx_not_rising:29.5->29.4", "obv_below_ema"))

    adx = build(db)["adx_rising"]

    assert adx["adx_not_rising_failed"] == 1
    assert adx["adx_enforced_block"] == 0
    assert adx["sole_enforce_blocker"] == 0


def test_both_thresholds_are_printed_side_by_side(db):
    """Один и тот же вопрос «растёт ли ADX» проверяется в двух местах с
    разными порогами: строго больше нуля в ТЗ и 0.5 в анти-чопе. В одном дампе
    видно, как это расходится (AVAX 34.6→34.7 — вырос, но помечен как не
    растущий). Расхождение должно быть видно в отчёте, а не в чьей-то памяти."""
    _tz(db, delta=-0.1)

    thresholds = build(db)["adx_rising"]["thresholds_in_use"]

    assert "допуск 0" in thresholds["tz_entry_shadow"]
    assert thresholds["anti_chop_young_trend"] == pytest.approx(0.5)


def test_missing_delta_is_skipped_not_counted_as_zero(db):
    """Поле пишется только с 04.09. Нулевая дельта означала бы «тренд встал» —
    у старых событий мы этого не знаем."""
    _tz(db, delta=-0.1)
    db.add(IntelligenceEvent(
        symbol="SOL/USDT", status="blocked", decision="tz_entry_conditions",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        payload_json={"evaluated": True, "failed": ["adx_not_rising:29.5->29.4"],
                      "enforce_reason": "blocked_by:adx_rising"},
    ))
    db.flush()

    adx = build(db)["adx_rising"]

    assert adx["adx_enforced_block"] == 2      # событие посчитано как блок
    assert adx["sole_enforce_blocker"] == 1    # но в распределение не попало


def test_window_cuts_off_older_events(db):
    db.add(IntelligenceEvent(
        symbol="SOL/USDT", status="blocked", decision="tz_entry_conditions",
        created_at=datetime.now(timezone.utc) - timedelta(hours=50),
        payload_json={"evaluated": True, "failed": [], "adx_delta": 1.0},
    ))
    db.flush()

    assert build(db, window_hours=24.0)["events"] == 0


def test_empty_history_does_not_explode(db):
    out = build(db)
    assert out["events"] == 0
    assert out["adx_rising"]["delta_all"] == {}
