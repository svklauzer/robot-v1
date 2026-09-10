"""Что происходит с позицией после TP1 (#tp1-overshoot-2026-09-10).

Формы сделок взяты из ленты: AVAX #476 перешагнула TP1 (0.73%), дошла до
+1.98% — в 2.7 раза дальше — и остаток закрылся безубытком; ADA #483 — то же на
+1.63%. Трейл после TP1 взводится при откате ≥ 0.4·MFE и в обоих случаях должен
был сработать задолго до безубытка.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
from core.db import Base
from models.bot import Bot
from models.signal import Signal
from models.user import User
from services.tp1_overshoot import _trail_trigger, build


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


@pytest.fixture(autouse=True)
def trail_settings(monkeypatch):
    monkeypatch.setattr(settings, "POST_TP1_TRAIL_MIN_MFE_PCT", 0.60, raising=False)
    monkeypatch.setattr(settings, "POST_TP1_TRAIL_GIVEBACK_SHARE", 0.40, raising=False)


def _sig(db, *, side="long", entry=100.0, tp1_pct=1.0, mfe=1.0, exit_pct=0.0,
         traj=None, reason="breakeven_stop", partial=True, result=None,
         trail_in_force=True, rest_qty=1.0, market=None):
    tp1 = entry * (1 + tp1_pct / 100) if side == "long" else entry * (1 - tp1_pct / 100)
    exit_price = entry * (1 + exit_pct / 100) if side == "long" else entry * (1 - exit_pct / 100)
    plan = {"lifecycle": {"entry_price": entry, "mfe_pct": mfe, "traj": traj or []}}
    if trail_in_force:
        plan["config"] = {"exit": {
            "post_tp1_trail_enabled": True,
            "post_tp1_trail_min_mfe_pct": 0.6,
            "post_tp1_trail_giveback_share": 0.4,
            "net_safe_floor_pct": 0.30,
            "tp1_partial_enabled": True,
            "min_protective_net_usdt": 0.25,
        }}
        if market is not None:
            plan["config"]["market"] = market
    if partial:
        plan["tp1_partial"] = {"closed_qty": 1.0, "remaining_qty": rest_qty}
    signal = Signal(
        bot_id=db.bot_id, symbol="X/USDT", side=side, status="closed",
        entry_zone_json={"from": entry, "to": entry}, stop_price=entry * 0.99,
        tp_json={"tp1": tp1, "tp2": entry * 1.05}, confidence=70.0, rationale="t",
        grade="B", is_public=True,
        closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        closed_exit_price=exit_price, closed_reason=reason,
        result_pct=result if result is not None else exit_pct,
        closed_net_pnl=0.0, qty=2.0, closed_total_cost=0.28, plan_json=plan,
    )
    db.add(signal)
    db.flush()
    return signal


# ── перешагнули и отдали ────────────────────────────────────────────────────

def test_a_trade_that_ran_far_past_tp1_and_came_back_to_breakeven(db):
    """Форма AVAX #476: TP1 0.73%, пик 1.98%, остаток закрыт безубытком."""
    traj = [[0, 0.0], [10, 0.8], [20, 1.98], [30, 1.1], [40, 0.05]]
    _sig(db, tp1_pct=0.73, mfe=1.98, exit_pct=0.05, traj=traj)

    out = build(db)

    assert out["reached_tp1"] == 1
    assert out["overshoot"]["went_further_1_25x"] == 1
    assert out["giveback"]["exit_near_breakeven"] == 1
    assert out["giveback"]["exit_below_tp1"] == 1
    # Вернули рынку весь ход ЗА TP1 и ещё немного — доля больше единицы.
    assert out["trades"][0]["gave_back_of_excess"] > 1.0


def test_buckets_sort_by_how_far_past_tp1(db):
    for mfe in (1.1, 1.3, 1.7, 2.5):
        _sig(db, tp1_pct=1.0, mfe=mfe, exit_pct=0.1,
             traj=[[0, 0], [10, mfe], [20, 0.1]])

    buckets = {b["ratio"]: b["n"] for b in build(db)["overshoot"]["buckets"]}

    assert buckets == {"1.00–1.25": 1, "1.25–1.50": 1, "1.50–2.00": 1, "2.00+": 1}


