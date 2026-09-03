"""TP2 — этап, а не потолок (#progressive-tp2-2026-09-03)
и защита прибыли между TP1 и TP2 (#post-tp1-dead-zone-2026-09-03).

Владелец: «почему всё может закончиться на TP2, если TP динамические?»
Вопрос точный, и ответ хуже, чем кажется.

Уровни TP действительно считаются динамически (tp2_dynamic, r_mult из
ADX/ATR/KAMA), но динамика живёт только на этапе ПЛАНИРОВАНИЯ. После публикации
tp_json заморожен, а выход трактовал tp2 как жёсткий потолок в двух независимых
местах — signal_lifecycle (закрытие по цене РОВНО tp2) и exit_policy (0.92×tp2).
Цель адаптивна между сделками и неподвижна внутри сделки, тогда как стоп
адаптивен и внутри. Асимметрия стоила правого хвоста целиком.

Худшее следствие — ЦЕНЗУРА СТАТИСТИКИ: гейт входа tp2_reached_too_rarely меряет,
как часто TP2 достигается, но TP2 одновременно и выход, значит «TP2 превышен»
ненаблюдаемо в принципе. Сделка, которая прошла бы 3×TP2, записывается ровно как
TP2. Замкнутый контур: потолок → срезанный хвост → низкая измеренная отдача →
гейт блокирует входы.

Вторая дыра, найденная попутно: три защитные ветки after_tp1_decision требуют
MFE ≥ 2.0% / ≥ 3.0% или стоп ≥ 3.0%, при медианном MFE наших режимов 0.52–1.19%
и типичном стопе 0.6–0.9%. Ни одна не могла взвестись на обычной сделке — после
TP1 было ровно два исхода: TP2 или сползание на безубыток. Боевые подтверждения:
ETH #470 MFE 0.86%→+0.26%, BTC #459 MFE 1.19%→+0.40%, ETH #460 MFE 1.23%→+0.22%.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services.exit_policy import ExitPolicyService
from services.signal_lifecycle import SignalLifecycleManager


# ── зона TP1→TP2 больше не мёртвая ──────────────────────────────────────────

def _svc():
    return ExitPolicyService()


def _mgr():
    return SignalLifecycleManager.__new__(SignalLifecycleManager)


@pytest.fixture
def post_tp1(monkeypatch):
    monkeypatch.setattr(settings, "POST_TP1_TRAIL_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "POST_TP1_TRAIL_MIN_MFE_PCT", 0.60, raising=False)
    monkeypatch.setattr(settings, "POST_TP1_TRAIL_GIVEBACK_SHARE", 0.40, raising=False)
    monkeypatch.setattr(settings, "MIN_PROTECTIVE_NET_USDT", 0.25, raising=False)


def test_typical_winner_no_longer_slides_to_breakeven(post_tp1):
    """Регресс на ETH #470: MFE 0.86%, откат — и раньше сделка доезжала до
    безубытка, потому что все ярусы требовали MFE ≥ 2%."""
    decision = _svc().after_tp1_decision(
        side="short",
        entry_price=100.0,
        current_price=99.60,     # +0.40% при MFE 0.86% → отдано 0.46 (53% > 40%)
        tp2_price=97.5,
        lifecycle={"mfe_pct": 0.86},
        symbol=None,
        market_type="swap",
        position_notional_usdt=240.0,
    )

    assert decision.exit is True
    assert decision.reason == "post_tp1_giveback_trail"


def test_modest_pullback_still_rides(post_tp1):
    """Тренд, который просто «дышит», резать рано: отдано 23% < 40%."""
    decision = _svc().after_tp1_decision(
        side="short",
        entry_price=100.0,
        current_price=99.34,     # +0.66% при MFE 0.86% → отдано 0.20
        tp2_price=97.5,
        lifecycle={"mfe_pct": 0.86},
        symbol=None,
        market_type="swap",
        position_notional_usdt=240.0,
    )

    assert decision.exit is False


def test_move_too_small_to_protect(post_tp1):
    """MFE 0.4% < порога 0.6%: защищать нечего, издержки съедят фиксацию."""
    decision = _svc().after_tp1_decision(
        side="short",
        entry_price=100.0,
        current_price=99.95,
        tp2_price=97.5,
        lifecycle={"mfe_pct": 0.40},
        symbol=None,
        market_type="swap",
        position_notional_usdt=240.0,
    )

    assert decision.exit is False


def test_post_tp1_trail_respects_the_net_gate(post_tp1, monkeypatch):
    """Тот же экономический гейт, что и у прочих защитных веток: фиксировать в
    минус по комиссии смысла нет — лучше дождаться TP2 или стопа."""
    monkeypatch.setattr(settings, "MIN_PROTECTIVE_NET_USDT", 50.0, raising=False)

    decision = _svc().after_tp1_decision(
        side="short",
        entry_price=100.0,
        current_price=99.60,
        tp2_price=97.5,
        lifecycle={"mfe_pct": 0.86},
        symbol=None,
        market_type="swap",
        position_notional_usdt=10.0,
    )

    assert decision.exit is False


def test_flag_off_restores_the_dead_zone(post_tp1, monkeypatch):
    monkeypatch.setattr(settings, "POST_TP1_TRAIL_ENABLED", False, raising=False)

    decision = _svc().after_tp1_decision(
        side="short",
        entry_price=100.0,
        current_price=99.60,
        tp2_price=97.5,
        lifecycle={"mfe_pct": 0.86},
        symbol=None,
        market_type="swap",
        position_notional_usdt=240.0,
    )

    assert decision.exit is False


# ── after_tp2_decision: выход по затуханию хвоста ───────────────────────────

@pytest.fixture
def tp2_trail(monkeypatch):
    monkeypatch.setattr(settings, "TP2_TRAIL_GIVEBACK_SHARE", 0.40, raising=False)


def test_tail_rides_while_it_holds(tp2_trail):
    """Хвост прошёл сверх TP2 и держится — не трогаем."""
    decision = _svc().after_tp2_decision(
        side="long",
        entry_price=100.0,
        tp2_price=102.0,
        peak_price=105.0,     # прирост сверх TP2 = 3 п.п.
        current_price=104.5,  # отдано 0.5 из 3 (17% < 40%)
    )

    assert decision.exit is False


def test_tail_is_banked_when_it_fades(tp2_trail):
    """Отдал 1.5 из 3 п.п. прироста (50% ≥ 40%) — забираем."""
    decision = _svc().after_tp2_decision(
        side="long",
        entry_price=100.0,
        tp2_price=102.0,
        peak_price=105.0,
        current_price=103.5,
    )

    assert decision.exit is True
    assert decision.reason == "tp2_trail_giveback"
    assert decision.exit_price == pytest.approx(103.5)


def test_giveback_is_measured_from_the_run_not_total_mfe(tp2_trail):
    """Доля считается от ПРИРОСТА СВЕРХ TP2, а не от полного MFE. Иначе порог
    зависел бы от того, как далеко стоял TP2, а не от того, сколько дал хвост:
    при близком TP2 любая свеча выглядела бы обвалом.

    Здесь полный MFE 5%, но прирост сверх TP2 всего 0.5 п.п. — и отдача 0.3 п.п.
    это 60% ХВОСТА, а не 6% MFE. Выход обязан сработать."""
    decision = _svc().after_tp2_decision(
        side="long",
        entry_price=100.0,
        tp2_price=104.5,
        peak_price=105.0,
        current_price=104.7,
    )

    assert decision.exit is True


def test_short_side_is_symmetric(tp2_trail):
    decision = _svc().after_tp2_decision(
        side="short",
        entry_price=100.0,
        tp2_price=98.0,
        peak_price=95.0,      # прирост сверх TP2 = 3 п.п.
        current_price=96.5,   # отдано 1.5 (50%)
    )

    assert decision.exit is True
    assert decision.reason == "tp2_trail_giveback"


def test_no_peak_means_no_decision(tp2_trail):
    """Нет пика — нет данных. Не закрываем вслепую."""
    decision = _svc().after_tp2_decision(
        side="long", entry_price=100.0, tp2_price=102.0,
        peak_price=None, current_price=103.0,
    )

    assert decision.exit is False


# ── храповик стопа: только в сторону прибыли ────────────────────────────────

class _Sig:
    def __init__(self, stop_price):
        self.stop_price = stop_price


def test_ratchet_pulls_the_stop_up_for_a_long():
    signal = _Sig(stop_price=100.05)          # безубыток после TP1
    moved = _mgr()._ratchet_tp2_stop(signal, "long", peak_price=105.0, buffer_pct=1.0)

    assert moved is True
    assert signal.stop_price == pytest.approx(103.95)


def test_ratchet_never_moves_the_stop_backwards():
    """Храповик: откат цены не имеет права ослабить уже достигнутую защиту."""
    signal = _Sig(stop_price=103.95)
    moved = _mgr()._ratchet_tp2_stop(signal, "long", peak_price=104.0, buffer_pct=1.0)

    assert moved is False
    assert signal.stop_price == pytest.approx(103.95)


def test_ratchet_is_symmetric_for_a_short():
    signal = _Sig(stop_price=99.95)
    moved = _mgr()._ratchet_tp2_stop(signal, "short", peak_price=95.0, buffer_pct=1.0)

    assert moved is True
    assert signal.stop_price == pytest.approx(95.95)


def test_tp2_level_can_never_be_given_back():
    """Ключевое требование владельца: «не отдавать обратно то, что зафиксировано
    на этапе TP2». Пик стартует ровно с TP2, храповик не отходит назад — значит
    защищённый уровень конструктивно не может опуститься ниже TP2 минус буфер."""
    signal = _Sig(stop_price=100.05)
    manager = _mgr()

    manager._ratchet_tp2_stop(signal, "long", peak_price=102.0, buffer_pct=0.5)
    floor_at_tp2 = signal.stop_price

    # Цена сходила выше и вернулась — защита обязана остаться на достигнутом.
    manager._ratchet_tp2_stop(signal, "long", peak_price=106.0, buffer_pct=0.5)
    manager._ratchet_tp2_stop(signal, "long", peak_price=102.5, buffer_pct=0.5)

    assert signal.stop_price > floor_at_tp2


# ── ширина трейла ───────────────────────────────────────────────────────────

def test_trail_width_scales_with_the_signal_own_tp_leg(monkeypatch):
    """Масштаб берём из отрезка TP1→TP2 самого сигнала: уровни TP уже
    динамические, значит длина отрезка и есть волатильность в цене."""
    monkeypatch.setattr(settings, "TP2_TRAIL_LEG_SHARE", 0.5, raising=False)
    monkeypatch.setattr(settings, "TP2_TRAIL_MIN_BUFFER_PCT", 0.20, raising=False)

    # отрезок 2% от цены входа → трейл 1%
    assert _mgr()._tp2_trail_buffer_pct(tp1=100.0, tp2=102.0, entry_price=100.0) == pytest.approx(1.0)


def test_trail_width_has_a_floor(monkeypatch):
    """Слишком узкий отрезок TP1→TP2 не имеет права дать трейл в доли базиса —
    такой стоп выбьет спредом на первом же тике."""
    monkeypatch.setattr(settings, "TP2_TRAIL_LEG_SHARE", 0.5, raising=False)
    monkeypatch.setattr(settings, "TP2_TRAIL_MIN_BUFFER_PCT", 0.20, raising=False)

    assert _mgr()._tp2_trail_buffer_pct(tp1=100.0, tp2=100.05, entry_price=100.0) == pytest.approx(0.20)


def test_broken_levels_fall_back_to_the_floor(monkeypatch):
    monkeypatch.setattr(settings, "TP2_TRAIL_MIN_BUFFER_PCT", 0.20, raising=False)

    assert _mgr()._tp2_trail_buffer_pct(tp1=None, tp2=102.0, entry_price=100.0) == pytest.approx(0.20)
    assert _mgr()._tp2_trail_buffer_pct(tp1=100.0, tp2=102.0, entry_price=0.0) == pytest.approx(0.20)


# ── связь: жёсткое закрытие на TP2 действительно убрано ─────────────────────

def test_lifecycle_no_longer_hard_closes_on_tp2():
    """Сторож на возврат потолка: если ветка снова начнёт безусловно закрывать
    по tp2, правый хвост опять срежется, а статистика входа снова окажется
    цензурированной — молча, потому что тесты уровней это не ловят."""
    import inspect

    src = inspect.getsource(SignalLifecycleManager._process_signal_core)
    branch = src.split('elif signal.status in ["tp1", "breakeven"]:', 1)[1]

    assert "_start_tp2_trail" in branch, "этап TP2 больше не вызывается"
    assert "not tp2_partial" in branch, (
        "условие достижения TP2 снова безусловное — этап отработает повторно"
    )


def test_progressive_tp2_can_be_switched_off():
    from core.config import Settings

    assert Settings().TP2_PROGRESSIVE_ENABLED is True
    assert 0.0 < Settings().TP2_PARTIAL_CLOSE_SHARE <= 1.0
