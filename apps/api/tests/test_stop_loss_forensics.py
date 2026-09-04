"""Чем стопнутая сделка отличалась на входе (#stop-forensics-2026-09-04).

Замер 04.09 по 91 закрытой сделке: 37 стопов дали −53.82 USDT, остальные 54 —
плюс 34.16. Без стопов система прибыльна, и сопровождение работает (tz_kama
режет по −0.38, breakeven_stop в плюс). Значит убивают входы, идущие против
сразу.

Отчёт ищет признак, разделяющий эти две группы. Ранговая мера (AUC) выбрана
потому, что на 37 наблюдениях среднее ломается одним выбросом.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.bot import Bot
from models.signal import Signal
from models.user import User
from services.stop_loss_forensics import _auc, build


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, Bot.__table__, Signal.__table__,
    ])
    session = sessionmaker(bind=engine)()
    user = User(email="o@e.com", password_hash="h")
    session.add(user)
    session.flush()
    bot = Bot(user_id=user.id, name="Main Robot", status="running",
              mode="paper", config_json={})
    session.add(bot)
    session.flush()
    session.bot_id = bot.id
    yield session
    session.close()


def _sig(db, *, reason: str, side: str = "long", net: float = -1.0,
         obi: float = 0.0, adx: float = 25.0, score: float = 80.0,
         regime: str = "trend_up_candidate", grade: str = "A"):
    signal = Signal(
        bot_id=db.bot_id, symbol="X/USDT", side=side, status="closed",
        entry_zone_json={"from": 100.0, "to": 100.0}, stop_price=99.0,
        tp_json={"tp1": 101.0, "tp2": 103.0},
        confidence=70.0, rationale="t", grade=grade, is_public=True,
        closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        closed_net_pnl=net, net_pnl_stop=-1.0, closed_reason=reason,
        plan_json={
            "regime": regime,
            "trade_mode": "trend",
            "entry_depth": {"obi": obi, "cvd_ratio": 0.0, "spread_pct": 0.01,
                            "cvd_trades": 10},
            "tz_shadow": {"adx": adx, "di_spread": 10.0, "stoch_k": 40.0,
                          "would_pass": False},
            "setup_quality": {"final_score": score, "trend_alignment": 50.0,
                              "entry_timing": 20.0, "volume_confirmation": 15.0,
                              "structure_quality": 12.0, "penalty": 0.0},
            "ml": {"ml_score": 0.5},
            "tp_reach": {"tp1_dist_pct": 1.0, "tp2_dist_pct": 3.0},
            "sizing": {"conviction": 1.0},
            "entry_zone_plan": {"mode": "market", "drift_pct": 0.0,
                                "depth": {"near_depth_share": 0.05}},
        },
    )
    db.add(signal)
    db.flush()
    return signal


# ── ранговая мера ───────────────────────────────────────────────────────────

def test_auc_is_half_when_groups_are_identical():
    assert _auc([1, 2, 3], [1, 2, 3]) == 0.5


def test_auc_reaches_the_edges_on_perfect_separation():
    assert _auc([5, 6, 7], [1, 2, 3]) == 1.0
    assert _auc([1, 2, 3], [5, 6, 7]) == 0.0


def test_auc_is_not_moved_by_a_single_outlier():
    """Ради этого мера и ранговая: на 37 наблюдениях один выброс сдвинул бы
    среднее так, что признак показался бы разделяющим."""
    assert _auc([1, 2, 3], [4, 5, 10_000]) == _auc([1, 2, 3], [4, 5, 6])


# ── разделение групп ────────────────────────────────────────────────────────

def _rows(out, feature):
    return next(r for r in out["numeric"] if r["feature"] == feature)


def test_finds_a_feature_that_separates(db):
    """Стопнутые входили при низком ADX, выжившие при высоком."""
    for _ in range(8):
        _sig(db, reason="stop_loss", adx=15.0)
    for _ in range(8):
        _sig(db, reason="tp2_reached", net=2.0, adx=35.0)

    row = _rows(build(db, min_group=5), "tz_shadow.adx")

    assert row["auc"] == 0.0                      # у стопнутых всегда ниже
    assert row["higher_in"] == "survived"
    assert row["verdict"] == "separates_strongly"


def test_marks_a_useless_feature_as_indistinguishable(db):
    """Главная защита от самообмана: на малой выборке почти любой признак
    даёт какой-то перекос, и его нельзя принимать за находку."""
    for i in range(8):
        _sig(db, reason="stop_loss", adx=20.0 + i)
    for i in range(8):
        _sig(db, reason="tp2_reached", net=2.0, adx=20.0 + i)

    row = _rows(build(db, min_group=5), "tz_shadow.adx")

    assert row["auc"] == 0.5
    assert row["verdict"] == "indistinguishable"


def test_signed_features_are_oriented_by_side(db):
    """obi/cvd — направленные: «поток за сделку» у лонга и шорта имеет
    противоположный знак. Без нормировки лонги и шорты гасят друг друга, и
    разделение пропадает даже там, где оно есть.

    Здесь у ВСЕХ выживших поток по сделке (+0.6 лонгам, −0.6 шортам), у всех
    стопнутых против. В сыром виде медианы обеих групп были бы около нуля.
    """
    for i in range(8):
        side = "long" if i % 2 else "short"
        _sig(db, reason="stop_loss", side=side, obi=-0.6 if side == "long" else 0.6)
    for i in range(8):
        side = "long" if i % 2 else "short"
        _sig(db, reason="tp2_reached", net=2.0, side=side,
             obi=0.6 if side == "long" else -0.6)

    row = _rows(build(db, min_group=5), "entry_depth.obi")

    assert row["median_stopped"] == pytest.approx(-0.6)
    assert row["median_survived"] == pytest.approx(0.6)
    assert row["auc"] == 0.0


def test_strongest_separator_comes_first(db):
    for i in range(8):
        _sig(db, reason="stop_loss", adx=15.0, score=80.0 + i)
    for i in range(8):
        _sig(db, reason="tp2_reached", net=2.0, adx=35.0, score=80.0 + i)

    out = build(db, min_group=5)

    assert out["numeric"][0]["feature"] == "tz_shadow.adx"


# ── группы и категории ──────────────────────────────────────────────────────

def test_group_totals_match_the_split(db):
    for _ in range(3):
        _sig(db, reason="stop_loss", net=-2.0)
    for _ in range(2):
        _sig(db, reason="breakeven_stop", net=1.0)

    out = build(db, min_group=1)

    assert out["stopped"]["n"] == 3
    assert out["stopped"]["net_usdt"] == pytest.approx(-6.0)
    assert out["survived"]["n"] == 2
    assert out["survived"]["net_usdt"] == pytest.approx(2.0)


def test_categorical_shows_stop_rate_per_level(db):
    for _ in range(3):
        _sig(db, reason="stop_loss", grade="B")
    _sig(db, reason="tp2_reached", net=2.0, grade="B")
    _sig(db, reason="tp2_reached", net=2.0, grade="A")

    levels = next(c for c in build(db, min_group=1)["categorical"]
                  if c["feature"] == "grade")["levels"]

    assert levels["B"]["n"] == 4
    assert levels["B"]["stop_rate"] == pytest.approx(0.75)
    assert levels["A"]["stop_rate"] == pytest.approx(0.0)


def test_thin_features_are_dropped_not_guessed(db):
    """Признак, у которого в группе меньше min_group значений, не выводится
    вовсе: доля по трём наблюдениям — не доля."""
    for _ in range(2):
        _sig(db, reason="stop_loss")
    for _ in range(2):
        _sig(db, reason="tp2_reached", net=2.0)

    assert build(db, min_group=5)["numeric"] == []


def test_regime_filter_narrows_the_sample(db):
    for _ in range(6):
        _sig(db, reason="stop_loss", regime="trend_up_candidate")
    for _ in range(6):
        _sig(db, reason="stop_loss", regime="trend_down_candidate")

    out = build(db, regime="trend_up_candidate", min_group=1)

    assert out["stopped"]["n"] == 6


def test_empty_history_does_not_explode(db):
    out = build(db)
    assert out["stopped"]["n"] == 0
    assert out["numeric"] == []
