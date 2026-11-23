"""Единый источник фич и меток мета-лейблера. v6 (#ml-audit-2026-11-23).

row_to_features возвращает list[float] в порядке FEATURE_NAMES (контракт с
ml_meta_labeler). is_phantom_row / total_net_pnl / tp1_partial_net_pnl /
row_to_label сохранены — их импортируют ml_meta_labeler и ml_shadow_report.
"""
from __future__ import annotations

import math
from typing import Any

from core.config import settings
from services.close_reasons import reached_tp2

CVD_MIN_TRADES: int = 10
FEATURE_VERSION: int = 6
FEATURE_NAMES: list[str] = [
    "stop_distance_pct", "log_stop_distance",
    "net_rr_tp1", "rr_asymmetry",
    "spread_pct", "cvd_ratio",
    "hour_sin", "hour_cos",
    "is_trend_up", "is_trend_down", "is_range", "is_reversal", "is_scalp", "is_crt",
    "di_spread", "adx14", "adx_delta", "momentum_ord", "extension_atr",
    "is_long",
]
_MOM_ORD = {"bullish": 1.0, "overheated": 1.5, "neutral": 0.0, "bearish": -1.0, "oversold": -1.5}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _plan(row: dict) -> dict:
    p = row.get("plan") or row.get("plan_json") or {}
    return p if isinstance(p, dict) else {}


def _gate(row: dict, name: str) -> dict:
    for src in (row, _plan(row), row.get("gates") or {}):
        g = src.get(name)
        if isinstance(g, dict):
            return g
    return {}


def _depth(row: dict) -> dict:
    d = row.get("entry_depth")
    if isinstance(d, dict):
        return d
    p = _plan(row)
    if isinstance(p.get("entry_depth"), dict):
        return p["entry_depth"]
    return row


def _entry_price(row: dict) -> float:
    basis = str(getattr(settings, "ML_ENTRY_PRICE_BASIS", "zone_mid"))
    zone = row.get("entry_zone")
    if not isinstance(zone, dict):
        zone = _plan(row).get("entry_zone")
    mid = 0.0
    if isinstance(zone, dict):
        lo, hi = _f(zone.get("from")), _f(zone.get("to"))
        if lo > 0 and hi > 0:
            mid = (lo + hi) / 2.0
    if basis == "fill":
        lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
        px = _f(lc.get("entry_price"))
        if px > 0:
            return px
    return mid


def _hour_of_day(row: dict) -> float:
    raw = row.get("opened_at") or row.get("created_at")
    if raw:
        s = str(raw)
        for sep in ("T", " "):
            if sep in s:
                try:
                    parts = s.split(sep, 1)
                    if len(parts) > 1:
                        return float(int(parts[1][:2]))
                except (ValueError, IndexError):
                    break
    from datetime import datetime, timezone as _tz
    return float(datetime.now(_tz.utc).hour)


def _momentum(row: dict, mg: dict) -> str:
    mom = str(mg.get("momentum") or "")
    if mom:
        return mom
    rat = str(row.get("rationale") or row.get("entry_reason") or "")
    toks = rat.split("_")
    if "trend" in toks:
        i = toks.index("trend")
        if i + 2 < len(toks):
            return toks[i + 2]
    return ""


