"""Качество входа: RSI-зоны, зона входа по стакану, режим волатильности сетки
(#rsi-dynamic-2026-07-27, #entry-zone-2026-07-27, #grid-vol-regime-2026-07-27).

Общая мысль всех трёх правок: бинарный ответ «входить / не входить» теряет
информацию. Между «сетап отличный» и «сетап негодный» лежит широкая полоса,
в которой правильная реакция — уменьшить размер или перенести цену, а не
отказаться.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import entry_zone, rsi_gate


# ── RSI: три зоны вместо двух ────────────────────────────────────────────────

def test_weak_trend_keeps_the_old_hard_threshold():
    """Без признаков силы порог не поднимается — прежнее поведение сохранено."""
    d = rsi_gate.evaluate(side="long", rsi=73.0, ema20=100.0, ema50=100.0,
                          atr=1.0, volume_ratio=1.0, htf_aligned=False)

    assert d.trend_strength == pytest.approx(0.0)
    assert d.dynamic_threshold == pytest.approx(72.0)
    assert d.zone == "late_entry", "73 выше порога 72, но ниже жёсткого блока 82"
    assert d.allowed and d.risk_multiplier < 1.0


def test_strong_trend_is_allowed_to_stay_overbought():
    """Разгон на объёме с согласием ТФ поднимает порог — это и было целью.

    Сильные тренды держатся выше 70 неделями; плоский порог резал именно ту
    часть движения, ради которой вход и делается.
    """
    d = rsi_gate.evaluate(side="long", rsi=76.0, ema20=102.0, ema50=100.0,
                          atr=1.0, volume_ratio=1.8, htf_aligned=True)

    assert d.trend_strength == pytest.approx(1.0)
    assert d.dynamic_threshold == pytest.approx(80.0)
    assert d.zone == "clear", "в сильном тренде RSI 76 больше не повод отказывать"
    assert d.risk_multiplier == 1.0


def test_extreme_rsi_is_blocked_even_in_the_strongest_trend():
    """Жёсткий блок остаётся: сила тренда не отменяет предел."""
    d = rsi_gate.evaluate(side="long", rsi=91.0, ema20=102.0, ema50=100.0,
                          atr=1.0, volume_ratio=2.0, htf_aligned=True)

    assert d.zone == "hard_block"
    assert not d.allowed and d.risk_multiplier == 0.0


def test_late_entry_reduces_size_instead_of_refusing():
    """Главное изменение: между порогом и блоком вход РАЗРЕШЁН меньшим размером."""
    d = rsi_gate.evaluate(side="long", rsi=84.0, ema20=102.0, ema50=100.0,
                          atr=1.0, volume_ratio=1.8, htf_aligned=True)

    assert d.zone == "late_entry"
    assert d.allowed
    assert 0.0 < d.risk_multiplier < 1.0
    assert d.risk_multiplier == pytest.approx(settings.RSI_LATE_ENTRY_RISK_MULTIPLIER)


def test_short_side_is_mirrored():
    weak = rsi_gate.evaluate(side="short", rsi=27.0, ema20=100.0, ema50=100.0,
                             atr=1.0, volume_ratio=1.0, htf_aligned=False)
    strong = rsi_gate.evaluate(side="short", rsi=24.0, ema20=98.0, ema50=100.0,
                               atr=1.0, volume_ratio=1.8, htf_aligned=True)

    assert weak.zone == "late_entry"
    assert strong.dynamic_threshold == pytest.approx(20.0)
    assert strong.zone == "clear", "падающий на объёме рынок имеет право быть перепродан"


def test_disabled_flag_restores_flat_binary_behaviour():
    old = settings.RSI_DYNAMIC_ENABLED
    try:
        settings.RSI_DYNAMIC_ENABLED = False
        d = rsi_gate.evaluate(side="long", rsi=73.0, ema20=102.0, ema50=100.0,
                              atr=1.0, volume_ratio=2.0, htf_aligned=True)
        assert d.zone == "hard_block" and not d.allowed
    finally:
        settings.RSI_DYNAMIC_ENABLED = old


# ── Зона входа по стакану ────────────────────────────────────────────────────

def _book(spread_mult: float = 1.0002, near_heavy: bool = True):
    depth = [50, 40, 30, 20, 10] if near_heavy else [1, 1, 1, 200, 300]
    bids = [[100.0 - i * 0.1, depth[i]] for i in range(5)]
    asks = [[100.0 * spread_mult + i * 0.1, depth[i]] for i in range(5)]
    return {"bids": bids, "asks": asks, "trades": [], "ts": 10 ** 12}


def test_healthy_book_takes_market():
    d = entry_zone.evaluate(side="long", last_price=100.0, snapshot=_book())
    assert d.mode == "market" and d.allowed


def test_wide_spread_moves_entry_to_support_instead_of_cancelling():
    """Перенос сохраняет сетап; отмена выбросила бы его целиком."""
    d = entry_zone.evaluate(side="long", last_price=100.45,
                            snapshot=_book(spread_mult=1.009))

    assert d.mode.startswith("limit_")
    assert d.allowed
    assert d.entry_price is not None and d.entry_price < 100.45
    assert d.drift_pct > 0, "перенос обязан быть В НАШУ пользу"
    assert any("спред" in r for r in d.reasons)


def test_support_too_far_cancels_the_setup():
    """Единственный повод отменить: опора дальше потолка переноса."""
    old = settings.ENTRY_ZONE_MAX_DRIFT_PCT
    try:
        settings.ENTRY_ZONE_MAX_DRIFT_PCT = 0.10
        d = entry_zone.evaluate(side="long", last_price=100.45,
                                snapshot=_book(spread_mult=1.009))
        assert d.mode == "reject" and not d.allowed
    finally:
        settings.ENTRY_ZONE_MAX_DRIFT_PCT = old


def test_stale_book_fails_open_to_market():
    """Фид молчит — движок не встаёт. Осознанный fail-open, факт фиксируется."""
    d = entry_zone.evaluate(side="long", last_price=100.0, snapshot=None)
    assert d.mode == "market" and d.allowed


def test_limit_entry_expires_but_market_never_does():
    limit = entry_zone.evaluate(side="long", last_price=100.45,
                                snapshot=_book(spread_mult=1.009))
    market = entry_zone.evaluate(side="long", last_price=100.0, snapshot=_book())

    assert entry_zone.is_stale(limit, settings.ENTRY_ZONE_TTL_SEC + 1)
    assert not entry_zone.is_stale(limit, 1.0)
    assert not entry_zone.is_stale(market, 10_000), "рыночный вход берёт то, что есть"


def test_micro_vwap_uses_our_own_side_of_the_book():
    """Для лонга опора — биды: те, кто уже стоит в покупке ниже рынка."""
    book = _book()
    vwap_long = entry_zone.micro_vwap("long", book)
    vwap_short = entry_zone.micro_vwap("short", book)

    assert vwap_long < 100.0 < vwap_short
