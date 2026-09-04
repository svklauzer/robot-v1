"""Матожидание по режимам против требования гейта
(#regime-expectancy-report-2026-09-04).

04.09 система сутки не открыла ни одной сделки. Гейт `tp2_reached_too_rarely`
требует долю достижений TP2 не ниже 1/(1+RR) — безубыточной частоты для ставки
«всё или ничего». Мы так не торгуем: половина позиции фиксируется на TP1, после
TP1 стоп переносится в безубыток, защитные выходы банкуют часть хода.

Отчёт кладёт требование гейта и фактическое матожидание рядом, чтобы решать по
факту. Сам он ничего не блокирует — только показания приборов.
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
from services.regime_expectancy_report import build


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


def _closed(db, *, regime: str, net: float, risk: float, entry: float = 100.0,
            tp1: float = 101.0, tp2: float = 103.0, mfe: float = 0.0,
            rr: float = 2.0, reason: str = "stop_loss",
            required_margin: float = 200.0, grade: str = "A"):
    signal = Signal(
        bot_id=db.bot_id, symbol="X/USDT", side="long", status="closed",
        entry_zone_json={"from": entry, "to": entry},
        stop_price=entry * 0.99,
        tp_json={"tp1": tp1, "tp2": tp2},
        confidence=70.0, rationale="t", grade=grade, is_public=True,
        closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        closed_net_pnl=net,
        net_pnl_stop=-abs(risk),
        net_rr_tp2=rr,
        # phantom_adjustment считает поправку от номинала — без него
        # завышение не выражается в USDT и остаётся нулевым.
        required_margin=required_margin,
        closed_reason=reason,
        plan_json={"regime": regime, "lifecycle": {"entry_price": entry, "mfe_pct": mfe}},
    )
    db.add(signal)
    db.flush()
    return signal


# ── сам расчёт ──────────────────────────────────────────────────────────────

def test_expectancy_is_net_pnl_over_planned_risk(db):
    """Определение: Σ net_pnl / Σ |плановый риск|. Издержки и частичная
    фиксация TP1 уже внутри closed_net_pnl."""
    for _ in range(3):
        _closed(db, regime="trend_up", net=2.0, risk=1.0)
    for _ in range(2):
        _closed(db, regime="trend_up", net=-1.0, risk=1.0)

    row = build(db)["regimes"][0]

    assert row["sample"] == 5
    assert row["net_pnl_usdt"] == pytest.approx(4.0)
    assert row["risk_usdt"] == pytest.approx(5.0)
    assert row["expectancy_r"] == pytest.approx(0.8)
    assert row["wins"] == 3 and row["losses"] == 2
    assert row["winrate_pct"] == pytest.approx(60.0)


def test_trades_without_planned_risk_are_excluded_from_both_sums(db):
    """Молча считая их нулевыми, мы занизили бы знаменатель и раздули
    ожидание — та же оговорка, что в regime_expectancy_sizer."""
    _closed(db, regime="r", net=5.0, risk=1.0)
    _closed(db, regime="r", net=5.0, risk=0.0)   # плановый риск неизвестен

    row = build(db)["regimes"][0]

    assert row["sample"] == 1
    assert row["expectancy_r"] == pytest.approx(5.0)


def test_small_samples_are_shown_not_hidden(db):
    """Отличие от сайзера: он прячет режимы с малой историей, потому что
    ПРИНИМАЕТ по ним решение. Здесь решений нет, и размер выборки — тоже
    показание."""
    _closed(db, regime="rare", net=1.0, risk=1.0)

    row = build(db)["regimes"][0]

    assert row["regime"] == "rare"
    assert row["sample"] == 1
    assert row["expectancy_r"] is not None


# ── сопоставление с требованием гейта ───────────────────────────────────────

def test_flags_a_regime_the_gate_blocks_while_it_earns(db):
    """Тот самый случай, ради которого отчёт написан: TP2 берётся редко, гейт
    отказывает — а режим прибылен, потому что зарабатывает на TP1 и трейле."""
    # RR 2.0 → гейт требует 1/(1+2) = 33%. MFE до TP2 (3%) не доходит ни разу,
    # но сделки закрываются в плюс частичной фиксацией.
    for _ in range(8):
        _closed(db, regime="trend_up", net=0.6, risk=1.0, mfe=1.5,
                reason="post_tp1_giveback_trail")
    for _ in range(2):
        _closed(db, regime="trend_up", net=-1.0, risk=1.0, mfe=0.2)

    row = build(db)["regimes"][0]

    assert row["tp_reach_required"] == pytest.approx(1 / 3, abs=1e-3)
    assert row["tp2_reach_realized"] == 0.0
    assert row["tp1_reach_realized"] == pytest.approx(0.8)
    assert row["expectancy_r"] > 0
    assert row["verdict"] == "gate_blocks_but_regime_is_profitable"


def test_flags_a_regime_the_gate_blocks_that_also_loses(db):
    """Обратный вердикт: гейт отказывает и режим действительно убыточен —
    тогда вопрос не к гейту, а к отбору сетапов."""
    for _ in range(10):
        _closed(db, regime="trend_down", net=-0.5, risk=1.0, mfe=0.1)

    row = build(db)["regimes"][0]

    assert row["expectancy_r"] < 0
    assert row["verdict"] == "gate_blocks_and_regime_loses"


def test_reach_uses_each_trades_own_plan_distances(db):
    """Достижимость меряется той же величиной, что и в tp_reachability: MFE
    против дистанций ЭТОЙ сделки, а не против общего порога."""
    _closed(db, regime="r", net=1.0, risk=1.0, entry=100.0, tp1=101.0, tp2=103.0, mfe=3.5)
    _closed(db, regime="r", net=1.0, risk=1.0, entry=100.0, tp1=101.0, tp2=103.0, mfe=1.2)

    row = build(db)["regimes"][0]

    assert row["reach_measured"] == 2
    assert row["tp2_reach_realized"] == pytest.approx(0.5)
    assert row["tp1_reach_realized"] == pytest.approx(1.0)


def test_close_reasons_show_where_the_money_comes_from(db):
    _closed(db, regime="r", net=3.0, risk=1.0, reason="tp2_reached")
    _closed(db, regime="r", net=-1.0, risk=1.0, reason="stop_loss")
    _closed(db, regime="r", net=-1.5, risk=1.0, reason="stop_loss")

    reasons = build(db)["regimes"][0]["by_close_reason"]

    assert reasons["stop_loss"]["n"] == 2
    assert reasons["stop_loss"]["net_usdt"] == pytest.approx(-2.5)
    assert reasons["tp2_reached"]["net_usdt"] == pytest.approx(3.0)


def test_window_excludes_old_trades(db):
    old = _closed(db, regime="r", net=99.0, risk=1.0)
    old.closed_at = datetime.now(timezone.utc) - timedelta(hours=100)
    db.flush()
    _closed(db, regime="r", net=1.0, risk=1.0)

    row = build(db, window_hours=10.0)["regimes"][0]

    assert row["sample"] == 1
    assert row["net_pnl_usdt"] == pytest.approx(1.0)


def test_empty_history_does_not_explode(db):
    out = build(db)
    assert out["regimes"] == []
    assert out["closed_signals"] == 0


# ── честный PnL: фантомные филлы (#regime-expectancy-honest-2026-09-04) ─────
#
# Ветка `tp2_reached` закрывает на 92% пути до цели (exit_policy) и книжит
# ПОЛНУЮ цену TP2. Отсюда расхождение в боевых данных, которое иначе выглядит
# необъяснимым: 11 сделок закрыты как tp2_reached, а MFE дотянулся до цели у
# одной. Записанный результат выше, чем сделка вообще ходила, — это исполнение
# лучше рынка, и детектор phantom_fill его ловит.
#
# Первая версия отчёта считала матожидание по сырому closed_net_pnl, то есть
# мерила край по прибыли, которой не было. Дашборд эту поправку применяет с
# июля (total_net_pnl_honest_usdt), отчёт её потерял.

def test_expectancy_uses_honest_pnl_not_the_booked_one(db):
    """result_pct выше MFE — филл лучше рынка. Такой исход обязан входить в
    матожидание по честной цене, иначе гейт судят по несуществующему краю."""
    s = _closed(db, regime="r", net=10.0, risk=1.0, entry=100.0,
                tp1=101.0, tp2=103.0, mfe=1.0, reason="tp2_reached")
    # Книжим 3% при максимуме хода 1% и последней ценой траектории 0.9%.
    s.result_pct = 3.0
    s.plan_json = {**s.plan_json, "lifecycle": {
        **s.plan_json["lifecycle"], "traj": [[0, 0.0], [10, 0.9]],
    }}
    db.flush()

    row = build(db)["regimes"][0]

    assert row["phantom_fills"] == 1
    assert row["phantom_overstatement_usdt"] > 0
    assert row["expectancy_r"] < row["expectancy_r_raw"], (
        "честное матожидание обязано быть ниже сырого — иначе поправка не применилась"
    )


def test_clean_trade_is_not_penalised(db):
    """Обратная сторона: сделка, закрытая не лучше рынка, поправки не получает."""
    _closed(db, regime="r", net=2.0, risk=1.0, mfe=5.0, reason="tp2_reached")

    row = build(db)["regimes"][0]

    assert row["phantom_fills"] == 0
    assert row["expectancy_r"] == row["expectancy_r_raw"]


def test_two_reach_measures_expose_the_92_percent_trigger(db):
    """Гейт спрашивает про ПОЛНУЮ дистанцию, а закрытие происходит на 92% пути.
    Обе величины выводятся рядом: их разрыв и есть зона, где книжится цель,
    которой рынок не коснулся."""
    # dist до TP2 = 3%; MFE 2.8% — это 93% пути: триггер сработал, цель нет.
    _closed(db, regime="r", net=1.0, risk=1.0, entry=100.0, tp1=101.0, tp2=103.0, mfe=2.8)

    row = build(db)["regimes"][0]

    assert row["tp2_reach_realized"] == 0.0
    assert row["tp2_trigger_realized"] == 1.0


# ── интервал: отличим ли результат от нуля (#expectancy-ci-2026-09-04) ─────
#
# В коде уже записан прецедент (signal_quality.py, 30.07): грейд измерили как
# A +0.090R [−0.210; +0.434] против B −0.070R [−0.181; +0.048]. Оба интервала
# накрывают ноль — ось ничего не предсказывает. 04.09 та же ось показала
# ПРОТИВОПОЛОЖНЫЙ знак (A хуже B), что для шума нормально и находкой не
# является. Без интервала это различить нельзя, а точечная оценка на четырёх
# десятках сделок читается как факт.

def test_noisy_sample_is_not_called_significant(db):
    """Разнородные исходы вокруг нуля: точечная оценка отрицательна, но
    интервал накрывает ноль — вывод «режим убыточен» делать нельзя."""
    for net in (2.0, -1.0, 3.0, -2.0, 1.0, -3.0, 0.5, -1.5, 2.5, -2.5):
        _closed(db, regime="noisy", net=net, risk=1.0)

    row = build(db)["regimes"][0]

    lo, hi = row["expectancy_r_ci"]
    assert lo < 0 < hi
    assert row["significant"] is False


def test_consistent_loss_is_called_significant(db):
    """Обратная сторона: когда убыток устойчив, интервал ноль не накрывает и
    вывод делать можно."""
    for _ in range(30):
        _closed(db, regime="bad", net=-0.9, risk=1.0)

    row = build(db)["regimes"][0]

    lo, hi = row["expectancy_r_ci"]
    assert hi < 0
    assert row["significant"] is True


def test_tiny_sample_gets_no_interval_at_all(db):
    """На четырёх сделках интервал не считается: любая его ширина создавала бы
    видимость измерения там, где его нет."""
    for _ in range(4):
        _closed(db, regime="tiny", net=-1.0, risk=1.0)

    row = build(db)["regimes"][0]

    assert row["expectancy_r_ci"] == [None, None]
    assert row["significant"] is None


def test_interval_is_reproducible(db):
    """Один и тот же набор сделок обязан давать один и тот же интервал —
    иначе отчёт выглядит пляшущим и ему перестают верить."""
    for net in (1.0, -2.0, 3.0, -1.0, 0.5, -0.5, 2.0, -3.0):
        _closed(db, regime="r", net=net, risk=1.0)

    assert build(db)["regimes"][0]["expectancy_r_ci"] == \
           build(db)["regimes"][0]["expectancy_r_ci"]


def test_grade_axis_is_measured_the_same_way(db):
    """Грейд раздаёт пороги входа и срок жизни сигнала. Меряем его той же
    величиной и тем же способом, что режимы, — иначе сравнивать с июльским
    замером нечего."""
    for _ in range(12):
        _closed(db, regime="r", net=-1.0, risk=1.0, grade="A")
    for _ in range(12):
        _closed(db, regime="r", net=1.0, risk=1.0, grade="B")

    grades = {g["grade"]: g for g in build(db)["grades"]}

    assert grades["A"]["expectancy_r"] == pytest.approx(-1.0)
    assert grades["B"]["expectancy_r"] == pytest.approx(1.0)
    assert grades["A"]["significant"] is True
