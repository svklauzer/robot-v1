"""Стенд линий трейла после TP1 (#trail-lines-2026-09-11).

Механика на синтетических путях: выход по закрытию за линией, пол фиксации на
TP1, живая схема этапа TP2, сочетания «и»/«или», подтверждение второй свечой и
отсутствие заглядывания вперёд у каждой линии.
"""
from __future__ import annotations

import pytest

from research.candle_replay import Trade
from research.trail_lines import (
    LIVE_GIVEBACK_SHARE, Rule, after_exit, anchored_vwap, kama_hourly, replay_trail,
    rolling_vwap, session_vwap,
)


def _trade(side="long", stop=1.0, tp1=1.0, tp2=2.0):
    return Trade(id=1, symbol="X/USDT", side=side, entry=100.0, stop_dist_pct=stop,
                 tp1_pct=tp1, tp2_pct=tp2, opened_ts=0, closed_ts=0, cost_pct=0.14,
                 actual_pct=0.0)


def _bar(fav, adv, close):
    return (0.0, fav, adv, close)


RIDE = Rule("x", ("x",))


def test_the_line_closes_the_whole_position_after_tp1():
    path = [_bar(1.1, 0.2, 1.05), _bar(1.8, 1.2, 1.7), _bar(1.75, 1.3, 1.4)]
    res = replay_trail(_trade(), path, RIDE, lines={"x": [None, 1.5, 1.5]})
    assert res.reason == "line" and res.exit_bar == 2
    assert res.gross_pct() == pytest.approx(1.4)


def test_a_line_below_the_lock_never_acts_first():
    """Стоп всей позиции на TP1: линия ниже него не успевает сработать."""
    path = [_bar(1.1, 0.2, 1.05), _bar(1.5, 1.2, 1.3), _bar(1.3, 0.9, 0.95)]
    res = replay_trail(_trade(), path, RIDE, lines={"x": [None, 0.5, 0.5]}, stop_slip=0.05)
    assert res.reason == "lock_stop"
    assert res.gross_pct() == pytest.approx(1.0 - 0.05)
    assert res.binding_bars == 0


def test_before_tp1_the_line_is_ignored():
    path = [_bar(0.5, -0.2, 0.1), _bar(0.4, -1.2, -1.1)]
    res = replay_trail(_trade(), path, RIDE, lines={"x": [5.0, 5.0]})
    assert res.reason == "stop" and res.gross_pct() == pytest.approx(-1.0)


def test_and_needs_both_lines_or_needs_either():
    path = [_bar(1.1, 0.2, 1.05), _bar(1.8, 1.3, 1.4)]
    lines = {"a": [None, 1.5], "b": [None, 1.2]}          # закрытие ниже a, выше b
    both = replay_trail(_trade(), path, Rule("and", ("a", "b"), "and"), lines=lines)
    either = replay_trail(_trade(), path, Rule("or", ("a", "b"), "or"), lines=lines)
    assert both.reason == "time_cap"
    assert either.reason == "line"


def test_confirmation_needs_consecutive_closes():
    path = [_bar(1.1, 0.2, 1.05), _bar(1.8, 1.3, 1.4), _bar(1.9, 1.5, 1.8), _bar(1.9, 1.3, 1.4),
            _bar(1.5, 1.2, 1.3)]
    res = replay_trail(_trade(), path, Rule("c", ("x",), confirm=2), lines={"x": [None, 1.5, 1.5, 1.5, 1.5]})
    assert res.exit_bar == 4, "одиночное закрытие за линией не должно выпускать"


def test_live_scheme_half_at_tp2_then_the_tail_gives_back_its_share():
    t = _trade(tp1=1.0, tp2=2.0)                          # буфер max(0.2, 0.5·1.0) = 0.5
    path = [_bar(1.1, 0.2, 1.05), _bar(2.1, 1.5, 2.0), _bar(3.0, 2.6, 2.9),
            _bar(2.95, 2.55, 2.55)]                        # отдача 0.45 ≥ 0.4·1.0
    res = replay_trail(t, path, Rule("live", (), shape="live"))
    assert [e.reason for e in res.exits] == ["tp2", "tail_giveback"]
    assert res.gross_pct() == pytest.approx(0.5 * 2.0 + 0.5 * 2.55)
    assert LIVE_GIVEBACK_SHARE == 0.40


