"""TP1 НЕ ставится знаменателем гейта (#tp1-from-measured-move-2026-08-22,
отменено 24.08.2026).

Отрицательный результат, записанный с числами, а не удалённый.

Правка 22.08 появилась по реальной беде: 21–22.08 в ленте решений десятки
блокировок `tp1_beyond_typical_move` по XRP и ADA:

    XRP  tp1_dist 1.197–1.206%  median_mfe 0.541%  ratio 2.21–2.23
    ADA  tp1_dist 1.188–1.531%  median_mfe 0.487%  ratio 2.44–3.14

при потолке `TP_REACH_MAX_RATIO=1.5`. Дистанция XRP держалась ровно на 1.2% —
это `TP1_DEFAULT_PCT`, то есть структуры в коридоре не находилось НИКОГДА и
«запасной» вариант работал как основной.

Лечение выбрали неверное: стали ставить TP1 по той же медиане MFE, которой гейт
эту цель проверял, и назвали это «одно число вместо двух спорящих». Выводить
одно из другого правильно для тождеств учёта (маржа, конверты) — но не для пары
«цель ↔ её валидация». Проверка, выведенная из проверяемого, ничего не
проверяет: отношение становилось 1.0 по построению.

И это не спасало даже арифметически. Пол коридора `TP1_MIN_PCT=0.6` вместе с
потолком 1.5 делал гейт проходимым только при медиане ≥ 0.4%. XRP с медианой
0.541% проскакивал, а ETH с 0.3154% (при ATR 1h 0.95%!) блокировался при ЛЮБОЙ
достижимой цели — 24.08 каждый скан. Выборка ETH при этом расти не могла: она
растёт только от закрытых сделок.

Сейчас: запасная цель — константа, гейт считает ЧАСТОТУ достижения TP2 против
порога из заявленного RR (см. `test_tp_reachability.py`).
"""
from __future__ import annotations

import pytest

from core.config import settings
from services.market_intelligence import MarketIntelligenceEngine


def _engine(symbol: str = "XRP/USDT"):
    eng = MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)
    eng._cur_symbol = symbol
    return eng


def test_fallback_is_a_constant_again():
    """Запасная цель не зависит от замера, которым её же проверяют."""
    assert _engine()._fallback_tp1_pct("trend_up_candidate") == pytest.approx(
        float(settings.TP1_DEFAULT_PCT))


def test_fallback_ignores_the_measurement_entirely(monkeypatch):
    """Даже если замер доступен, цель его не спрашивает.

    Страховка от возврата связки: подмена `typical_move_pct` не должна ни на
    что влиять — функции больше нет, и цель на неё не смотрит.
    """
    import services.tp_reachability as tpr

    monkeypatch.setattr(tpr, "typical_move_pct", lambda *_a, **_kw: 0.54,
                        raising=False)
    assert _engine()._fallback_tp1_pct("trend_up_candidate") == pytest.approx(
        float(settings.TP1_DEFAULT_PCT))


def test_measured_move_helper_is_gone():
    """`typical_move_pct` удалена: ею ставилась цель по знаменателю проверки."""
    import services.tp_reachability as tpr

    assert not hasattr(tpr, "typical_move_pct")


def test_ratio_gate_constant_is_gone():
    """`TP_REACH_MAX_RATIO` не возвращать — он и создавал безусловный замок."""
    assert not hasattr(settings, "TP_REACH_MAX_RATIO")


def test_the_old_pairing_was_a_deadlock_below_this_median():
    """Арифметика замка, ради памяти: 0.6 / 1.5 = 0.4%.

    Ближе `TP1_MIN_PCT` цель поставить нельзя, а гейт требовал
    `tp1_dist <= 1.5 * median`. Значит любая пара (символ, режим) с медианой
    ниже 0.4% была заблокирована безусловно. XRP 0.541% жил, ETH 0.3154% — нет.
    """
    floor_pct = float(settings.TP1_MIN_PCT)
    old_max_ratio = 1.5  # снятая константа, воспроизведена здесь намеренно
    unsatisfiable_below = floor_pct / old_max_ratio

    assert unsatisfiable_below == pytest.approx(0.4)
    assert 0.3154 < unsatisfiable_below   # ETH — вечный отказ
    assert 0.541 > unsatisfiable_below    # XRP — проскакивал


def test_tp1_stays_inside_the_corridor(monkeypatch):
    """Пол и потолок коридора никуда не делись."""
    eng = _engine()
    last = 100.0

    monkeypatch.setattr(settings, "TP1_DEFAULT_PCT", 0.2, raising=False)
    tp1_low = eng._reachable_tp1("long", last, None, None)
    assert tp1_low >= last * (1 + float(settings.TP1_MIN_PCT) / 100.0) * 0.999

    monkeypatch.setattr(settings, "TP1_DEFAULT_PCT", 5.0, raising=False)
    tp1_high = eng._reachable_tp1("long", last, None, None)
    assert tp1_high <= last * (1 + float(settings.TP1_MAX_PCT) / 100.0) * 1.001


def test_structural_level_still_wins_over_fallback():
    """Реальная структура в коридоре важнее константы — как в TRX #428.

    Там структура нашлась (TP1 +0.68% от last), гейт прошёл, сделка дошла до
    TP2: +3.22 USDT в остатке плюс 0.53 на частичной фиксации.
    """
    eng = _engine("TRX/USDT")
    last = 100.0
    m15 = {"resistance": 101.0}   # +1.0% — внутри коридора [0.6; 1.8]

    tp1 = eng._reachable_tp1("long", last, None, m15)

    assert tp1 == pytest.approx(101.0 * (1 - 0.0005), abs=0.01)
