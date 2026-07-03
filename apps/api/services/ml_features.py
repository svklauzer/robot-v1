"""ML feature contract — единый источник правды по признакам мета-лейблера.

Превращает И логированную строку trade_outcomes.jsonl (для обучения), И живого
кандидата из robot_loop (для предсказания) в ОДИН и тот же числовой вектор.
Любое расхождение train/serve — главный источник тихих багов в ML, поэтому
извлечение признаков живёт в одном месте.

Признаки берём ТОЛЬКО те, что есть и в логах, и у живого кандидата:
scores нет в старых логах → не используем; берём confidence/grade/regime/side/
RR/entry_depth (стакан). Всё дефолтится безопасно (нет данных → нейтраль).
"""
from __future__ import annotations

from typing import Any

# (#audit-ml-cvd) CVD из окна с горсткой сделок — шум (cvd_ratio схлопывается в ±1.0
# при 1–2 сделках; в live так почти всегда). Ниже порога зануляем CVD-фичи —
# ОДИНАКОВО в train и serve, иначе train/serve skew.
CVD_MIN_TRADES: int = 10

# Порядок ВАЖЕН и фиксирован — модель обучается и предсказывает по нему.
FEATURE_NAMES: list[str] = [
    "confidence",
    "grade_ord",          # A+=3 A=2 B=1 C=0
    "side_is_short",      # 1 short / 0 long
    "net_rr_tp1",
    "net_rr_tp2",
    "is_trend_down",
    "is_trend_up",
    "is_crt",
    "is_reversal",
    "spread_pct",
    "obi",
    "bid_wall_share",
    "ask_wall_share",
    "cvd_ratio",
    "cvd_trades",
]


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _grade_ord(grade: Any) -> float:
    return {"A+": 3.0, "A": 2.0, "B": 1.0, "C": 0.0}.get(str(grade or "").upper(), 1.0)


def _depth(row: dict) -> dict:
    d = row.get("entry_depth")
    return d if isinstance(d, dict) else {}


def row_to_features(row: dict) -> list[float]:
    """Логированная строка ИЛИ живой кандидат → вектор фич (порядок FEATURE_NAMES)."""
    regime = str(row.get("regime") or "").lower()
    side = str(row.get("side") or row.get("action") or "").lower()
    d = _depth(row)
    cvd_trades = _f(d.get("cvd_trades"))
    cvd_reliable = cvd_trades >= float(CVD_MIN_TRADES)
    return [
        _f(row.get("confidence"), 60.0),
        _grade_ord(row.get("grade")),
        1.0 if side in ("short", "sell") else 0.0,
        _f(row.get("net_rr_tp1")),
        _f(row.get("net_rr_tp2")),
        1.0 if "trend_down" in regime else 0.0,
        1.0 if "trend_up" in regime else 0.0,
        1.0 if "crt" in regime else 0.0,
        1.0 if "reversal" in regime else 0.0,
        _f(d.get("spread_pct")),
        _f(d.get("obi")),
        _f(d.get("bid_wall_share")),
        _f(d.get("ask_wall_share")),
        _f(d.get("cvd_ratio")) if cvd_reliable else 0.0,
        cvd_trades,
    ]


def row_to_label(row: dict, label_kind: str = "is_win") -> int | None:
    """Метка из логированного исхода. None — если строка ещё без исхода."""
    labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
    if label_kind == "hit_tp2":
        if "hit_tp2" in labels:
            return 1 if labels.get("hit_tp2") else 0
        return 1 if str(row.get("closed_reason")) == "tp2_reached" else 0
    # default: is_win по closed_net_pnl
    if "is_win" in labels:
        return 1 if labels.get("is_win") else 0
    pnl = row.get("closed_net_pnl")
    if pnl is None:
        return None
    try:
        return 1 if float(pnl) > 0 else 0
    except (TypeError, ValueError):
        return None
