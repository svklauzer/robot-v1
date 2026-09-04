"""Защёлка импульса входа (#entry-impulse-2026-09-04).

Трендовый кандидат — СОСТОЯНИЕ: 4h+1h, держится сутками. Условия ТЗ — СОБЫТИЯ:
разворот ADX, кросс Stoch, один бар. Их требуют истинными одновременно, а они
последовательны — импульс случается на младшем ТФ раньше, чем тренд проступит на
старших. Замер 04.09: 68 отказов из 71 по `adx_not_rising`, медиана adx_delta
−0.589, максимум за сутки +0.28.

Защёлка запоминает событие на окно, чтобы состояние успело подтвердиться. Ни
одно условие при этом не ослабляется — снимается только требование
одновременности.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import entry_impulse_latch as latch_mod
from services.entry_impulse_latch import (
    IMPULSE_ADX_TURN, IMPULSE_STOCH_CROSS, ImpulseLatch, detect,
    substitutes_adx_rising,
)


def _tf(adx=30.0, adx_prev=29.0, k=40.0, k_prev=30.0, d=35.0, d_prev=35.0):
    return {"15m": {
        "adx14": adx, "adx14_prev": adx_prev,
        "stoch_rsi_k": k, "stoch_rsi_k_prev": k_prev,
        "stoch_rsi_d": d, "stoch_rsi_d_prev": d_prev,
    }}


# ── обнаружение события ─────────────────────────────────────────────────────

def test_adx_turning_up_is_an_impulse():
    kind, readings = detect(_tf(adx=30.0, adx_prev=29.0), "long")
    assert kind == IMPULSE_ADX_TURN
    assert readings["adx_delta"] == pytest.approx(1.0)


def test_falling_adx_alone_is_not_an_impulse():
    """Ровно тот случай, что блокирует сегодня: ADX 29.6 → 29.5."""
    kind, readings = detect(_tf(adx=29.5, adx_prev=29.6, k=40.0, k_prev=40.0,
                                d=35.0, d_prev=35.0), "long")
    assert kind is None
    assert readings["adx_delta"] == pytest.approx(-0.1)


def test_stoch_cross_is_an_impulse_and_needs_the_previous_bar():
    """Пересечение — событие одного бара. Без предыдущих значений его не
    отличить от «уже давно выше», поэтому нужны все четыре числа."""
    crossed = _tf(adx=29.5, adx_prev=29.6, k=40.0, k_prev=30.0, d=35.0, d_prev=35.0)
    assert detect(crossed, "long")[0] == IMPULSE_STOCH_CROSS

    already_above = _tf(adx=29.5, adx_prev=29.6, k=40.0, k_prev=38.0, d=35.0, d_prev=35.0)
    assert detect(already_above, "long")[0] is None


def test_cross_direction_follows_the_side():
    down = _tf(adx=29.5, adx_prev=29.6, k=30.0, k_prev=40.0, d=35.0, d_prev=35.0)
    assert detect(down, "short")[0] == IMPULSE_STOCH_CROSS
    assert detect(down, "long")[0] is None


def test_readings_come_back_even_without_an_impulse():
    """Иначе защёлка превращается в «просто не сработала», без разбираемой
    причины — ровно та беда, из-за которой гейты и становятся необъяснимыми."""
    kind, readings = detect(_tf(adx=29.5, adx_prev=29.6, k=40.0, k_prev=40.0,
                                d=35.0, d_prev=35.0), "long")
    assert kind is None
    assert readings["adx"] == 29.5 and readings["stoch_k"] == 40.0


def test_missing_context_does_not_explode():
    assert detect(None, "long") == (None, {"adx": None, "adx_delta": None,
                                           "stoch_k": None, "stoch_d": None})


# ── сама защёлка ────────────────────────────────────────────────────────────

def test_impulse_survives_until_the_state_confirms():
    """Суть правки. Импульс в t=0, состояние подтвердилось в t=900 —
    вход разрешён, хотя ADX к этому моменту уже падает."""
    latch = ImpulseLatch()
    latch.observe("SOL/USDT", "long", _tf(adx=30.0, adx_prev=29.0), now=0.0)

    assert latch.live("SOL/USDT", "long", now=900.0) is not None


def test_the_window_expires_on_its_own():
    """Защёлка не удлиняет жизнь импульса задним числом: окно отсчитывается от
    события и истекает само."""
    latch = ImpulseLatch()
    latch.observe("SOL/USDT", "long", _tf(adx=30.0, adx_prev=29.0), now=0.0)

    assert latch.live("SOL/USDT", "long", now=1801.0) is None


def test_latch_is_per_symbol_and_per_side():
    latch = ImpulseLatch()
    latch.observe("SOL/USDT", "long", _tf(adx=30.0, adx_prev=29.0), now=0.0)

    assert latch.live("SOL/USDT", "long", now=10.0) is not None
    assert latch.live("SOL/USDT", "short", now=10.0) is None
    assert latch.live("ETH/USDT", "long", now=10.0) is None


def test_a_fresh_impulse_restarts_the_window():
    latch = ImpulseLatch()
    latch.observe("SOL/USDT", "long", _tf(adx=30.0, adx_prev=29.0), now=0.0)
    latch.observe("SOL/USDT", "long", _tf(adx=32.0, adx_prev=30.0), now=1700.0)

    assert latch.live("SOL/USDT", "long", now=3000.0) is not None


def test_snapshot_carries_the_reasoning_not_just_a_flag():
    latch = ImpulseLatch()
    latch.observe("SOL/USDT", "long", _tf(adx=30.0, adx_prev=29.0), now=0.0)

    snap = latch.snapshot("SOL/USDT", "long", now=600.0)

    assert snap["live"] is True
    assert snap["impulse"]["kind"] == IMPULSE_ADX_TURN
    assert snap["impulse"]["age_sec"] == pytest.approx(600.0)
    assert snap["mode"] == "shadow"
    assert snap["window_sec"] == pytest.approx(1800.0)
    # Третье значение того же порога — рядом со снимком, а не только в коде.
    assert snap["adx_rise_min"] == pytest.approx(0.0)


# ── влияние на вход ─────────────────────────────────────────────────────────

def test_shadow_mode_changes_nothing(monkeypatch):
    """Режим по умолчанию. Считаем и пишем, вход не трогаем: в репозитории уже
    дважды отгружали правило с порогом «на глаз», и один раз замер показал вред.
    """
    monkeypatch.setattr(settings, "ENTRY_IMPULSE_LATCH_MODE", "shadow", raising=False)
    latch = ImpulseLatch()
    latch.observe("SOL/USDT", "long", _tf(adx=30.0, adx_prev=29.0), now=0.0)

    assert substitutes_adx_rising(latch.snapshot("SOL/USDT", "long", now=10.0)) is False


def test_enforce_lifts_the_block_only_with_a_live_latch(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_IMPULSE_LATCH_MODE", "enforce", raising=False)
    latch = ImpulseLatch()
    latch.observe("SOL/USDT", "long", _tf(adx=30.0, adx_prev=29.0), now=0.0)

    assert substitutes_adx_rising(latch.snapshot("SOL/USDT", "long", now=600.0)) is True
    assert substitutes_adx_rising(latch.snapshot("SOL/USDT", "long", now=5000.0)) is False


def test_latch_only_ever_substitutes_one_condition():
    """Защёлка утверждает «импульс был недавно», а не «всё прочее в порядке».
    Направление, сторона KAMA и объём обязаны остаться нетронутыми — иначе одна
    правка тихо разоружает три фильтра сразу.
    """
    import inspect

    source = inspect.getsource(latch_mod.substitutes_adx_rising)
    for family in ("di", "obv", "kama", "stoch"):
        assert f'"{family}"' not in source


def test_window_and_mode_come_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_IMPULSE_WINDOW_SEC", 60.0, raising=False)
    latch = ImpulseLatch()
    latch.observe("SOL/USDT", "long", _tf(adx=30.0, adx_prev=29.0), now=0.0)

    assert latch.live("SOL/USDT", "long", now=59.0) is not None
    assert latch.live("SOL/USDT", "long", now=61.0) is None


def test_rise_minimum_is_configurable(monkeypatch):
    """Второй порог того же вопроса уже существует (анти-чоп требует 0.5).
    Здесь он вынесен в настройку, а не прибит, чтобы третьего значения на глаз
    в кодовой базе не появилось."""
    monkeypatch.setattr(settings, "ENTRY_IMPULSE_ADX_RISE_MIN", 0.5, raising=False)

    weak = _tf(adx=29.6, adx_prev=29.4, k=40.0, k_prev=40.0, d=35.0, d_prev=35.0)
    assert detect(weak, "long")[0] is None

    strong = _tf(adx=30.0, adx_prev=29.0, k=40.0, k_prev=40.0, d=35.0, d_prev=35.0)
    assert detect(strong, "long")[0] == IMPULSE_ADX_TURN
