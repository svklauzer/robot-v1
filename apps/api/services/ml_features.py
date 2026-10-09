# services/ml_features.py
from __future__ import annotations
import math
from typing import Any
from services.close_reasons import reached_tp2

CVD_MIN_TRADES: int = 10
FEATURE_VERSION: int = 4  # Сброс старых несовместимых моделей

FEATURE_NAMES: list[str] = [
    "stop_distance_pct",
    "log_stop_distance",  
    "confidence",
    "net_rr_tp1",
    "rr_asymmetry",
    "spread_pct",
    "cvd_ratio",
    "hour_sin",           
    "hour_cos",           
    "is_scalp",           
    "is_crt"              
]

def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _grade_ord(grade: Any) -> float:
    return {"A+": 3.0, "A": 2.0, "B": 1.0, "C": 0.0}.get(str(grade or "").upper(), 1.0)


def _depth(row: dict) -> dict:
    d = row.get("entry_depth")
    return d if isinstance(d, dict) else {}

def _entry_price(row: dict) -> float:
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    px = _f(lc.get("entry_price"))
    if px > 0:
        return px
    zone = row.get("entry_zone")
    if isinstance(zone, dict):
        lo, hi = _f(zone.get("from")), _f(zone.get("to"))
        if lo > 0 and hi > 0:
            return (lo + hi) / 2.0
    return 0.0


def _hour_of_day(row: dict) -> float:
    raw = row.get("opened_at") or row.get("created_at")
    if raw:
        s = str(raw)
        for sep in ("T", " "):
            if sep in s:
                try:
                    # ИСПРАВЛЕНО: Безопасный строковый срез сегмента часов внутри сплита
                    parts = s.split(sep, 1)
                    if len(parts) > 1:
                        return float(int(parts[1][:2]))
                except (ValueError, IndexError):
                    break
    from datetime import datetime, timezone as _tz
    return float(datetime.now(_tz.utc).hour)

def row_to_features(row: dict) -> list[float]:
    d = _depth(row)
    cvd_trades = _f(d.get("cvd_trades"))
    cvd_reliable = cvd_trades >= float(CVD_MIN_TRADES)

    rr1 = _f(row.get("net_rr_tp1"))
    rr2 = _f(row.get("net_rr_tp2"))

    entry = _entry_price(row)
    stop = _f(row.get("stop_price"))
    stop_dist_pct = abs(entry - stop) / entry * 100.0 if entry > 1e-9 and stop > 0 else 0.0
    log_stop_dist = math.log(stop_dist_pct + 1e-5) if stop_dist_pct > 0 else 0.0

    hour = _hour_of_day(row)
    hour_sin = math.sin(2 * math.pi * hour / 24.0)
    hour_cos = math.cos(2 * math.pi * hour / 24.0)

    rationale = str(row.get("rationale") or "").lower()
    trade_mode = str(row.get("trade_mode") or "").lower()
    is_scalp = 1.0 if "scalp" in rationale or trade_mode == "scalp" else 0.0
    is_crt = 1.0 if "crt" in rationale or trade_mode == "crt" else 0.0

    return [
        stop_dist_pct,
        log_stop_dist,
        _f(row.get("confidence"), 60.0),
        rr1,
        rr2 / rr1 if rr1 > 1e-9 else 0.0,
        _f(d.get("spread_pct")),
        _f(d.get("cvd_ratio")) if cvd_reliable else 0.0,
        hour_sin,
        hour_cos,
        is_scalp,
        is_crt
    ]


def is_phantom_row(row: dict) -> bool:
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    try:
        return float(row["result_pct"]) > float(lc["mfe_pct"]) + 1e-9
    except (KeyError, TypeError, ValueError):
        return False

def row_to_label(row: dict, label_kind: str = "beats_costs", min_r: float = 0.3) -> int | None:
    labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
    if label_kind == "hit_tp2":
        if "hit_tp2" in labels:
            return 1 if labels.get("hit_tp2") else 0
        return 1 if reached_tp2(row.get("closed_reason"), row.get("plan") or {}) else 0

    pnl = row.get("closed_net_pnl")
    if pnl is None:
        return None
    try:
        pnl = float(pnl)
    except (TypeError, ValueError):
        return None

    if label_kind == "is_win":
        return 1 if pnl > 0 else 0

    risk = abs(_f(row.get("net_pnl_stop")))
    if risk <= 1e-9:
        return None
    return 1 if (pnl / risk) >= float(min_r) else 0