def test_live_tail_ratchet_stops_below_the_peak():
    t = _trade(tp1=1.0, tp2=2.0)
    path = [_bar(1.1, 0.2, 1.05), _bar(2.1, 1.5, 2.0), _bar(4.0, 3.0, 3.9), _bar(3.9, 3.4, 3.6)]
    res = replay_trail(t, path, Rule("live", (), shape="live"))
    assert res.exits[-1].reason == "tail_stop"
    assert res.exits[-1].pct == pytest.approx(4.0 - 0.5)


def test_tp2_shape_trails_only_the_tail_by_the_line():
    t = _trade(tp1=1.0, tp2=2.0)
    path = [_bar(1.1, 0.2, 1.05), _bar(1.9, 1.2, 1.3), _bar(2.1, 1.6, 2.05), _bar(2.4, 1.9, 1.95)]
    lines = {"x": [None, 1.5, 1.5, 2.0]}                  # до TP2 закрытие 1.3 < 1.5 — не выход
    res = replay_trail(t, path, Rule("x", ("x",), shape="tp2"), lines=lines)
    assert [e.reason for e in res.exits] == ["tp2", "line"]
    assert res.gross_pct() == pytest.approx(0.5 * 2.0 + 0.5 * 1.95)


def test_avwap_from_tp1_is_anchored_at_the_tp1_bar():
    path = [_bar(0.5, 0.0, 0.3), _bar(1.2, 0.9, 1.1), _bar(1.6, 1.4, 1.5), _bar(1.5, 1.2, 1.25)]
    vol = [100.0, 1.0, 1.0, 1.0]                          # объём до TP1 не должен влиять
    res = replay_trail(_trade(), path, Rule("a", ("avwap_tp1",)), vol=vol)
    # AVWAP для свечи 3 = среднее типичных цен свечей 1 и 2 = (1.0667 + 1.5) / 2 ≈ 1.283
    assert res.reason == "line" and res.exit_bar == 3


def test_premature_exit_is_told_apart_from_a_justified_one():
    t = _trade()
    path = [_bar(1.1, 0.2, 1.05), _bar(1.8, 1.3, 1.4), _bar(2.5, 1.5, 2.4)]
    res = replay_trail(t, path, RIDE, lines={"x": [None, 1.5, 1.5]})
    assert after_exit(t, path, res) == "premature"
    path2 = [_bar(1.1, 0.2, 1.05), _bar(1.8, 1.3, 1.4), _bar(1.5, 0.9, 1.0)]
    res2 = replay_trail(t, path2, RIDE, lines={"x": [None, 1.5, 1.5]})
    assert after_exit(t, path2, res2) == "justified"


@pytest.mark.parametrize("build", [
    lambda typ, vol, ts: rolling_vwap(typ, vol, 3),
    lambda typ, vol, ts: session_vwap(typ, vol, ts),
    lambda typ, vol, ts: anchored_vwap(typ, vol, 1),
])
def test_vwap_lines_do_not_look_at_the_current_bar(build):
    typ, vol = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], [1.0] * 6
    ts = [i * 900_000 for i in range(6)]
    base = build(typ, vol, ts)
    typ2, vol2 = typ[:], vol[:]
    typ2[4], vol2[4] = 100.0, 50.0                        # меняем текущую свечу
    assert build(typ2, vol2, ts)[4] == base[4]


def test_session_vwap_resets_at_utc_midnight():
    day = 86_400_000
    ts = [day - 1_800_000, day - 900_000, day, day + 900_000]
    out = session_vwap([1.0, 3.0, 10.0, 20.0], [1.0] * 4, ts)
    assert out[1] == pytest.approx(1.0)
    assert out[2] is None, "первая свеча суток не должна видеть вчерашний VWAP"
    assert out[3] == pytest.approx(10.0)


def test_hourly_kama_uses_only_closed_hours():
    hour = 3_600_000
    candles = [[i * 900_000, 100.0, 101.0, 99.0, 100.0 + (i % 7), 1.0] for i in range(4 * 30)]
    base = kama_hourly(_trade(), candles)
    changed = [c[:] for c in candles]
    changed[-2][4] = 500.0                                # закрытие внутри последнего часа
    assert kama_hourly(_trade(), changed)[-2] == base[-2]
    # Значение внутри часа не меняется от свечи к свече — меняется на границе.
    i0 = next(i for i, c in enumerate(candles) if c[0] % hour == 0 and i > 60)
    assert base[i0] == base[i0 + 1] == base[i0 + 3]