# ── трейл после TP1 ─────────────────────────────────────────────────────────

def test_a_trail_that_should_have_fired_is_reported_as_missed(db):
    """Ради этого отчёт. Трейл взводится на откате ≥ 0.4·1.98 = 0.792, то есть
    при цене ≤ 1.188%. На +1.18% условие наступило, а сделка доехала до
    безубытка. Это не рыночный риск, а неотработавшая ветка выхода."""
    traj = [[0, 0.0], [10, 0.8], [20, 1.98], [30, 1.18], [40, 0.9], [50, 0.05]]
    _sig(db, tp1_pct=0.73, mfe=1.98, exit_pct=0.05, traj=traj)

    trail = build(db)["post_tp1_trail"]

    assert trail["condition_met"] == 1
    assert trail["missed"] == 1
    assert trail["fired"] == 0
    assert trail["median_trigger_pct"] == pytest.approx(1.18)


def test_trades_closed_before_the_trail_existed_are_not_missed(db):
    """Трейл после TP1 появился 03.09. Сделка с той же траекторией, но без
    трейла в снимке конфига — не пропуск, а время до правила. Первая версия
    отчёта этого не различала и насчитала бы баг из четырёх сделок ленты,
    закрытых раньше, чем ветка появилась в коде."""
    traj = [[0, 0.0], [10, 0.8], [20, 1.98], [30, 1.18], [50, 0.05]]
    _sig(db, tp1_pct=0.73, mfe=1.98, exit_pct=0.05, traj=traj, trail_in_force=False)

    trail = build(db)["post_tp1_trail"]

    assert trail["in_force"] == 0
    assert trail["not_yet_in_force"] == 1
    assert trail["missed"] == 0


def test_the_rule_of_the_trade_is_used_not_todays_setting(db, monkeypatch):
    """Поменяли порог после закрытия — оценивать сделку по новому порогу нельзя."""
    monkeypatch.setattr(settings, "POST_TP1_TRAIL_GIVEBACK_SHARE", 0.9, raising=False)
    traj = [[0, 0.0], [10, 0.8], [20, 1.98], [30, 1.18], [50, 0.05]]
    _sig(db, tp1_pct=0.73, mfe=1.98, exit_pct=0.05, traj=traj)

    # По снимку (0.4) условие наступило на 1.18; при 0.9 не наступило бы вовсе.
    assert build(db)["post_tp1_trail"]["missed"] == 1


def test_a_trail_that_did_fire_is_not_missed(db):
    traj = [[0, 0.0], [10, 1.98], [20, 1.19]]
    _sig(db, tp1_pct=0.73, mfe=1.98, exit_pct=0.6, traj=traj,
         reason="post_tp1_giveback_trail")

    trail = build(db)["post_tp1_trail"]

    assert trail["fired"] == 1
    assert trail["missed"] == 0


def test_trail_only_counts_after_tp1_was_crossed(db):
    """Откат ДО пересечения TP1 — это другая ветка (до TP1), а не этот трейл."""
    trigger, _ = _trail_trigger(
        [[0, 0.0], [10, 0.7], [20, 0.3], [30, 0.2]], tp1_dist=1.0,
        min_mfe=0.6, share=0.4,
    )
    assert trigger is None


def test_below_min_mfe_the_trail_never_arms(db):
    trigger, _ = _trail_trigger(
        [[0, 0.0], [10, 0.55], [20, 0.1]], tp1_dist=0.5, min_mfe=0.6, share=0.4,
    )
    assert trigger is None


def test_tp2_closes_are_not_blamed_on_the_trail(db):
    """За TP2 работает своя лестница. Ставить её в вину трейлу после TP1 —
    значит найти «пропуск» там, где сработала другая ветка."""
    traj = [[0, 0], [10, 2.0], [20, 1.1], [30, 2.1]]
    _sig(db, tp1_pct=1.0, mfe=2.1, exit_pct=2.05, traj=traj, reason="tp2_reached")

    assert build(db)["post_tp1_trail"]["missed"] == 0


