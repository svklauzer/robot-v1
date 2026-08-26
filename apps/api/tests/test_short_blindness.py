"""Шорт-слепота классификатора режима (#short-blindness-2026-08-26).

Двое суток 24–26.08 система не открыла ни одной сделки. Гейты входа были ни при
чём: ВСЕ события — `tz_entry_conditions` с `blocked_by:di,kama` по BTC и ETH,
то есть KAMA/DI законно отказывались покупать падающий рынок. Проблема была
раньше по цепочке — кандидат мог родиться только ДЛИННЫМ.

Причина: `total` из `_score_context` — величина НАПРАВЛЕННАЯ (тренд 75 вверх /
25 вниз, импульс 70 / 30; шкала симметрична вокруг 50), а порог `total >= 58`
применялся к обеим сторонам «для симметрии». Одинаково сильный даунтренд даёт
total на 23 пункта ниже, поэтому шорт через ветку голосования был недостижим
в принципе.

Тот же класс ошибки, что и в `tp_reachability`: одно число, два смысла. Рядом,
в `_detect_regime`, это же число читается направленно и правильно: ≥62 вверх,
≤42 вниз.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services.market_intelligence import MarketIntelligenceEngine


class _Ctx(dict):
    """Контекст ТФ: движок читает и словари, и объекты."""


def _ctx(trend: str, momentum: str = "neutral") -> _Ctx:
    return _Ctx(trend=trend, momentum=momentum, volatility="normal")


def _engine():
    return MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)


def _regime(contexts, scores, monkeypatch, learning=True):
    eng = _engine()
    monkeypatch.setattr(eng, "_learning_mode", lambda: learning, raising=False)
    return eng._detect_multi_timeframe_regime(contexts, scores)


# ── арифметика перекоса ─────────────────────────────────────────────────────
def test_identical_strength_scores_23_points_apart():
    """Доказательство перекоса из самих весов, без данных.

    Одинаково сильный тренд вверх и вниз при прочих равных расходится на 23
    пункта: 61.5 против 38.5. Порог 58 для обеих сторон — это запрет шорта.
    """
    def total(trend, momentum):
        return trend * 0.30 + momentum * 0.20 + 50 * 0.20 + 50 * 0.20 + 50 * 0.10

    up = total(75, 70)     # идеальный аптренд
    down = total(25, 30)   # идеальный даунтренд

    assert up == pytest.approx(61.5)
    assert down == pytest.approx(38.5)
    assert up - down == pytest.approx(23.0)
    assert down < float(getattr(settings, "SETUP_VOTING_MIN_TOTAL_SCORE", 58.0))


# ── боевые числа 26.08 ──────────────────────────────────────────────────────
_XRP_SCORES = {"trend": 31.6, "momentum": 43.2, "volume": 41.2,
               "structure": 64.4, "volatility": 63.2, "total": 45.56}
_ADA_SCORES = {"trend": 31.6, "momentum": 47.2, "volume": 49.0,
               "structure": 57.6, "volatility": 63.2, "total": 46.56}

# 1m flat, 5m/15m/1h вниз, 4h ещё mixed после роста — три голоса вниз.
_FALLING = {
    "1m": _ctx("flat"),
    "5m": _ctx("trend_down", "bullish"),
    "15m": _ctx("trend_down", "neutral"),
    "1h": _ctx("trend_down", "bearish"),
    "4h": _ctx("mixed", "bearish"),
}


@pytest.mark.parametrize("scores,name", [(_XRP_SCORES, "XRP"), (_ADA_SCORES, "ADA")])
def test_falling_market_now_produces_a_short_candidate(scores, name, monkeypatch):
    """XRP и ADA 26.08: 1h ADX 33 и 43, DI −19 и −20, цена под всеми EMA.

    Учебный шорт, который классификатор называл `mixed` и пропускал.
    """
    assert _regime(_FALLING, scores, monkeypatch) == "trend_down_candidate", name


def test_long_side_is_untouched(monkeypatch):
    """Длинная сторона не должна измениться ни на пункт.

    BTC 24.08, прошедший тогда голосование: trend 75, total 59.28. Правка
    трогает только шорт — иначе мы поменяли бы выборку входов с обеих сторон
    и снова не поняли бы, что подействовало.
    """
    rising = {
        "1m": _ctx("flat"),
        "5m": _ctx("trend_up", "bullish"),
        "15m": _ctx("trend_up", "bullish"),
        "1h": _ctx("trend_up", "neutral"),
        "4h": _ctx("trend_up", "neutral"),
    }
    scores = {"trend": 75.0, "momentum": 54.4, "volume": 49.3,
              "structure": 48.0, "volatility": 64.4, "total": 59.28}

    assert _regime(rising, scores, monkeypatch) == "trend_up_candidate"


def test_weak_chop_still_gets_no_short(monkeypatch):
    """Симметрия не должна превращаться в раздачу шортов на любом сползании.

    Слабое направление и посредственное качество: зеркальный total не дотянет
    до того же порога 58, что и для лонга.
    """
    scores = {"trend": 45.0, "momentum": 48.0, "volume": 40.0,
              "structure": 42.0, "volatility": 45.0, "total": 43.9}

    assert _regime(_FALLING, scores, monkeypatch) != "trend_down_candidate"


def test_no_separate_knob_was_introduced_for_shorts():
    """Ни одной новой константы: у шорта тот же порог, что у лонга.

    Отдельная ручка под шорт означала бы возможность подкрутить одну сторону
    под данные — ровно тот приём, из-за которого мы уже теряли гипотезы.
    """
    import inspect

    src = inspect.getsource(
        MarketIntelligenceEngine._detect_multi_timeframe_regime)

    # Обе стороны сверяются с одними и теми же двумя настройками.
    assert src.count("_vote_min") == 3      # объявление + лонг + шорт
    assert src.count("_min_total") == 3
    assert "short_total_score >= _vote_min" in src
    assert "short_total_score >= _min_total" in src
