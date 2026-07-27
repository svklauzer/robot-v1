"""Профили реакции на стакан по движкам (#depth-profiles-2026-07-27).

Запрос Капитана: «движки все разные — SCALP, TREND, CRT, GRID. Уровни реакции
должны быть разные».

Проблема, которую это закрывает. Depth-гейт имеет 7 параметров, а по профилю
был разделён ровно ОДИН — максимальный спред. OBI, стенка и CVD были общими:
скальпер на 1m и трендовый вход с 4h-биасом реагировали на стакан одинаково.

Почему это неверно по существу: информативность мгновенного потока падает с
ростом горизонта сделки.

    SCALP    живёт 5–45 минут  → стакан ЭТО и есть его сигнал, реакция острая
    RANGE    живёт до 90 минут → стакан важен, но чуть грубее
    TREND/CRT держатся часами  → мгновенный перекос это шум, реакция грубая

Цена ошибки видна в телеметрии 27.07: ADA trend_down со setup_score 88–100
отклонялся ВОСЕМЬ раз за полтора часа по `obi_against_short: obi=0.87>=0.45` —
то есть сетап с 4-часовым биасом резался состоянием стакана в конкретную
секунду. Для позиции, которую держат 5–12 часов, это не информация.

Границы профилей намеренно НЕ выключают защиту: для position-профиля
сохраняются вето на экстремальный перекос и на полностью встречный поток —
убирается только чувствительность к рядовым колебаниям.
"""
from __future__ import annotations

from core.config import settings

# Профили ведения, к которым привязана реакция на стакан.
SCALP_REGIMES = ("scalp", "range")


def profile_for(regime: str | None, trade_mode: str | None = None) -> str:
    """`scalp` — короткий горизонт (micro-scalp, range), `position` — длинный
    (trend_up/trend_down/crt/reversal)."""
    r = str(regime or "").lower()
    m = str(trade_mode or "").lower()
    if r in SCALP_REGIMES or m in SCALP_REGIMES:
        return "scalp"
    return "position"


def _pick(profile: str, base_key: str, position_key: str, default):
    """Для position берём профильный ключ, если он задан; иначе — общий."""
    if profile == "position":
        val = getattr(settings, position_key, None)
        if val is not None:
            return val
    return getattr(settings, base_key, default)


def depth_gate_params(regime: str | None, trade_mode: str | None = None) -> dict:
    """Полный набор порогов depth-гейта под профиль движка.

    Единая точка: и robot_loop, и scan-путь берут пороги отсюда, иначе движки
    снова разъедутся (так уже было — спред развели, остальное забыли).
    """
    profile = profile_for(regime, trade_mode)
    return {
        "profile": profile,
        "max_spread_pct": float(_pick(
            profile, "OB_MAX_SPREAD_PCT", "OB_POSITION_MAX_SPREAD_PCT", 0.08)),
        "obi_confirm": float(_pick(
            profile, "OB_OBI_CONFIRM", "OB_POSITION_OBI_CONFIRM", 0.15)),
        "wall_confirm": float(_pick(
            profile, "OB_WALL_CONFIRM_SHARE", "OB_POSITION_WALL_CONFIRM_SHARE", 0.30)),
        "cvd_block_ratio": float(_pick(
            profile, "OB_CVD_ENTRY_BLOCK_RATIO", "OB_POSITION_CVD_ENTRY_BLOCK_RATIO", 0.35)),
        "cvd_min_trades": int(_pick(
            profile, "OB_CVD_MIN_TRADES", "OB_POSITION_CVD_MIN_TRADES", 25)),
        "obi_hard_veto": float(_pick(
            profile, "OB_OBI_HARD_VETO", "OB_POSITION_OBI_HARD_VETO", 0.45)),
        "wall_rescue_max_adverse_obi": float(_pick(
            profile, "OB_WALL_RESCUE_MAX_ADVERSE_OBI",
            "OB_POSITION_WALL_RESCUE_MAX_ADVERSE_OBI", 0.35)),
        "cvd_thin_ratio": float(_pick(
            profile, "OB_CVD_THIN_RATIO", "OB_POSITION_CVD_THIN_RATIO", 0.9)),
        "cvd_thin_min_trades": int(_pick(
            profile, "OB_CVD_THIN_MIN_TRADES", "OB_POSITION_CVD_THIN_MIN_TRADES", 1)),
    }