# ── честность цен ───────────────────────────────────────────────────────────

def test_exit_is_capped_at_the_peak(db):
    """tp2_reached книжит цену TP2, закрываясь на 92% пути. Выход не может быть
    лучше пика траектории."""
    _sig(db, tp1_pct=1.0, mfe=1.8, exit_pct=2.0, traj=[[0, 0], [10, 1.8]],
         reason="tp2_reached")

    assert build(db)["trades"][0]["exit_pct"] == pytest.approx(1.8)


def test_short_side_is_measured_in_its_own_direction(db):
    _sig(db, side="short", tp1_pct=1.0, mfe=1.6, exit_pct=0.1,
         traj=[[0, 0], [10, 1.6], [20, 0.1]])

    row = build(db)["trades"][0]

    assert row["tp1_dist_pct"] == pytest.approx(1.0)
    assert row["exit_pct"] == pytest.approx(0.1)
    assert row["reached_tp1"] is True


def test_trades_without_a_trajectory_record_are_skipped_not_guessed(db):
    signal = _sig(db, tp1_pct=1.0, mfe=1.5, exit_pct=0.1)
    signal.plan_json = {}
    db.flush()

    assert build(db)["closed_analysed"] == 0


# ── не дошедшие ─────────────────────────────────────────────────────────────

def test_near_misses_are_counted_separately(db):
    """Подошли к TP1 на 80%+ и не дотянули — это вопрос к месту TP1, а не к
    выходу, и для гейта на TP1 он первый."""
    _sig(db, tp1_pct=1.0, mfe=0.85, exit_pct=-0.8, partial=False,
         traj=[[0, 0], [10, 0.85], [20, -0.8]], reason="stop_loss")
    _sig(db, tp1_pct=1.0, mfe=0.3, exit_pct=-0.8, partial=False,
         traj=[[0, 0], [10, 0.3], [20, -0.8]], reason="stop_loss")

    out = build(db)

    assert out["reached_tp1"] == 0
    assert out["near_miss_80pct"] == 1


# ── альтернативы ────────────────────────────────────────────────────────────

def test_a_rule_that_fires_first_closes_the_rest_even_if_tp2_came_later(db):
    """Первая версия брала лучшее из двух — max(срабатывание, факт) — и
    приписывала правилу и защиту от отката, и весь последующий ход к TP2.
    Цена пересекла TP1 (1.0), дошла до 1.6, откатилась к 0.9 и ушла на TP2 2.5.
    Трейл (откат 0.7 ≥ 0.4·1.6) закрыл бы остаток на 0.9, стоп на TP1 — на 1.0.
    """
    traj = [[0, 0], [10, 1.0], [20, 1.6], [30, 0.9], [40, 2.5]]
    _sig(db, tp1_pct=1.0, mfe=2.5, exit_pct=2.5, traj=traj, reason="tp2_reached")

    cf = build(db)["counterfactuals"]

    assert cf["actual"]["sum_pct"] == pytest.approx(1.61, abs=1e-3)
    assert cf["partial_then_trail_at_market"]["sum_pct"] == pytest.approx(0.81, abs=1e-3)
    assert cf["partial_then_stop_at_tp1"]["sum_pct"] == pytest.approx(0.86, abs=1e-3)


def test_counterfactuals_use_the_same_trades_and_one_cost(db):
    """Половина на TP1 (1.0%), остаток ушёл на пик 2.0% и вернулся к 0.05%.
    Факт: 0.5·1.0 + 0.5·0.05 − 0.14 = 0.385.
    Стоп остатка на TP1: 0.5·1.0 + 0.5·1.0 − 0.14 = 0.86.
    Трейл по рынку на откате 0.8 от пика 2.0 (= 1.2%): 0.5·1.0 + 0.5·1.2 − 0.14.
    """
    traj = [[0, 0], [10, 1.0], [20, 2.0], [30, 1.2], [40, 0.05]]
    _sig(db, tp1_pct=1.0, mfe=2.0, exit_pct=0.05, traj=traj)

    cf = build(db)["counterfactuals"]

    assert cf["actual"]["sum_pct"] == pytest.approx(0.385, abs=1e-3)
    assert cf["partial_then_stop_at_tp1"]["sum_pct"] == pytest.approx(0.86, abs=1e-3)
    assert cf["partial_then_trail_at_market"]["sum_pct"] == pytest.approx(0.96, abs=1e-3)
    assert cf["all_out_at_tp1"]["sum_pct"] == pytest.approx(0.86, abs=1e-3)


