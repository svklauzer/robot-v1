"""HTF-вето для сетки (#grid-htf-veto-2026-07-27).

Замер 12 циклов 25–26.07 разделяет исходы по RSI без единого исключения:

    убытки  — RSI 76.2 / 83.1 / 87.0 → −2.20 суммарно (все grid_stop_loss)
    прибыли — RSI 50.6 / 66.0 / 69.9 → +0.15 суммарно

Механика: «нейтральная» сетка в одностороннем ходе набирает только контр-сторону
(все sell-уровни filled), цена уходит дальше — корзина ловит стоп. Это не чоп,
для которого сетка предназначена.

Тот же дефект уже лечили HTF-вето у скальпа и у трендового движка; сетка была
третьим движком с той же болезнью.
"""
import pytest

from core.config import settings


# Реальные циклы из /grid/state за 25–26.07: (RSI на закрытии, realized_pnl)
REAL_CYCLES = [
    (58.42, 0.0), (55.31, 0.0), (58.17, 0.0), (56.50, 0.0), (54.42, 0.0),
    (52.00, 0.0), (69.94, +0.0296), (50.60, +0.0524), (66.02, +0.0627),
    (83.07, -0.8913), (76.24, -0.3727), (86.98, -0.9315),
]


def _vetoed(rsi: float) -> bool:
    hi = float(settings.GRID_HTF_RSI_OVERHEAT)
    lo = float(settings.GRID_HTF_RSI_OVERSOLD)
    return rsi >= hi or rsi <= lo


def test_veto_would_have_cut_every_losing_cycle():
    losers = [(rsi, pnl) for rsi, pnl in REAL_CYCLES if pnl < 0]
    assert losers, "выборка убыточных циклов пуста — тест потерял смысл"

    not_cut = [rsi for rsi, _ in losers if not _vetoed(rsi)]
    assert not not_cut, f"вето пропускает убыточные циклы при RSI {not_cut}"


def test_veto_keeps_every_profitable_cycle():
    winners = [(rsi, pnl) for rsi, pnl in REAL_CYCLES if pnl > 0]
    assert winners

    cut = [rsi for rsi, _ in winners if _vetoed(rsi)]
    assert not cut, f"вето режет прибыльные циклы при RSI {cut} — порог слишком узкий"


def test_measured_effect_on_the_real_sample():
    saved = sum(pnl for rsi, pnl in REAL_CYCLES if pnl < 0 and _vetoed(rsi))
    lost = sum(pnl for rsi, pnl in REAL_CYCLES if pnl > 0 and _vetoed(rsi))

    assert saved <= -2.0, "вето должно снимать основной убыток выборки"
    assert lost == 0.0, "вето не должно стоить ни одной прибыльной корзины"


def test_threshold_is_aligned_with_the_other_engines():
    """Пороги сетки не должны расходиться с трендовым вето — это один принцип."""
    assert settings.GRID_HTF_RSI_OVERHEAT == settings.TREND_HTF_RSI_HARD_OVERHEAT
    assert settings.GRID_HTF_RSI_OVERSOLD == settings.TREND_HTF_RSI_HARD_OVERSOLD


def test_veto_can_be_disabled():
    old = settings.GRID_HTF_EXTREME_VETO
    try:
        settings.GRID_HTF_EXTREME_VETO = False
        assert settings.GRID_HTF_EXTREME_VETO is False
    finally:
        settings.GRID_HTF_EXTREME_VETO = old


def test_engine_actually_checks_the_veto():
    """Регресс: гейт должен стоять в пути открытия цикла, а не только в конфиге."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "services" / "grid_engine.py"
    text = src.read_text(encoding="utf-8")

    assert "GRID_HTF_EXTREME_VETO" in text, "вето не подключено к движку сетки"
    assert "GRID_HTF_RSI_OVERHEAT" in text
    # гейт обязан стоять внутри _maybe_open — единственного пути открытия
    open_block = text[text.index("def _maybe_open"):]
    assert "GRID_HTF_EXTREME_VETO" in open_block, "вето вне пути открытия цикла"
