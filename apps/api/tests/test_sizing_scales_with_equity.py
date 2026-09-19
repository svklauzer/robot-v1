"""Размер позиции масштабируется со счётом и плечом (#sizing-scales-with-equity-2026-09-19).

Требование владельца: «любое плечо, любой депозит — система распределяет
средства автоматически и не привязывается к сумме депозита; всё, что на торговом
счету, то она и использует, с ростом позиций».

Потолок нотионала одного ордера был абсолютным (250 USDT). На счёте 300 с плечом
5 он резал позицию до шестой части доступной экспозиции, а на счёте 30 000
держал систему на том же размере, что и на 300. Доля от экспозиции решает обе
задачи одной настройкой.
"""
from __future__ import annotations

import inspect

import pytest

from core.config import settings


@pytest.fixture
def pct(monkeypatch):
    def _set(value: float):
        monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_PCT", value)
    return _set


def test_absolute_cap_stays_until_the_share_is_switched_on(pct):
    """Ноль по умолчанию: включение меняет размер позиций, то есть эпоху."""
    pct(0.0)
    assert settings.max_order_notional(3000, 1) == settings.LIVE_MAX_ORDER_NOTIONAL_USDT
    assert settings.max_order_notional(300, 5) == settings.LIVE_MAX_ORDER_NOTIONAL_USDT


def test_share_of_exposure_replaces_the_absolute_cap(pct):
    pct(20.0)
    # 300 USDT × 5 = 1500 экспозиции, пятая часть на ордер.
    assert settings.max_order_notional(300, 5) == pytest.approx(300.0)


def test_cap_grows_with_the_deposit_and_with_the_leverage(pct):
    pct(20.0)
    assert settings.max_order_notional(600, 5) == pytest.approx(600.0)   # вдвое больше счёт
    assert settings.max_order_notional(300, 10) == pytest.approx(600.0)  # вдвое больше плечо
    assert settings.max_order_notional(30000, 5) == pytest.approx(30000.0)


def test_unknown_equity_falls_back_to_the_absolute_cap(pct):
    """В live баланс мог не прочитаться. Предохранитель обязан работать и там,
    где размер счёта в этот момент неизвестен."""
    pct(20.0)
    assert settings.max_order_notional(None, 5) == settings.LIVE_MAX_ORDER_NOTIONAL_USDT
    assert settings.max_order_notional(0, 5) == settings.LIVE_MAX_ORDER_NOTIONAL_USDT


def test_leverage_below_one_never_shrinks_the_cap(pct):
    pct(20.0)
    assert settings.max_order_notional(1000, 0) == pytest.approx(200.0)
    assert settings.max_order_notional(1000, None) == pytest.approx(200.0)


def test_sizing_and_submission_use_the_same_formula():
    """Расхождение между сайзингом и отправкой уже стоило отклонённых ордеров в
    live при полном плане в бумаге (#live-notional-parity-2026-08-04)."""
    from services import live_executor, trade_plan

    assert "settings.max_order_notional(balance_usdt, leverage_value)" in inspect.getsource(trade_plan)
    # Оба типа ордера идут одним телом (#limit-entry-2026-09-19), потолок там же.
    assert "settings.max_order_notional(" in inspect.getsource(live_executor.LiveExecutor._place)


def test_submission_asks_for_the_balance_only_when_the_share_is_on():
    """Иначе на каждый ордер уходил бы лишний запрос баланса."""
    source = inspect.getsource(__import__("services.live_executor", fromlist=["x"]).LiveExecutor._place)
    head = source.split("cap = float(settings.max_order_notional", 1)[0]
    assert 'LIVE_MAX_ORDER_NOTIONAL_PCT' in head, "капитал спрашивается без проверки доли"


def test_preflight_reports_the_effective_cap():
    from services import live_preflight

    source = inspect.getsource(live_preflight.LivePreflight._check_capital)
    assert "settings.max_order_notional(capital, leverage)" in source
    assert "order_cap_scales_with_equity" in source


def test_blueprint_carries_the_setting():
    """Торговая настройка живёт в config.py и в render.yaml одновременно."""
    from pathlib import Path

    blueprint = (Path(__file__).resolve().parents[3] / "render.yaml").read_text(encoding="utf-8")
    block = blueprint.split("key: LIVE_MAX_ORDER_NOTIONAL_PCT", 1)[1].split("- key:", 1)[0]
    assert 'value: "0"' in block


# ── запас экономики сделки: доля номинала вместо суммы ──────────────────────
def _signal(**over):
    base = {"symbol": "XRP/USDT", "side": "long", "grade": "B", "confidence": 70.0,
            "rationale": "", "required_margin": 90.0, "leverage": 1,
            "net_rr_tp1": 2.0, "net_rr_tp2": 3.0,
            "net_pnl_tp1": 1.7, "net_pnl_tp2": 1.7, "net_pnl_stop": -1.2}
    return {**base, **over}


_STATE = {"equity_usdt": 300.0, "used_margin_usdt": 0.0, "daily_pnl_usdt": 0.0,
          "drawdown_pct": 0.0, "open_positions_count": 0, "active_signals_by_symbol": {}}


def _cfg(**over):
    from services.anti_drain_guard import AntiDrainConfig

    return AntiDrainConfig(min_confidence=0.0, max_position_margin_pct=100.0,
                           max_used_margin_pct=100.0, max_open_positions=5,
                           economics_use_tp2=True, **over)


def test_absolute_edge_floor_blocks_a_small_account():
    """Ровно та привязка, которую просили убрать: 1.20 USDT запаса при номинале
    90 — это больше процента от позиции, и сделка не проходит."""
    from services.anti_drain_guard import should_open_signal

    allowed, reason = should_open_signal(_signal(), _STATE, _cfg(min_expected_edge_after_costs_usdt=1.20))

    assert allowed is False and reason == "blocked_bad_trade_economics"


def test_share_of_notional_judges_the_same_trade_by_its_size():
    """0.48% номинала — то же требование, что 1.20 USDT при номинале 250."""
    from services.anti_drain_guard import should_open_signal

    allowed, _ = should_open_signal(
        _signal(), _STATE,
        _cfg(min_expected_edge_after_costs_usdt=1.20, min_expected_edge_after_costs_pct=0.48),
    )

    assert allowed is True, "запас 0.43 USDT на номинал 90 — сделка проходит"


def test_the_share_scales_with_leverage():
    """Плечо меняет номинал, а с ним и масштаб издержек, от которых защищает запас."""
    from services.anti_drain_guard import should_open_signal

    cfg = _cfg(min_expected_edge_after_costs_pct=0.48)
    # Та же маржа при плече 5 — номинал 450, запас 2.16 USDT: прибыли 1.7 мало.
    allowed, reason = should_open_signal(_signal(leverage=5), _STATE, cfg)

    assert allowed is False and reason == "blocked_bad_trade_economics"


def test_absolute_floor_still_works_when_the_share_is_off():
    from services.anti_drain_guard import should_open_signal

    allowed, _ = should_open_signal(
        _signal(net_pnl_tp2=5.0), _STATE, _cfg(min_expected_edge_after_costs_usdt=1.20))

    assert allowed is True


def test_loop_and_decision_card_carry_the_share():
    """Порог, по которому судят сделку, должен быть виден в её карточке."""
    from services import decision_config
    from workers import robot_loop

    assert "min_expected_edge_after_costs_pct=" in inspect.getsource(robot_loop)
    assert '"min_edge_after_costs_pct"' in inspect.getsource(decision_config)