# ── почему трейл не сработал: гейт нетто ────────────────────────────────────

_SWAP = {"taker_fee": 0.0005, "slippage_buffer_pct": 0.0002}
_DIP = [[0, 0.0], [10, 0.8], [20, 1.98], [30, 1.18], [50, 0.05]]


def test_a_small_remainder_is_blocked_by_the_net_gate(db):
    """Гейт меряет выход по полу издержек (0.30% swap), а не по рынку (1.18%).
    Остаток 100 USDT: 100·(0.0030 − 0.0012) = 0.18 < 0.25 — выход запрещён,
    хотя по рынку он дал бы 100·(0.0118 − 0.0012) = 1.06 USDT."""
    _sig(db, tp1_pct=0.73, mfe=1.98, exit_pct=0.05, traj=_DIP, market=_SWAP)

    out = build(db)
    trail, gate = out["post_tp1_trail"], out["trades"][0]["trail_gate"]

    assert trail["missed"] == 1
    assert trail["missed_blocked_by_net_gate"] == 1
    assert gate["remaining_notional_usdt"] == pytest.approx(100.0)
    assert gate["gate_booked_pct"] == pytest.approx(0.30)
    assert gate["gate_net_usdt"] == pytest.approx(0.18, abs=1e-4)
    assert gate["market_net_usdt"] == pytest.approx(1.06, abs=1e-4)


def test_a_large_remainder_passes_so_the_miss_has_another_cause(db):
    """Остаток 1000 USDT: 1000·0.0018 = 1.8 ≥ 0.25. Если трейл всё равно не
    сработал — гейт ни при чём, и это надо искать в другом месте."""
    _sig(db, tp1_pct=0.73, mfe=1.98, exit_pct=0.05, traj=_DIP, market=_SWAP,
         rest_qty=10.0)

    trail = build(db)["post_tp1_trail"]

    assert trail["missed_gate_would_pass"] == 1
    assert trail["missed_blocked_by_net_gate"] == 0


def test_without_fees_in_the_snapshot_the_gate_is_unknown_not_guessed(db):
    _sig(db, tp1_pct=0.73, mfe=1.98, exit_pct=0.05, traj=_DIP)

    trail = build(db)["post_tp1_trail"]

    assert trail["missed"] == 1
    assert trail["missed_gate_unknown"] == 1
    assert trail["missed_blocked_by_net_gate"] == 0


# ── частичная фиксация на TP1 ───────────────────────────────────────────────

def test_a_partial_that_was_enabled_but_never_happened_is_reported(db):
    """Фиксация включена в снимке, а записи о ней нет: ошибка исполнения
    глотается в ведении, и TP1 молча превращается в перенос стопа."""
    _sig(db, tp1_pct=1.0, mfe=1.5, exit_pct=0.07, partial=False,
         traj=[[0, 0], [10, 1.5], [20, 0.07]])

    health = build(db)["tp1_partial_health"]

    assert health == {"expected": 1, "missing": 1, "missing_ids": [health["missing_ids"][0]]}


def test_without_a_partial_the_gate_sees_the_whole_position(db):
    """position.qty не уменьшался — гейт в ведении видел всю позицию:
    200 USDT·0.0018 = 0.36 ≥ 0.25. Тогда гейт выход не запрещал."""
    _sig(db, tp1_pct=0.73, mfe=1.98, exit_pct=0.05, traj=_DIP, market=_SWAP,
         partial=False)

    out = build(db)
    gate = out["trades"][0]["trail_gate"]

    assert gate["notional_source"] == "full_position"
    assert gate["remaining_notional_usdt"] == pytest.approx(200.0)
    assert out["post_tp1_trail"]["missed_gate_would_pass"] == 1


