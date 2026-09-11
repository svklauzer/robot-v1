"""Стенд динамических SL/TP на свечах истории (#dynamic-levels-2026-09-12).

Проверяется механика правил на синтетических путях цены: стоп до TP1, половина
на TP1 и стоп остатка на нём, TP2, трейл по ATR, выход по KAMA, режимы
одновременного касания и отсутствие заглядывания вперёд у ATR.
"""
from __future__ import annotations

import pytest

from research.candle_replay import (
    Trade, atr_pct, net_pct, path_pct, replay, trade_from_item,
)


def _trade(side="long", stop=1.0, tp1=1.0, tp2=2.0, cost=0.14):
    return Trade(id=1, symbol="X/USDT", side=side, entry=100.0, stop_dist_pct=stop,
                 tp1_pct=tp1, tp2_pct=tp2, opened_ts=0, closed_ts=0, cost_pct=cost,
                 actual_pct=0.0)


def _bar(fav, adv, close=None, open_=0.0):
    return (open_, fav, adv, close if close is not None else (fav + adv) / 2)


def test_a_stop_before_tp1_loses_the_stop_distance():
    res = replay(_trade(), [_bar(0.5, -1.2)], "current")
    assert res.gross_pct() == pytest.approx(-1.0)


def test_half_at_tp1_then_the_rest_on_the_tp1_stop():
    res = replay(_trade(), [_bar(1.1, 0.2, 1.0), _bar(1.05, 0.9)], "current")
    assert res.gross_pct() == pytest.approx(0.5 * 1.0 + 0.5 * 1.0)


def test_half_at_tp1_then_tp2():
    res = replay(_trade(), [_bar(1.1, 0.2, 1.0), _bar(2.1, 1.2)], "current")
    assert res.gross_pct() == pytest.approx(0.5 * 1.0 + 0.5 * 2.0)


def test_the_same_bar_touch_depends_on_the_mode():
    path = [_bar(1.2, -1.2)]                      # и TP1, и стоп в одной свече
    assert replay(_trade(), path, "current", pessimistic=True).gross_pct() == pytest.approx(-1.0)
    assert replay(_trade(), path, "current", pessimistic=False).exits[0].reason == "tp1"


def test_atr_trail_rides_past_tp2_and_trails_the_rest():
    """Тренд: TP1, потом ровный рост по 0.5% за свечу (ATR ≈ 0.5), потом
    откат. Трейл 2·ATR от максимума закрытия выпускает остаток выше TP2."""
    path = [_bar(0.3, -0.1, 0.2) for _ in range(15)]            # прогрев ATR
    path += [_bar(1.1, 0.5, 1.0)]                                 # TP1
    path += [_bar(1.0 + 0.5 * i + 0.25, 1.0 + 0.5 * i - 0.25, 1.0 + 0.5 * i) for i in range(1, 9)]
    path += [_bar(4.9, 1.5, 1.6)]                                  # откат
    res = replay(_trade(tp2=2.0), path, "atr_trail", k=2.0)

    assert res.exits[0].reason == "tp1"
    assert res.exits[-1].reason == "stop_after_tp1"
    assert res.exits[-1].pct > 2.0, "трейл отпустил остаток ниже бывшего TP2"


def test_chandelier_takes_no_partial():
    path = [_bar(0.3, -0.1, 0.2) for _ in range(15)] + [_bar(0.2, -1.5, -1.2)]
    res = replay(_trade(), path, "chandelier", k=3.0)
    assert len(res.exits) == 1 and res.exits[0].share == 1.0


def test_kama_exit_on_a_close_behind_the_line():
    # Минимум держится выше стопа остатка на TP1 (1.0), закрытие 1.05 — ниже
    # KAMA 1.1: выходит правило KAMA, а не стоп на TP1.
    path = [_bar(1.1, 0.5, 1.0), _bar(1.3, 1.05, 1.2), _bar(1.2, 1.01, 1.05)]
    kama = [None, 1.1, 1.1]
    res = replay(_trade(), path, "kama", kama=kama)
    assert res.exits[-1].reason == "kama_close" and res.exits[-1].pct == pytest.approx(1.05)


def test_the_tp1_stop_still_guards_the_rest_under_kama():
    path = [_bar(1.1, 0.5, 1.0), _bar(1.2, 0.95, 0.96)]
    res = replay(_trade(), path, "kama", kama=[None, 1.1])
    assert res.exits[-1].reason == "stop_after_tp1" and res.exits[-1].pct == pytest.approx(1.0)


def test_time_cap_exits_on_the_last_close():
    res = replay(_trade(), [_bar(0.5, -0.5, 0.3)], "current")
    assert res.exits[-1].reason == "time_cap" and res.gross_pct() == pytest.approx(0.3)


def test_atr_does_not_look_ahead():
    path = [_bar(1.0, 0.0, 0.5)] * 14 + [_bar(10.0, -10.0, 0.0)]
    atr = atr_pct(path)
    assert atr[14] == pytest.approx(1.0), "огромная свеча попала в собственный ATR"


def test_short_path_is_measured_in_its_direction():
    t = _trade(side="short")
    path = path_pct(t, [[0, 100.0, 100.5, 99.0, 99.2]])
    assert path[0][1] == pytest.approx(1.0) and path[0][2] == pytest.approx(-0.5)


def test_costs_are_paid_once():
    res = replay(_trade(cost=0.14), [_bar(1.1, 0.2, 1.0), _bar(2.1, 1.2)], "current")
    assert net_pct(_trade(cost=0.14), res) == pytest.approx(1.5 - 0.14)


def test_a_signal_export_item_becomes_a_trade():
    item = {
        "id": 7, "symbol": "BTC/USDT", "side": "long", "status": "closed",
        "tp": {"tp1": 101.0, "tp2": 103.0}, "qty": 1.0, "net_pnl_stop": -1.14,
        "closed_total_cost": 0.14, "closed_net_pnl": 0.5, "result_pct": 0.5,
        "required_margin": 100.0, "closed_reason": "breakeven_stop",
        "plan": {"trade_mode": "trend", "lifecycle": {
            "entry_price": 100.0, "mfe_pct": 1.2,
            "first_seen_at": "2026-09-01T10:00:00+00:00",
            "closed_at": "2026-09-01T16:00:00+00:00"}},
    }
    t = trade_from_item(item)
    assert t.stop_dist_pct == pytest.approx(1.0)
    assert t.tp1_pct == pytest.approx(1.0) and t.tp2_pct == pytest.approx(3.0)
    assert t.closed_ts - t.opened_ts == 6 * 3600
    assert t.actual_pct == pytest.approx(0.5)
