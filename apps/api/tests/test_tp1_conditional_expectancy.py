"""Порог достижения TP1 из измеренных исходов (#tp1-conditional-2026-09-04).

Гейт достижимости требует `tp2_hit >= 1/(1+RR_tp2)` — точку безубыточности
бинарной ставки «взял цель или получил стоп». Выход давно не бинарный, и на
числах 04.09 разрыв очевиден: TP1 берётся в 15–33%, TP2 не взят ни разу, а RR
до TP1 около 0.32–0.47 — бинарная формула потребовала бы 68–76%.

Здесь порог не постулируется, а выводится: p, при котором
p*E[R|дошла] + (1-p)*E[R|не дошла] = 0.
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
from services.tp1_conditional_expectancy import build


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


def _sig(db, *, net: float, mfe: float | None = None, tp1_dist: float = 1.0,
         reason: str = "tz_kama", partial: bool = False, risk: float = 1.0):
    plan = {"tp_reach": {"tp1_dist_pct": tp1_dist}}
    if mfe is not None:
        plan["lifecycle"] = {"mfe_pct": mfe}
    if partial:
        plan["tp1_partial"] = {"closed_qty": 1.0, "net_pnl": 0.5}

    signal = Signal(
        bot_id=db.bot_id, symbol="X/USDT", side="long", status="closed",
        entry_zone_json={"from": 100.0, "to": 100.0}, stop_price=99.0,
        tp_json={"tp1": 101.0, "tp2": 103.0}, confidence=70.0, rationale="t",
        grade="A", is_public=True,
        closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        closed_net_pnl=net, net_pnl_stop=-risk, closed_reason=reason,
        plan_json=plan,
    )
    db.add(signal)
    db.flush()
    return signal


# ── сам порог ───────────────────────────────────────────────────────────────

def test_threshold_comes_from_measured_outcomes_not_from_rr():
    """Ради этого отчёт и написан. Дошедшие дают +2R, недошедшие −1R:
    p*2 + (1-p)*(-1) = 0  →  p = 1/3. Никакого 1/(1+RR)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, Bot.__table__, Signal.__table__])
    session = sessionmaker(bind=engine)()
    user = User(email="o@e.com", password_hash="h")
    session.add(user); session.flush()
    bot = Bot(user_id=user.id, name="R", status="running", mode="paper", config_json={})
    session.add(bot); session.flush()
    session.bot_id = bot.id

    for _ in range(6):
        _sig(session, net=2.0, mfe=1.5)      # дошла до TP1 (1.5 >= 1.0)
    for _ in range(6):
        _sig(session, net=-1.0, mfe=0.2)     # не дошла

    out = build(session)

    assert out["reached_tp1"]["expectancy_r"] == pytest.approx(2.0)
    assert out["missed_tp1"]["expectancy_r"] == pytest.approx(-1.0)
    assert out["required_rate"] == pytest.approx(1 / 3, abs=1e-4)
    session.close()


def test_observed_rate_is_compared_against_the_threshold(db):
    for _ in range(6):
        _sig(db, net=2.0, mfe=1.5)
    for _ in range(6):
        _sig(db, net=-1.0, mfe=0.2)

    out = build(db)

    assert out["observed_rate"] == pytest.approx(0.5)
    assert out["verdict"] == "clears_the_bar"


