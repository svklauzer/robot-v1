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


# ── концентрация и геометрия (#census-concentration-2026-09-04) ─────────────

def _tp2(db, symbol: str, *, regime: str = "crt", mfe: float = 0.7742,
         tp1: float = 2.7, tp2: float = 3.1, hit: float = 0.0238,
         hit1: float = 0.0714, need: float = 0.34):
    db.add(IntelligenceEvent(
        symbol=symbol, status="blocked", decision="tp2_reached_too_rarely",
        action="long", regime=regime,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        payload_json={"evaluated": True, "allowed": False,
                      "median_mfe_pct": mfe, "tp1_dist_pct": tp1,
                      "tp2_dist_pct": tp2, "tp2_hit_rate": hit,
                      "tp1_hit_rate": hit1,
                      "required_hit_rate": need},
    ))
    db.flush()


def test_one_noisy_symbol_does_not_read_as_a_system_wide_problem(db):
    """Счётчик событий переоценивает символы, которые тикают чаще. 500
    блокировок одного символа и 500 блокировок пяти разных — это разные
    диагнозы, а в `by_decision` они выглядят одинаково."""
    for _ in range(20):
        _tp2(db, "ADA/USDT")
    _tp2(db, "XRP/USDT")

    slot = build(db)["concentration"]["tp2_reached_too_rarely"]

    assert slot["events"] == 21
    assert slot["symbols"] == 2
    assert slot["top"]["ADA/USDT"] == 20
    assert slot["top_share"] == pytest.approx(0.9524, abs=1e-4)


def test_geometry_gap_is_reported_as_a_ratio(db):
    """Гейт достижимости не «слишком строг»: он сравнивает цель с измеренным
    ходом инструмента. Отношение и есть диагноз — цель вчетверо дальше
    типичного хода чинится постановкой целей, а не ослаблением гейта."""
    for _ in range(3):
        _tp2(db, "ADA/USDT", mfe=0.7742, tp2=3.1)

    row = build(db)["tp2_reach"]["ADA/USDT|crt"]

    assert row["median_mfe_pct"] == pytest.approx(0.7742)
    assert row["tp2_dist_pct"] == pytest.approx(3.1)
    assert row["tp2_over_mfe"] == pytest.approx(4.0, abs=0.01)


def test_symbols_and_regimes_do_not_get_mixed(db):
    _tp2(db, "ADA/USDT", regime="crt", tp2=3.1)
    _tp2(db, "ADA/USDT", regime="scalp", tp2=0.8)

    reach = build(db)["tp2_reach"]

    assert reach["ADA/USDT|crt"]["tp2_dist_pct"] == pytest.approx(3.1)
    assert reach["ADA/USDT|scalp"]["tp2_dist_pct"] == pytest.approx(0.8)


def test_missing_geometry_does_not_invent_a_ratio(db):
    db.add(IntelligenceEvent(
        symbol="ADA/USDT", status="blocked", decision="tp2_reached_too_rarely",
        regime="crt", created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        payload_json={"evaluated": True},
    ))
    db.flush()

    assert build(db)["tp2_reach"]["ADA/USDT|crt"]["tp2_over_mfe"] is None


def test_tp1_viability_is_reported_next_to_tp2(db):
    """(#tp1-viability-2026-09-04) Это число решает направление починки. TP1
    берётся, TP2 нет — верхняя цель должна быть трейлом, а не точкой. Не
    берётся и TP1 — сломана вся сетка, и трейл не поможет."""
    for _ in range(3):
        _tp2(db, "SOL/USDT", regime="trend_up_candidate", hit1=0.31, hit=0.0)

    row = build(db)["tp2_reach"]["SOL/USDT|trend_up_candidate"]

    assert row["tp1_hit_rate"] == pytest.approx(0.31)
    assert row["tp2_hit_rate"] == 0.0


# ── защёлка импульса (#entry-impulse-2026-09-04) ────────────────────────────

def _tz_latched(db, *, live: bool, blocked_by: str = "adx_rising",
                age: float = 600.0):
    db.add(IntelligenceEvent(
        symbol="SOL/USDT", status="blocked", decision="tz_entry_conditions",
        action="long", regime="trend_up_candidate",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        payload_json={
            "evaluated": True, "would_pass": False,
            "failed": ["adx_not_rising:29.5->29.4"], "adx_delta": -0.1,
            "enforce_reason": f"blocked_by:{blocked_by}",
            "impulse_latch": {
                "mode": "shadow", "window_sec": 1800.0, "live": live,
                "impulse": ({"kind": "adx_turned_up", "age_sec": age}
                            if live else None),
            },
        },
    ))
    db.flush()


def test_live_latch_on_a_block_is_the_hypothesis_under_test(db):
    """Если импульс БЫЛ, просто раньше, чем подтвердилось состояние, то
    блокировка — следствие требования одновременности, а не отсутствия
    импульса. Это и есть число, ради которого защёлка сначала идёт в тень."""
    for _ in range(7):
        _tz_latched(db, live=True)
    for _ in range(3):
        _tz_latched(db, live=False)

    out = build(db)["adx_rising"]["impulse_latch"]

    assert out["observed"] == 10
    assert out["live_on_adx_block"] == 7
    assert out["live_and_sole_blocker"] == 7
    assert out["share_live"] == pytest.approx(0.7)


def test_latch_counts_separate_sole_blockers_from_the_rest(db):
    """Enforce откроет только те входы, где adx_rising — единственный блокер.
    Считать остальные значило бы пообещать поток, которого не будет."""
    _tz_latched(db, live=True, blocked_by="adx_rising")
    _tz_latched(db, live=True, blocked_by="adx_rising,obv")

    out = build(db)["adx_rising"]["impulse_latch"]

    assert out["live_on_adx_block"] == 2
    assert out["live_and_sole_blocker"] == 1


def test_events_without_the_latch_field_are_not_counted_as_absent(db):
    """Поле пишется с 04.09. Старое событие без него — не «защёлки не было»,
    а «мы не смотрели»; смешать их значило бы занизить долю."""
    _tz(db, delta=-0.1)                 # без impulse_latch
    _tz_latched(db, live=True)

    out = build(db)["adx_rising"]["impulse_latch"]

    assert out["observed"] == 1
    assert out["share_live"] == pytest.approx(1.0)
