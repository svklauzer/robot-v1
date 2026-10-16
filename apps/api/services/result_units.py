"""Единицы результата сделки (#result-units-2026-10-16).

Инвариант репозитория: `result_pct` — чистый результат в ПРОЦЕНТАХ НОМИНАЛА
(= цена, нетто издержек): closed_net_pnl == notional * result_pct / 100. В тех же
единицах лежат lifecycle.mfe_pct / mae_pct / final_result_pct и точки traj, и на
этом стоят phantom_fill, exit_replay, capture-rate, validation_gates.

С сигнала #621 (20.09.2026) _close_signal начал писать result_pct в процентах
МАРЖИ (плечо 10 → ×10), и все сравнения result_pct с mfe_pct стали ложными.
"""
from __future__ import annotations


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def signal_notional(signal) -> float | None:
    """Номинал позиции = исходный qty * цена входа. None — нет данных (не гадаем)."""
    plan = getattr(signal, "plan_json", None) or {}
    if not isinstance(plan, dict):
        plan = {}
    lc = plan.get("lifecycle") if isinstance(plan.get("lifecycle"), dict) else {}
    qty = _f(getattr(signal, "qty", None)) or _f(plan.get("qty"))
    entry = _f(lc.get("entry_price"))
    if entry is None:
        zone = getattr(signal, "entry_zone", None) or plan.get("entry_zone") or {}
        if isinstance(zone, dict):
            lo, hi = _f(zone.get("from")), _f(zone.get("to"))
            if lo and hi:
                entry = (lo + hi) / 2.0
    if qty and entry and qty > 0 and entry > 0:
        return qty * entry
    return None


def pct_of_notional(net_pnl, signal) -> float | None:
    n = signal_notional(signal)
    p = _f(net_pnl)
    if not n or p is None:
        return None
    return round(p / n * 100.0, 4)
