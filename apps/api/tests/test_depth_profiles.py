"""Профили реакции на стакан по движкам (#depth-profiles-2026-07-27).

Запрос Капитана: «движки все разные — SCALP, TREND, CRT, GRID. Уровни реакции
должны быть разные».

Из 7 параметров depth-гейта по профилю был разделён ровно один — спред.
Скальпер на 1m и трендовый вход с 4h-биасом реагировали на стакан одинаково,
хотя информативность мгновенного потока падает с горизонтом сделки.
"""
import pytest

from services.depth_profiles import depth_gate_params, profile_for
from services.orderbook_analyzer import DepthSignal, OrderBookAnalyzer

PARAMS = ("max_spread_pct", "obi_confirm", "wall_confirm", "cvd_block_ratio",
          "cvd_min_trades", "obi_hard_veto", "wall_rescue_max_adverse_obi",
          "cvd_thin_ratio", "cvd_thin_min_trades")


def test_profile_mapping_matches_holding_horizon():
    for regime in ("scalp", "range"):
        assert profile_for(regime) == "scalp"
    for regime in ("trend_up_candidate", "trend_down_candidate", "crt",
                   "reversal_long_candidate"):
        assert profile_for(regime) == "position"


def test_position_profile_is_coarser_on_instant_flow():
    """Длинный горизонт обязан быть терпимее к секундному состоянию стакана."""
    s = depth_gate_params("scalp")
    p = depth_gate_params("trend_down_candidate")

    assert p["obi_hard_veto"] > s["obi_hard_veto"], "вето по OBI не огрублено"
    assert p["obi_confirm"] < s["obi_confirm"]
    assert p["cvd_block_ratio"] > s["cvd_block_ratio"], "CVD-порог не огрублён"
    assert p["cvd_thin_min_trades"] > s["cvd_thin_min_trades"], (
        "одна сделка не может быть выборкой для часового горизонта"
    )
    assert p["max_spread_pct"] >= s["max_spread_pct"]


def test_every_gate_parameter_is_profiled():
    """Регресс на исходный дефект: разделён должен быть КАЖДЫЙ параметр,
    а не только спред — иначе движки снова разъедутся."""
    s = depth_gate_params("scalp")
    p = depth_gate_params("trend_up_candidate")

    for key in PARAMS:
        assert key in s and key in p, f"параметр {key} выпал из профиля"

    differing = [k for k in PARAMS if s[k] != p[k]]
    assert len(differing) >= 6, (
        f"по профилю разделено лишь {len(differing)} параметров: {differing}"
    )


def test_extreme_skew_is_still_blocked_for_position():
    """Огрубление не должно превращаться в отключение защиты."""
    dp = depth_gate_params("trend_down_candidate")
    dp.pop("profile")
    sig = DepthSignal(fresh=True, spread_pct=0.013, mid=0.165, obi=0.871,
                      bid_wall_share=0.57, ask_wall_share=0.21,
                      cvd=0.0, cvd_ratio=-0.47, cvd_trades=26)

    ok, reason = OrderBookAnalyzer.entry_gate("short", sig, **dp)

    assert ok is False
    assert "obi_against_short" in reason


def test_moderate_skew_no_longer_blocks_a_trend_setup():
    """Боевой кейс ADA 27.07: setup_score 99.7, obi 0.514 — сетап с 4h-биасом
    резался секундным перекосом стакана."""
    sig = DepthSignal(fresh=True, spread_pct=0.017, mid=0.165, obi=0.5145,
                      bid_wall_share=0.568, ask_wall_share=0.608,
                      cvd=0.0, cvd_ratio=0.5531, cvd_trades=8)

    scalp = depth_gate_params("scalp"); scalp.pop("profile")
    pos = depth_gate_params("trend_down_candidate"); pos.pop("profile")

    assert OrderBookAnalyzer.entry_gate("short", sig, **scalp)[0] is False
    assert OrderBookAnalyzer.entry_gate("short", sig, **pos)[0] is True


def test_scalp_thresholds_are_unchanged():
    """Скальп — родная среда стакана, его чувствительность трогать нельзя."""
    from core.config import settings

    s = depth_gate_params("scalp")
    assert s["obi_hard_veto"] == float(settings.OB_OBI_HARD_VETO)
    assert s["cvd_block_ratio"] == float(settings.OB_CVD_ENTRY_BLOCK_RATIO)
    assert s["cvd_thin_min_trades"] == int(settings.OB_CVD_THIN_MIN_TRADES)


def test_both_entry_paths_use_the_shared_profile():
    """Оба пути входа обязаны брать пороги из одной точки, иначе пред-проход и
    публикация разойдутся в оценке одного сетапа (так уже было со спредом)."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "workers" / "robot_loop.py"
    text = src.read_text(encoding="utf-8")

    assert text.count("depth_gate_params(") >= 2, (
        "один из путей входа всё ещё читает пороги напрямую из settings"
    )
