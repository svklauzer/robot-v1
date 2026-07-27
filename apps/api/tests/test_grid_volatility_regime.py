"""Сетка и режим волатильности (#grid-vol-regime-2026-07-27).

Сетка усредняется против движения — это её механика и её же риск. В боковике
добор берёт осцилляцию; в направленном пробое он берёт нож: каждый следующий
уровень исполняется, цена не возвращается, корзина растёт против тренда до
стопа.

Регайм long/short этого не различает: он говорит о НАПРАВЛЕНИИ, а не о том,
остался ли возврат к среднему. Признак — расстояние цены от EMA в единицах ATR.
"""
from __future__ import annotations

from core.config import settings
from services.grid_engine import GridEngine


class _Store:
    def __init__(self):
        self.saved = None

    def put_cycle(self, symbol, cyc):
        self.saved = cyc

    def close_cycle(self, *a, **k):
        raise AssertionError("флип в этих сценариях не ожидается")


def _engine(regime="long", atr=1.0, ema=100.0):
    eng = GridEngine.__new__(GridEngine)
    eng.store = _Store()
    eng._fresh_market = lambda symbol: (regime, atr, {"ema": ema, "rsi": 55.0})
    return eng


def _cycle(**over):
    cyc = {
        "regime": "long", "levels": [], "flip_streak": 0,
        "created_at": "2026-07-27T00:00:00+00:00", "frozen": False,
    }
    cyc.update(over)
    return cyc


def test_breakout_freezes_averaging_immediately():
    """Цена в 3 ATR от EMA — добор мгновенно замораживается, без подтверждений.

    Ждать подтверждения пробоя нельзя: пока ждём, исполнится ещё уровень.
    """
    eng = _engine()
    cyc = _cycle()
    eng._adapt(cyc, "BTC/USDT", price=103.0)   # 3 ATR выше EMA 100

    assert cyc["vol_regime"] == "breakout"
    assert cyc["frozen"] is True
    assert cyc["range_streak"] == 0


def test_return_to_range_requires_confirmation():
    """Оттаиваем не сразу: иначе включим добор на первом откате внутри пробоя."""
    eng = _engine()
    cyc = _cycle(frozen=True, vol_regime="breakout")

    need = int(settings.GRID_RANGE_CONFIRM_TICKS)
    for i in range(need - 1):
        eng._adapt(cyc, "BTC/USDT", price=100.5)   # 0.5 ATR — в диапазоне
        assert cyc["frozen"] is True, f"тик {i + 1}: подтверждение ещё не набрано"
        assert cyc["vol_regime"] == "range_pending"

    eng._adapt(cyc, "BTC/USDT", price=100.5)
    assert cyc["vol_regime"] == "range"
    assert cyc["frozen"] is False


def test_middle_zone_keeps_state_that_is_the_hysteresis():
    """Между порогами состояние НЕ меняется — иначе пила у границы."""
    eng = _engine()
    cyc = _cycle(frozen=True, vol_regime="breakout")

    # 1.5 ATR: выше порога возврата (1.0), ниже порога пробоя (2.0).
    eng._adapt(cyc, "BTC/USDT", price=101.5)

    assert cyc["frozen"] is True, "в промежуточной зоне заморозка держится"
    assert cyc["vol_regime"] == "breakout"
    assert cyc["range_streak"] == 0, "счётчик возврата сброшен"


def test_thresholds_are_actually_hysteretic():
    """Порог оттаивания обязан быть ниже порога заморозки, иначе гистерезиса нет."""
    assert settings.GRID_RANGE_ATR_DIST < settings.GRID_BREAKOUT_ATR_DIST


def test_atr_normalisation_makes_the_threshold_symbol_agnostic():
    """2% от EMA — пробой для BTC и обычный день для TRX.

    Один и тот же ход в процентах даёт разный вердикт при разной ATR, и это
    правильно: сравнивается волатильность, а не проценты.
    """
    calm = _engine(atr=0.5)      # тихий рынок
    wild = _engine(atr=3.0)      # волатильный

    c1, c2 = _cycle(), _cycle()
    calm._adapt(c1, "X/USDT", price=102.0)   # 2.0 → 4 ATR
    wild._adapt(c2, "Y/USDT", price=102.0)   # 2.0 → 0.67 ATR

    assert c1["vol_regime"] == "breakout"
    assert c2["vol_regime"] != "breakout"


def test_flag_off_restores_previous_behaviour():
    old = settings.GRID_FREEZE_ON_BREAKOUT
    try:
        settings.GRID_FREEZE_ON_BREAKOUT = False
        eng = _engine()
        cyc = _cycle()
        eng._adapt(cyc, "BTC/USDT", price=105.0)
        assert "vol_regime" not in cyc
        assert cyc["frozen"] is False
    finally:
        settings.GRID_FREEZE_ON_BREAKOUT = old
