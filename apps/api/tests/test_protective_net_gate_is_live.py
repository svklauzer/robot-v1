"""Экономический гейт защитных выходов обязан РЕАЛЬНО работать в бою
(#protective-net-gate-dead-2026-09-03).

ExitPolicyService считает `_estimated_net_usdt(...)` и не фиксирует защитным
выходом, если результат в USDT ниже MIN_PROTECTIVE_NET_USDT. Это поведение уже
было покрыто тестом (test_backstop_respects_net_usdt_floor) — но тест САМ
передавал `position_notional_usdt`, а боевой вызов из SignalLifecycleManager
его не передавал никогда. `est_net` всегда был None, ветка
`est_net is None or est_net >= floor` всегда истинна, гейт не отработал ни разу.

Худший вид мёртвой настройки: значение выставлено, откалибровано в комментарии
(#gate-recalib-2026-07-25), покрыто зелёным тестом — и не влияет ни на что.

Цена в бою (OKX, 02–03.09.2026): защитные выходы фиксировали ОТРИЦАТЕЛЬНЫЙ
нетто, ровно то, что гейт обязан предотвращать. ADA #464 — gross 0.0, net −0.33
(чистая комиссия). ETH #474 — net −0.02, закрыт за 8 часов до хода, накрывшего
и TP1 (2437), и TP2 (2447); план по TP2 был +3.59 USDT.

Этот файл проверяет СВЯЗЬ вызова, а не саму формулу: формула уже под тестом.
"""
from __future__ import annotations

import inspect

from services.exit_policy import ExitPolicyService
from services.signal_lifecycle import SignalLifecycleManager


# ── связь: боевой вызов передаёт номинал ────────────────────────────────────

def test_lifecycle_passes_position_notional_to_exit_policy():
    """Без этого аргумента весь экономический гейт — мёртвый код."""
    src = inspect.getsource(SignalLifecycleManager._process_signal_core)

    # tz_context — последний аргумент вызова, поэтому им и ограничиваем окно:
    # split(")") здесь не годится, внутри аргументов есть свои скобки.
    call = src.split("before_tp1_decision(", 1)[1].split("tz_context=", 1)[0]
    assert "position_notional_usdt=" in call, (
        "before_tp1_decision снова зовут без номинала — MIN_PROTECTIVE_NET_USDT "
        "перестанет влиять на защитные выходы, молча"
    )


def test_exit_policy_still_accepts_the_argument():
    """Имя параметра на обеих сторонах одно. Разъедутся — гейт снова умрёт."""
    params = inspect.signature(ExitPolicyService.before_tp1_decision).parameters
    assert "position_notional_usdt" in params


# ── сам расчёт номинала ─────────────────────────────────────────────────────

class _Position:
    def __init__(self, qty, entry_price, status="open"):
        self.qty = qty
        self.entry_price = entry_price
        self.status = status


class _Signal:
    id = 1
    symbol = "ADA/USDT"
    side = "short"
    qty = None
    entry_zone_json = {"from": 0.1956, "to": 0.1959}
    plan_json = {}


def _manager_with_position(position):
    manager = SignalLifecycleManager.__new__(SignalLifecycleManager)
    manager._get_open_position_for_signal = lambda db, signal: position
    manager._get_latest_position_for_signal = lambda db, signal: position
    return manager


def test_notional_comes_from_the_real_filled_position():
    """Источник размера — фактический филл, а не план: план мог быть пересчитан."""
    manager = _manager_with_position(_Position(qty=1200.0, entry_price=0.1956))

    notional = manager._position_notional_usdt(None, _Signal())

    assert notional == 1200.0 * 0.1956


def test_notional_falls_back_to_signal_plan_without_position():
    manager = _manager_with_position(None)
    signal = _Signal()
    signal.qty = 0.1

    assert manager._position_notional_usdt(None, signal, entry_price=2409.32) == 0.1 * 2409.32


def test_unknown_size_returns_none_and_keeps_fail_open():
    """None = «размер неизвестен». Гейт обязан остаться fail-open: неизвестность
    не имеет права заблокировать защитный выход вслепую."""
    manager = _manager_with_position(None)

    assert manager._position_notional_usdt(None, _Signal(), entry_price=None) is None


