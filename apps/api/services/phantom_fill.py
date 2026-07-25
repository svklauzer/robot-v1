"""Детектор фантомных филлов — единый источник правды (#phantom-fill-2026-07-25).

Баг: защитные ветки `exit_policy` книжили цену выхода по экономическому гейту
`MIN_PROTECTIVE_EXIT_PCT`, а не по рынку. Сделка «закрывалась» по цене, которой
рынок не видел (TRX #281: филл 0.334561 при максимуме 0.331845 — ровно
entry×1.018; +8.18 USDT вместо честных ~+1.6).

Сам баг исправлен, но ИСТОРИЯ остаётся завышенной, и от `closed_net_pnl` питаются
все потребители: /analytics/summary, validation-gates, symbol-performance, ML-метки,
дневные отчёты. Пока такие исходы не выкатятся из окна, любой вердикт о готовности
к live опирается на прибыль, которой не было.

Диагностический признак: записанный `result_pct` выше максимума, который сделка
вообще достигала (`lifecycle.mfe_pct`). Стоп/трейл не может исполниться лучше рынка.
"""
from __future__ import annotations

from typing import Any, Iterable


def phantom_adjustment(signal: Any) -> tuple[bool, float]:
    """(is_phantom, поправка_USDT<=0).

    Поправка — сколько нужно ВЫЧЕСТЬ из `closed_net_pnl`, чтобы получить честный
    результат (филл по последней цене траектории, т.е. по рынку на момент закрытия).
    """
    lifecycle = (getattr(signal, "plan_json", None) or {}).get("lifecycle") or {}
    try:
        booked = float(signal.result_pct)
        mfe = float(lifecycle.get("mfe_pct"))
    except (TypeError, ValueError):
        return False, 0.0
    if booked <= mfe + 1e-9:
        return False, 0.0

    traj = lifecycle.get("traj") or []
    try:
        honest = float(traj[-1][1])
    except (TypeError, ValueError, IndexError):
        honest = mfe
    try:
        notional = float(getattr(signal, "required_margin", 0) or 0.0)
    except (TypeError, ValueError):
        notional = 0.0
    return True, -abs(notional * (booked - honest) / 100.0)


def summarize(signals: Iterable[Any], *, max_ids: int = 20) -> dict[str, Any]:
    """Сводка по выборке закрытых сигналов.

    Возвращает count / суммарную поправку (<=0) / список id для расследования.
    """
    count = 0
    delta = 0.0
    ids: list[int] = []
    for signal in signals:
        is_phantom, adj = phantom_adjustment(signal)
        if not is_phantom:
            continue
        count += 1
        delta += adj
        if len(ids) < max_ids:
            try:
                ids.append(int(signal.id))
            except (TypeError, ValueError):
                pass
    return {
        "phantom_fill_count": count,
        "phantom_fill_delta_usdt": round(delta, 6),
        "phantom_fill_overstatement_usdt": round(abs(delta), 6),
        "phantom_fill_signal_ids": ids,
    }
