"""Блокировка anti-drain объясняет себя числами (#anti-drain-figures-2026-09-08).

07.09 в ленте стояло `blocked_position_margin_limit` по BTC с payload
`{"symbol","status","decision","anti_drain":true}` — и всё. Сколько маржи
требовалось, какой был потолок и от какого капитала он считался, из записи
восстановить было нельзя; владельцу пришлось спрашивать.

Все остальные гейты печатают свои числа: `adx=12.2<20.00`,
`di_spread=8.49<15.00`, `depth_no_bid_support:obi=-0.651(wall=0.41)`. Этот молчал,
хотя величины лежали в той же области видимости.

Отдельно закреплён `sizing_budget_usdt`: план строится под динамический бюджет,
а отвергается по потолку anti-drain. Когда бюджет выше потолка, между ними
мёртвая полоса — план собирается и гарантированно отвергается. Ровно эту
коллизию уже ловили 28.07 с другой стороны (SCALP_ANTI_DRAIN_MAX_POSITION_MARGIN_PCT),
и там она стоила двенадцати часов без сделок.
"""
from __future__ import annotations

import inspect
import re

from workers import robot_loop


def _anti_drain_block_source() -> str:
    """Кусок цикла, где пишется событие отказа anti-drain."""
    src = inspect.getsource(robot_loop)
    start = src.index('decision=anti_reason')
    return src[start : src.index("db.flush()", start)]


def test_the_block_records_what_it_compared():
    block = _anti_drain_block_source()

    for field in ("required_margin_usdt", "max_position_margin_pct",
                  "used_margin_usdt", "max_used_margin_pct", "equity_usdt"):
        assert field in block, f"в записи нет величины, по которой принято решение: {field}"


def test_the_block_records_the_budget_the_plan_was_built_for():
    """Без этого числа мёртвая полоса между бюджетом сайзинга и потолком гварда
    не видна: в ленте будет просто «отказано», а причина — что цель выше потолка."""
    assert "sizing_budget_usdt" in _anti_drain_block_source()


def test_percentages_are_compared_against_percentages():
    """Гвард делит на equity и умножает на 100, то есть ЖДЁТ проценты. Часть
    настроек сайзинга рядом хранится долями (0.13, 0.85), и подстановка доли
    сюда превратила бы потолок 13% в 0.13% — блокировку всего подряд.
    """
    guard = inspect.getsource(robot_loop)
    caps = re.findall(r"max_position_margin_pct=\(?\s*\n?\s*float\(getattr\(settings, \"([A-Z_]+)\"", guard)
    assert caps, "не найдено, откуда берётся пер-позишн потолок"

    from core.config import settings
    for name in caps:
        value = float(getattr(settings, name))
        assert value > 1.0, f"{name}={value} похоже на долю, а гвард сравнивает с процентом"


def test_the_guard_still_blocks_an_oversized_position():
    """Сам предохранитель не тронут: правка касается только записи."""
    from services.anti_drain_guard import AntiDrainConfig, should_open_signal

    cfg = AntiDrainConfig(max_position_margin_pct=70.0, min_confidence=0.0)
    signal = {"symbol": "BTC/USDT", "side": "short", "grade": "B", "confidence": 60.0,
              "rationale": "", "required_margin": 800.0,
              "net_rr_tp1": 2.0, "net_rr_tp2": 3.0,
              "net_pnl_tp1": 5.0, "net_pnl_tp2": 9.0, "net_pnl_stop": -3.0}
    state = {"equity_usdt": 950.0, "used_margin_usdt": 0.0, "daily_pnl_usdt": 0.0,
             "drawdown_pct": 0.0, "open_positions_count": 0,
             "active_signals_by_symbol": {}}

    allowed, reason = should_open_signal(signal, state, cfg)

    assert allowed is False
    assert reason == "blocked_position_margin_limit"
    # 800 / 950 = 84.2% — выше потолка 70%. Ровно случай BTC 07.09.
    assert 800.0 / 950.0 * 100 > cfg.max_position_margin_pct


def test_a_position_inside_the_cap_passes():
    from services.anti_drain_guard import AntiDrainConfig, should_open_signal

    cfg = AntiDrainConfig(max_position_margin_pct=70.0, min_confidence=0.0)
    signal = {"symbol": "BTC/USDT", "side": "short", "grade": "B", "confidence": 60.0,
              "rationale": "", "required_margin": 400.0,
              "net_rr_tp1": 2.0, "net_rr_tp2": 3.0,
              "net_pnl_tp1": 5.0, "net_pnl_tp2": 9.0, "net_pnl_stop": -3.0}
    state = {"equity_usdt": 950.0, "used_margin_usdt": 0.0, "daily_pnl_usdt": 0.0,
             "drawdown_pct": 0.0, "open_positions_count": 0,
             "active_signals_by_symbol": {}}

    assert should_open_signal(signal, state, cfg)[0] is True