def features_dict(row: dict) -> dict:
    d = _depth(row)
    cvd_reliable = _f(d.get("cvd_trades")) >= float(CVD_MIN_TRADES)
    rr1 = _f(row.get("net_rr_tp1"))
    rr2 = _f(row.get("net_rr_tp2"))
    entry = _entry_price(row)
    stop = _f(row.get("stop_price"))
    stop_dist_pct = abs(entry - stop) / entry * 100.0 if entry > 1e-9 and stop > 0 else 0.0
    log_stop_dist = math.log(stop_dist_pct + 1e-5) if stop_dist_pct > 0 else 0.0
    hour = _hour_of_day(row)
    p = _plan(row)
    regime = str(row.get("regime") or p.get("regime") or "")
    trade_mode = str(row.get("trade_mode") or p.get("trade_mode") or "").lower()
    rationale = str(row.get("rationale") or "").lower()
    side = str(row.get("side") or "").lower()
    tz = _gate(row, "tz_shadow")
    tt = _gate(row, "trend_trigger")
    mg = _gate(row, "momentum_gate")
    return {
        "stop_distance_pct": stop_dist_pct,
        "log_stop_distance": log_stop_dist,
        "net_rr_tp1": rr1,
        "rr_asymmetry": rr2 / rr1 if rr1 > 1e-9 else 0.0,
        "spread_pct": _f(d.get("spread_pct")),
        "cvd_ratio": _f(d.get("cvd_ratio")) if cvd_reliable else 0.0,
        "hour_sin": math.sin(2 * math.pi * hour / 24.0),
        "hour_cos": math.cos(2 * math.pi * hour / 24.0),
        "is_trend_up": 1.0 if regime == "trend_up_candidate" else 0.0,
        "is_trend_down": 1.0 if regime == "trend_down_candidate" else 0.0,
        "is_range": 1.0 if regime == "range" else 0.0,
        "is_reversal": 1.0 if "reversal" in regime else 0.0,
        "is_scalp": 1.0 if (regime == "scalp" or trade_mode == "scalp" or "scalp" in rationale) else 0.0,
        "is_crt": 1.0 if (regime == "crt" or trade_mode == "crt" or "crt" in rationale) else 0.0,
        "di_spread": _f(tz.get("di_spread")),
        "adx14": _f(tz.get("adx")),
        "adx_delta": _f(tz.get("adx_delta")),
        "momentum_ord": _MOM_ORD.get(_momentum(row, mg), 0.0),
        "extension_atr": _f(tt.get("extension_atr")),
        "is_long": 1.0 if side in ("long", "buy") else 0.0,
    }


def row_to_features(row: dict) -> list[float]:
    fd = features_dict(row)
    return [float(fd[k]) for k in FEATURE_NAMES]


def logged_features(row: dict) -> list[float] | None:
    """Вектор, сохранённый live-скором на входе (plan.ml.features). None если нет/старый."""
    if not bool(getattr(settings, "ML_TRAIN_ON_LOGGED_FEATURES", True)):
        return None
    ml = _plan(row).get("ml") or row.get("ml") or {}
    if not isinstance(ml, dict):
        return None
    if int(ml.get("feature_version") or 0) != FEATURE_VERSION:
        return None
    vec = ml.get("features")
    if isinstance(vec, dict):
        return [float(vec.get(k, 0.0)) for k in FEATURE_NAMES]
    if isinstance(vec, list) and len(vec) == len(FEATURE_NAMES):
        return [float(x) for x in vec]
    return None


def is_phantom_row(row: dict) -> bool:
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    try:
        side = str(row.get("side") or "").lower()
        x = lc.get("exit_price")
        x = row.get("closed_exit_price") if x is None else x
        m = lc.get("max_profit_price")
        if side in ("long", "short") and x is not None and m is not None:
            x, m = float(x), float(m)
            if x > 0 and m > 0:
                return x > m * (1 + 1e-6) if side == "long" else x < m * (1 - 1e-6)
    except (TypeError, ValueError):
        pass
    try:
        return float(row["result_pct"]) > float(lc["mfe_pct"]) + 1e-9
    except (KeyError, TypeError, ValueError):
        return False


def tp1_partial_net_pnl(row: dict) -> float:
    v = row.get("tp1_partial_net_pnl")
    if v is None:
        part = _plan(row).get("tp1_partial")
        v = part.get("net_pnl") if isinstance(part, dict) else None
    out = _f(v, 0.0)
    return out if math.isfinite(out) else 0.0


def total_net_pnl(row: dict) -> float | None:
    pnl = row.get("closed_net_pnl")
    if pnl is None:
        return None
    try:
        pnl = float(pnl)
    except (TypeError, ValueError):
        return None
    return pnl if math.isfinite(pnl) else None


def row_to_label(row: dict, label_kind: str = "beats_costs", min_r: float = 0.3) -> int | None:
    labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
    if label_kind == "hit_tp2":
        if "hit_tp2" in labels:
            return 1 if labels.get("hit_tp2") else 0
        return 1 if reached_tp2(row.get("closed_reason"), row.get("plan") or {}) else 0
    pnl = total_net_pnl(row)
    if pnl is None:
        return None
    if label_kind == "is_win":
        return 1 if pnl > 0 else 0
    risk = abs(_f(row.get("net_pnl_stop")))
    if risk <= 1e-9:
        return None
    if label_kind == "net_r":
        thr = float(getattr(settings, "ML_LABEL_NET_R_MIN", 1.0))
        return 1 if (pnl / risk) >= thr else 0
    return 1 if (pnl / risk) >= float(min_r) else 0
