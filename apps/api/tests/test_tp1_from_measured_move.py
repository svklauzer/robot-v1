"""TP1 ставится по ИЗМЕРЕННОМУ ходу (#tp1-from-measured-move-2026-08-22).

Замер, из-за которого правка появилась. 21–22.08 в ленте решений десятки
блокировок `tp1_beyond_typical_move` по XRP и ADA:

    XRP  tp1_dist 1.197–1.206%  median_mfe 0.541%  ratio 2.21–2.23
    ADA  tp1_dist 1.188–1.531%  median_mfe 0.487%  ratio 2.44–3.14

при потолке `TP_REACH_MAX_RATIO=1.5`. Дистанция XRP держалась ровно на 1.2% —
это `TP1_DEFAULT_PCT`, то есть структуры в коридоре не находилось НИКОГДА и
«запасной» вариант работал как основной. В аптренде так и должно быть:
`resistance` — максимум последних баров, он остаётся позади растущей цены.

Итог: система ставила цель, которую сама же признавала недостижимой, и резала
собственный вход. Для сравнения TRX #428, где структура в коридоре нашлась
(TP1 +0.68% от last), гейт прошёл и сделка дошла до TP2: +3.22 USDT.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services.market_intelligence import MarketIntelligenceEngine


def _engine(symbol: str = "XRP/USDT"):
    eng = MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)
    eng._cur_symbol = symbol
    return eng


def test_fallback_uses_measured_move_not_the_constant(monkeypatch):
    """Медианный ход 0.54% → цель около него, а не 1.2%."""
    import services.tp_reachability as tpr

    monkeypatch.setattr(tpr, "typical_move_pct", lambda *_a, **_kw: 0.54)
    monkeypatch.setattr(settings, "TP1_TYPICAL_MOVE_MULT", 1.0, raising=False)

    pct = _engine()._fallback_tp1_pct("trend_up_candidate")
    assert pct == pytest.approx(0.54, abs=0.01)


def test_fallback_keeps_constant_when_measurement_is_missing(monkeypatch):
    """Нет замера — остаётся прежняя константа. Fail-open, не ноль."""
    import services.tp_reachability as tpr

    monkeypatch.setattr(tpr, "typical_move_pct", lambda *_a, **_kw: None)
    pct = _engine()._fallback_tp1_pct("trend_up_candidate")
    assert pct == pytest.approx(float(settings.TP1_DEFAULT_PCT))


def test_tp1_stays_inside_the_corridor(monkeypatch):
    """Пол и потолок коридора никуда не делись.

    Медиана 0.2% не должна ставить цель ниже TP1_MIN_PCT: там её съедят
    издержки. Медиана 5% — не выше TP1_MAX_PCT.
    """
    import services.tp_reachability as tpr

    eng = _engine()
    last = 100.0

    monkeypatch.setattr(tpr, "typical_move_pct", lambda *_a, **_kw: 0.2)
    tp1_low = eng._reachable_tp1("long", last, None, None)
    assert tp1_low >= last * (1 + float(settings.TP1_MIN_PCT) / 100.0) * 0.999

    monkeypatch.setattr(tpr, "typical_move_pct", lambda *_a, **_kw: 5.0)
    tp1_high = eng._reachable_tp1("long", last, None, None)
    assert tp1_high <= last * (1 + float(settings.TP1_MAX_PCT) / 100.0) * 1.001


def test_structural_level_still_wins_over_fallback(monkeypatch):
    """Реальная структура в коридоре важнее замера — как в TRX #428.

    Замер отвечает на вопрос «сколько обычно проходит», структура — «где
    встречная ликвидность». Второе конкретнее, и если оно есть, берём его.
    """
    import services.tp_reachability as tpr

    monkeypatch.setattr(tpr, "typical_move_pct", lambda *_a, **_kw: 0.54)
    eng = _engine("TRX/USDT")
    last = 100.0
    # сопротивление на +1.0% — внутри коридора [0.6; 1.8]
    m15 = {"resistance": 101.0}

    tp1 = eng._reachable_tp1("long", last, None, m15)

    assert tp1 == pytest.approx(101.0 * (1 - 0.0005), abs=0.01)


def test_measured_fallback_passes_the_reachability_gate(monkeypatch):
    """Главное: цель, поставленная по замеру, гейт больше не режет.

    Именно это и ломалось — placement и проверка были двумя независимыми
    числами про один вопрос и спорили между собой.
    """
    import services.tp_reachability as tpr

    median = 0.541  # XRP из боевых данных
    monkeypatch.setattr(tpr, "typical_move_pct", lambda *_a, **_kw: median)

    pct = _engine()._fallback_tp1_pct("trend_up_candidate")
    pct = max(float(settings.TP1_MIN_PCT), min(pct, float(settings.TP1_MAX_PCT)))

    ratio = pct / median
    assert ratio <= float(settings.TP_REACH_MAX_RATIO)