def test_a_snapshot_without_fee_gets_it_from_the_trade_cost(db):
    """С 02.09 снимок писался без ставки (#snapshot-fee-2026-09-11). Круг
    издержек сделки 0.28 / 200 = 0.14% → ставка (0.0014 − 0.0002)/2 = 0.0006,
    это swap → пол 0.30, а не записанный по ошибке спотовый 0.60.
    Нетто гейта: 100·(0.0030 − 0.0014) = 0.16 < 0.25 — выход запрещён."""
    _sig(db, tp1_pct=0.73, mfe=1.98, exit_pct=0.05, traj=_DIP,
         market={"taker_fee": None, "slippage_buffer_pct": 0.0002})

    gate = build(db)["trades"][0]["trail_gate"]

    assert gate["fee_source"] == "derived_from_trade_cost"
    assert gate["fee_rate"] == pytest.approx(0.0006)
    assert gate["gate_booked_pct"] == pytest.approx(0.30)
    assert gate["gate_net_usdt"] == pytest.approx(0.16, abs=1e-4)
    assert gate["gate_pass"] is False


# ── кривая уровня стопа остатка ─────────────────────────────────────────────

def test_the_lock_curve_trades_protection_for_runners(db):
    """Две сделки, TP1 = 1.0, половина зафиксирована.
    Бегун: после TP1 откат до 0.6 и уход на TP2 2.5 — уровни 0.75 и 1.0 его
    срезают. Отдавший: пик 1.5 и безубыток 0.07 — любой уровень выше нуля его
    спасает. Лучший уровень — 0.5: спасает отдавшего и не трогает бегуна.
    """
    _sig(db, tp1_pct=1.0, mfe=2.5, exit_pct=2.5, reason="tp2_reached",
         traj=[[0, 0], [10, 1.0], [20, 1.6], [30, 0.6], [40, 2.5]])
    _sig(db, tp1_pct=1.0, mfe=1.5, exit_pct=0.07,
         traj=[[0, 0], [10, 1.0], [20, 1.5], [30, 0.07]])

    cf = build(db)["counterfactuals"]
    curve = {c["lock_frac_of_tp1"]: c["sum_pct"] for c in cf["lock_curve"]}

    assert curve[0.0] == pytest.approx(1.61 + 0.395, abs=1e-3)
    assert curve[0.5] == pytest.approx(1.61 + 0.61, abs=1e-3)
    assert curve[0.75] == pytest.approx(0.735 + 0.735, abs=1e-3)
    assert max(curve, key=curve.get) == 0.5
    # Точка 1.0 кривой — та же альтернатива «стоп на TP1».
    assert curve[1.0] == pytest.approx(cf["partial_then_stop_at_tp1"]["sum_pct"], abs=1e-6)


def test_the_pessimistic_bound_counts_a_dip_within_one_step_as_a_touch(db):
    """Бегун откатился до 1.03 при TP1 = 1.0 и ушёл на TP2. Траектория пишет
    точку каждые 0.05%, так что настоящий минимум мог быть 0.98 — ниже TP1.
    Оптимистично стоп на TP1 бегуна не задел, пессимистично — задел."""
    _sig(db, tp1_pct=1.0, mfe=2.5, exit_pct=2.5, reason="tp2_reached",
         traj=[[0, 0], [10, 1.0], [20, 1.6], [30, 1.03], [40, 2.5]])

    cf = build(db)["counterfactuals"]
    top = next(c for c in cf["lock_curve"] if c["lock_frac_of_tp1"] == 1.0)

    assert top["vs_actual_pct"] == pytest.approx(0.0, abs=1e-6)
    # 0.5 остатка теряют 2.5 − 1.0 = 1.5 → −0.75.
    assert top["vs_actual_pessimistic_pct"] == pytest.approx(-0.75, abs=1e-6)