def test_below_the_bar_is_named_as_such(db):
    """Дошедшие дают всего +0.5R: порог = 1/1.5 = 0.667, а доходит половина."""
    for _ in range(6):
        _sig(db, net=0.5, mfe=1.5)
    for _ in range(6):
        _sig(db, net=-1.0, mfe=0.2)

    out = build(db)

    assert out["required_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert out["observed_rate"] == pytest.approx(0.5)
    assert out["verdict"] == "below_the_bar"


def test_no_threshold_exists_when_reaching_tp1_does_not_pay(db):
    """Сильнее любого числа: если дошедшие не лучше недошедших, порога не
    существует и чинить надо не гейт, а сам выход. Возвращать сюда какое-нибудь
    число значило бы предложить настроить то, что не поможет."""
    for _ in range(6):
        _sig(db, net=-1.0, mfe=1.5)
    for _ in range(6):
        _sig(db, net=-1.0, mfe=0.2)

    out = build(db)

    assert out["required_rate"] is None
    assert out["verdict"] == "reaching_tp1_does_not_pay"


# ── признаки достижения ─────────────────────────────────────────────────────

def test_geometric_marker_uses_mfe_against_tp1_distance(db):
    _sig(db, net=1.0, mfe=1.0, tp1_dist=1.0)     # ровно дотянула — считается
    _sig(db, net=-1.0, mfe=0.99, tp1_dist=1.0)   # не дотянула

    out = build(db, marker="geometric")

    assert out["reached_tp1"]["n"] == 1
    assert out["missed_tp1"]["n"] == 1


def test_actual_marker_uses_the_real_partial_fill(db):
    """Расхождение двух признаков само по себе диагноз: цена дошла, а фиксация
    не сработала. Поэтому признака два, а не один."""
    _sig(db, net=1.0, mfe=1.5, partial=False)    # геометрия да, фиксации нет
    _sig(db, net=1.0, mfe=1.5, partial=True)

    assert build(db, marker="geometric")["reached_tp1"]["n"] == 2
    assert build(db, marker="actual")["reached_tp1"]["n"] == 1


def test_trades_without_mfe_are_dropped_not_guessed(db):
    """Отсутствие замера — не «не дошла». Иначе старые сделки без lifecycle
    утяжеляли бы проигрышную ветвь и занижали порог."""
    _sig(db, net=1.0, mfe=None)

    out = build(db)

    assert out["unusable"] == 1
    assert out["reached_tp1"]["n"] == 0 and out["missed_tp1"]["n"] == 0


def test_trades_without_risk_are_dropped(db):
    _sig(db, net=1.0, mfe=1.5, risk=0.0)

    assert build(db)["unusable"] == 1


# ── честность и осторожность ────────────────────────────────────────────────

def test_phantom_markup_is_removed_from_the_winning_branch(db):
    """Ветка tp2_reached книжит полную цену TP2, закрываясь на 92% пути.
    Наценка попадает ТОЛЬКО к дошедшим — то есть ровно туда, где мы измеряем
    выигрыш. Без поправки порог вышел бы заниженным."""
    signal = _sig(db, net=5.0, mfe=1.5, reason="tp2_reached")
    signal.required_margin = 100.0
    signal.result_pct = 3.0          # забукали больше, чем прошла цена (mfe 1.5)
    signal.plan_json = {**signal.plan_json,
                        "lifecycle": {"mfe_pct": 1.5, "traj": [[1, 1.4]]}}
    db.flush()

    honest = build(db)["reached_tp1"]["net_usdt"]

    assert honest < 5.0, "фантомная наценка не снята с выигрышной ветви"


def test_thin_sample_is_refused_rather_than_answered(db):
    for _ in range(3):
        _sig(db, net=2.0, mfe=1.5)
    for _ in range(3):
        _sig(db, net=-1.0, mfe=0.2)

    out = build(db)

    assert out["verdict"] == "sample_too_thin"
    assert out.get("required_rate_ci") is None, "интервал по трём сделкам — не интервал"


def test_interval_is_reported_for_the_threshold_itself(db):
    """Порог — нелинейная функция двух ветвей, поэтому бутстрапится целиком, а
    не склеивается из краёв двух интервалов."""
    for i in range(12):
        _sig(db, net=2.0 + i * 0.1, mfe=1.5)
    for i in range(12):
        _sig(db, net=-1.0 + i * 0.05, mfe=0.2)

    out = build(db)
    lo, hi = out["required_rate_ci"]

    assert lo is not None and hi is not None
    assert lo <= out["required_rate"] <= hi
    assert out["spread_positive_share"] == pytest.approx(1.0)


def test_close_reasons_show_what_the_trail_earns(db):
    """Ради разговора о трейле: внутри дошедших до TP1 видно, чем они
    закончились — это и есть вклад сопровождения."""
    _sig(db, net=1.5, mfe=1.5, reason="breakeven_stop")
    _sig(db, net=-0.4, mfe=1.5, reason="tz_kama")

    reasons = build(db)["reached_tp1"]["by_close_reason"]

    assert reasons["breakeven_stop"]["n"] == 1
    assert reasons["tz_kama"]["net_usdt"] == pytest.approx(-0.4)


def test_empty_history_does_not_explode(db):
    out = build(db)
    assert out["closed"] == 0
    assert out["required_rate"] is None
    assert out["verdict"] == "sample_too_thin"
