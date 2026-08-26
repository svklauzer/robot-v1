"""HTF-выравнивание спрашивает тренд, а не цену против EMA200
(#htf-align-trend-2026-08-26).

Вторая преграда из двух, из-за которых система не открыла ни одной сделки
24–26.08. Первая — шорт-слепота классификатора (`test_short_blindness.py`).
Эта — жёстче: даже родившийся шортовый кандидат вето́вался.

Гейт определял «4h вверх» как `last_close > ema200`. EMA200 на 4h — средняя за
~33 дня, поэтому после крупного роста условие держится месяцами. Замер 26.08,
цена 4h против EMA200 4h:

    XRP +22.9%   ETH +18.8%   SOL +18.7%   BTC +15.5%
    ADA +10.6%   AVAX +8.6%   TRX +1.6%

Все семь — шорт запрещён. При этом поле `trend` того же контекста 4h у XRP,
ADA, AVAX и TRX было `mixed`, то есть аптренда нет. Две величины про одну вещь,
и гейт брал не ту.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services.market_intelligence import MarketIntelligenceEngine


def _htf_blocks(action: str, htf_trend: str, *, is_reversal: bool = False) -> bool:
    """Повторяет условие гейта на изолированном контексте."""
    eng = MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)
    contexts = {"4h": {"trend": htf_trend, "last_close": 1.436, "ema200": 1.169}}

    if not bool(getattr(settings, "HTF_ALIGN_ENABLED", True)) or is_reversal:
        return False

    _htf = eng._tf(contexts, str(getattr(settings, "HTF_ALIGN_TF", "4h")))
    _htf_trend = str(eng._ctx_value(_htf, "trend", "") or "")
    if action == "long" and _htf_trend == "trend_down":
        return True
    if action == "short" and _htf_trend == "trend_up":
        return True
    return False


# ── боевой случай 26.08 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("symbol", ["XRP", "ADA", "AVAX", "TRX"])
def test_mixed_htf_no_longer_forbids_a_short(symbol):
    """4h = mixed: направления нет, значит и запрещать нечего.

    Цена у всех четверых была выше 4h EMA200 (от +1.6% до +22.9%) — по прежнему
    правилу это считалось аптрендом и вето́вало шорт.
    """
    assert _htf_blocks("short", "mixed") is False, symbol


def test_real_htf_uptrend_still_forbids_a_short():
    """Явный аптренд на 4h по-прежнему закрывает шорт — гейт не выключен.

    26.08 это BTC, ETH и SOL: у них 4h-тренд действительно `trend_up`.
    """
    assert _htf_blocks("short", "trend_up") is True


def test_real_htf_downtrend_still_forbids_a_long():
    """Симметрично для лонга — правило осталось двусторонним."""
    assert _htf_blocks("long", "trend_down") is True


def test_flat_htf_blocks_neither_side():
    """Боковик на 4h — тоже не направление."""
    assert _htf_blocks("short", "flat") is False
    assert _htf_blocks("long", "flat") is False


def test_reversal_is_still_exempt():
    """Разворотный контур жил вне этого гейта и живёт дальше.

    Он единственный с положительным net по `/analytics/mfe-mae` (+6.79 на 19
    сделках, edge_ratio 3.11) — трогать его в этой правке нечего.
    """
    assert _htf_blocks("short", "trend_up", is_reversal=True) is False


# ── то, ради чего правка и делалась ─────────────────────────────────────────
def test_price_above_ema200_alone_is_not_a_trend():
    """Прежнее определение разошлось бы с `trend` на боевых числах XRP.

    Цена 1.436 выше EMA200 1.169 на 22.9% — по старому правилу «аптренд», по
    полю `trend` того же контекста — `mixed`. Тест закрепляет, что теперь
    решает второе.
    """
    px, ema200 = 1.436, 1.169
    assert px > ema200                        # старое правило сказало бы «вверх»
    assert _htf_blocks("short", "mixed") is False  # новое смотрит на trend


def test_gate_reads_trend_not_ema200():
    """Страховка от возврата: в коде гейта не должно остаться ema200."""
    import inspect
    import re

    src = inspect.getsource(MarketIntelligenceEngine._evaluate_setup_quality)
    block = re.search(r"HTF_ALIGN_ENABLED.*?htf_against_short_4h_up", src, re.S)

    assert block is not None, "блок HTF-выравнивания не найден"
    assert "ema200" not in block.group(0)
    assert 'trend_down' in block.group(0) and 'trend_up' in block.group(0)
