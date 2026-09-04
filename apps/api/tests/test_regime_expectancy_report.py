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
            rr: float = 2.0, reason: str = "stop_loss"):
    signal = Signal(
        bot_id=db.bot_id, symbol="X/USDT", side="long", status="closed",
        entry_zone_json={"from": entry, "to": entry},
        stop_price=entry * 0.99,
        tp_json={"tp1": tp1, "tp2": tp2},
        confidence=70.0, rationale="t", grade="A", is_public=True,
        closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        closed_net_pnl=net,
        net_pnl_stop=-abs(risk),
        net_rr_tp2=rr,
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