def test_zero_or_negative_size_is_not_a_notional():
    manager = _manager_with_position(_Position(qty=0.0, entry_price=0.1956))

    assert manager._position_notional_usdt(None, _Signal()) is None


# ── регресс на боевой сценарий ──────────────────────────────────────────────

def _tz_ctx_intact():
    """Структура тренда цела: kama=100 с буфером 0.5% ломается ниже 99.5,
    adx_peak=20 много ниже TZ_EXIT_ADX_PEAK_MIN=35 — структурные выходы ТЗ
    молчать обязаны, и решение доходит до бэкстопа (та же фикстура, что в
    test_exit_policy.py)."""
    return {
        "kama": 100.0, "adx": 20.0, "adx_peak": 20.0,
        "obv": 1000.0, "obv_ema20": 800.0,
    }


def _tz_setup(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "TZ_TREND_EXIT_ONLY", True, raising=False)
    monkeypatch.setattr(settings, "TZ_USE_DYNAMIC_ATR_STOPS", False, raising=False)
    monkeypatch.setattr(settings, "TZ_EXIT_KAMA_BUFFER_PCT", 0.50, raising=False)
    monkeypatch.setattr(settings, "TZ_EXIT_ADX_PEAK_MIN", 35.0, raising=False)
    monkeypatch.setattr(settings, "TZ_EXIT_CONDITIONS", "kama,adx", raising=False)
    monkeypatch.setattr(settings, "TZ_MFE_GIVEBACK_BACKSTOP_ENABLED", True, raising=False)


def test_breakeven_giveback_no_longer_banks_a_negative_net(monkeypatch):
    """Регресс ровно на ADA #464: MFE 0.61% отдан полностью, цена вернулась к
    входу. Фиксировать тут нечего — gross 0, нетто равен минус комиссии.

    С живым гейтом (номинал 234.84 USDT, порог 0.25) выход не имеет права
    сработать: est_net отрицателен.
    """
    from core.config import settings

    _tz_setup(monkeypatch)
    monkeypatch.setattr(settings, "MIN_PROTECTIVE_NET_USDT", 0.25, raising=False)

    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=100.0,      # вернулись ровно к входу: gross = 0
        stop_price=95.0,
        mfe_pct=0.61,
        trade_mode="trend",
        symbol=None,
        market_type="swap",
        position_notional_usdt=234.84,
        tz_context=_tz_ctx_intact(),
    )

    assert decision.exit is False, (
        "защитный выход зафиксировал бы отрицательный нетто — это и есть тот "
        "сценарий, ради которого MIN_PROTECTIVE_NET_USDT существует"
    )


def test_same_setup_without_notional_stays_fail_open(monkeypatch):
    """Контроль: без номинала поведение прежнее (выход разрешён). Иначе фикс
    молча превратился бы в блокировку выходов там, где размер неизвестен."""
    _tz_setup(monkeypatch)

    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=100.0,
        stop_price=95.0,
        mfe_pct=0.61,
        trade_mode="trend",
        symbol=None,
        market_type="swap",
        tz_context=_tz_ctx_intact(),
    )

    assert decision.exit is True
    assert decision.reason == "tz_mfe_giveback_backstop"


def test_real_profit_still_gets_protected(monkeypatch):
    """Обратная сторона: гейт не должен превратиться в «никогда не фиксируем».
    Те же долевые условия отдачи, но позиция крупная — остаток прибыли в USDT
    выше порога, и выход обязан сработать как раньше."""
    from core.config import settings

    _tz_setup(monkeypatch)
    monkeypatch.setattr(settings, "MIN_PROTECTIVE_NET_USDT", 0.25, raising=False)

    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=100.2,      # +0.20%: внутри net_safe, но на 5000 USDT
        stop_price=95.0,          # это ~4 USDT нетто — заметно выше порога 0.25
        mfe_pct=2.0,
        trade_mode="trend",
        symbol=None,
        market_type="swap",
        position_notional_usdt=5000.0,
        tz_context=_tz_ctx_intact(),
    )

    assert decision.exit is True
    assert decision.reason == "tz_mfe_giveback_backstop"
