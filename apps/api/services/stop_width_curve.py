"""Не слишком ли далеко стоит стоп (#stop-width-2026-09-12).

Разбор 11.09 (`/analytics/stop-forensics?outcome=tp1`) закрыл вопрос о гейте на
TP1. Признак, по которому он судил бы, не разделяет (AUC 0.60, интервал
[0.45; 0.75]), а лучшая когорта — сделки, прошедшие условия ТЗ, — доходит до
TP1 в 38% случаев и всё равно теряет −0.41 USDT на сделку. Дело не в том, как
часто сделка доходит до TP1, а в том, сколько теряет недошедшая против того,
что приносит дошедшая: плановый RR до TP1 у нас около 0.3–0.5, стоп стоит
дальше цели.

Этот отчёт проверяет один рычаг — ширину стопа. Стоп переносится на долю k
нынешней дистанции, и каждая сделка проигрывается по своей траектории:

  * цена до пересечения TP1 опустилась ниже −k·стоп — сделка закрыта там, в
    минус на всю позицию (частичной фиксации до TP1 нет);
  * не опустилась — исход тот же, что был: путь цены тот же, до настоящего
    стопа она тоже не доходила.

После TP1 ширина исходного стопа уже не важна: стоп переносится на уровень
после TP1. Поэтому смотрится только участок до TP1 — первое событие решает
(см. отчёт после TP1: брать лучшее из двух исходов нельзя).

Начальный стоп в плане не записан: `signal.stop_price` после TP1 переписан.
Дистанция восстанавливается из плановой потери на стопе:
`|net_pnl_stop| = qty·|вход − стоп| + издержки`. Насколько восстановление
верно, отчёт проверяет сам на сделках, закрытых по стопу: их траектория
обязана доходить до восстановленного уровня (`consistency`).

Пессимистичная граница — как у кривой стопа после TP1: касание засчитывается,
если записанный минимум был ближе шага траектории к уровню. На k = 1.0 истина
известна (это настоящий стоп), и строка — ноль по определению.

Ничего не меняет и не блокирует — только показания.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal
from services.stop_loss_forensics import _honest_net
from services.tp1_overshoot import _actual_cost_pct, _entry_price, _num, _tp1_dist_pct

_FRACS: tuple[float, ...] = (0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
_DEFAULT_COST_PCT = 0.14


def _notional(signal: Signal, entry: float) -> float | None:
    qty = _num(signal.qty) or _num((signal.plan_json or {}).get("qty"))
    if not qty or entry <= 0:
        return None
    return qty * entry


def _stop_dist_pct(signal: Signal, entry: float) -> float | None:
    """Исходная дистанция стопа в % — из плановой потери на стопе без издержек."""
    plan = signal.plan_json or {}
    risk = _num(signal.net_pnl_stop)
    if risk is None:
        risk = _num(plan.get("net_pnl_stop"))
    notional = _notional(signal, entry)
    if not risk or not notional:
        return None
    cost_pct = _actual_cost_pct(signal, entry)
    cost_usdt = notional * (cost_pct if cost_pct is not None else _DEFAULT_COST_PCT) / 100.0
    gross = abs(risk) - cost_usdt
    if gross <= 0:
        return None
    return gross / notional * 100.0


def _pre_tp1_low(traj: list, tp1_dist: float | None) -> tuple[float | None, bool]:
    """Минимум траектории до первого пересечения TP1 и было ли пересечение."""
    low: float | None = None
    for point in traj or []:
        try:
            pct = float(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        if tp1_dist is not None and pct >= tp1_dist:
            return low, True
        low = pct if low is None else min(low, pct)
    return low, False


def _analyse(signal: Signal) -> dict | None:
    plan = signal.plan_json or {}
    lifecycle = plan.get("lifecycle") or {}
    traj = lifecycle.get("traj") or []
    entry = _entry_price(signal, lifecycle)
    if not traj or not entry:
        return None
    stop_dist = _stop_dist_pct(signal, entry)
    notional = _notional(signal, entry)
    if stop_dist is None or not notional:
        return None

    low, crossed = _pre_tp1_low(traj, _tp1_dist_pct(signal, entry))
    if low is None:
        return None
    cost = _actual_cost_pct(signal, entry)
    return {
        "id": signal.id,
        "symbol": signal.symbol,
        "side": signal.side,
        "trade_mode": str(plan.get("trade_mode") or "unknown"),
        "reason": str(signal.closed_reason or "unknown"),
        "stop_dist_pct": round(stop_dist, 4),
        "pre_tp1_low_pct": round(low, 4),
        # Насколько глубоко цена ушла против нас до TP1, в долях стопа.
        "depth_in_stops": round(max(-low, 0.0) / stop_dist, 4),
        "reached_tp1": crossed,
        "actual_pct": round(_honest_net(signal) / notional * 100.0, 4),
        "cost_pct": round(cost if cost is not None else _DEFAULT_COST_PCT, 4),
        "notional_usdt": round(notional, 2),
        "traj_step_pct": _num(lifecycle.get("traj_step")) or float(
            getattr(settings, "TRAJ_MIN_STEP_PCT", 0.05)),
    }


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(int(round(q * (len(ordered) - 1))), len(ordered) - 1)
    return round(ordered[idx], 4)


def _curve(rows: list[dict]) -> list[dict]:
    actual_sum = sum(r["actual_pct"] for r in rows)
    out = []
    for k in _FRACS:
        opt, pess = [], []
        usdt_opt = usdt_pess = 0.0
        winners_cut = losers_trimmed = 0
        for r in rows:
            level = -k * r["stop_dist_pct"]
            low, step = r["pre_tp1_low_pct"], r["traj_step_pct"]
            stopped = -k * r["stop_dist_pct"] - r["cost_pct"]
            if k >= 1.0:
                # Настоящий стоп: что было, то и было.
                hit = hit_p = False
            else:
                hit = low < level
                hit_p = low - step < level
            cf = stopped if hit else r["actual_pct"]
            cf_p = stopped if hit_p else r["actual_pct"]
            opt.append(cf)
            pess.append(cf_p)
            usdt_opt += (cf - r["actual_pct"]) * r["notional_usdt"] / 100.0
            usdt_pess += (cf_p - r["actual_pct"]) * r["notional_usdt"] / 100.0
            if hit and r["reached_tp1"]:
                winners_cut += 1
            if hit and r["reason"] == "stop_loss":
                losers_trimmed += 1
        out.append({
            "stop_frac": k,
            "n": len(rows),
            "vs_actual_pct": round(sum(opt) - actual_sum, 4),
            "vs_actual_pessimistic_pct": round(sum(pess) - actual_sum, 4),
            "vs_actual_usdt": round(usdt_opt, 2),
            "vs_actual_pessimistic_usdt": round(usdt_pess, 2),
            # Сделки, дошедшие до TP1, которые такой стоп закрыл бы раньше.
            "winners_cut": winners_cut,
            # Стопы, которые закрылись бы на меньшей потере.
            "losers_trimmed": losers_trimmed,
        })
    return out


def build(db: Session, *, window_hours: float = 2160.0, trade_mode: str | None = None,
          side: str | None = None, max_rows: int = 4000) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))
    query = (
        db.query(Signal)
        .filter(Signal.status == "closed", Signal.closed_at.isnot(None),
                Signal.closed_at >= cutoff)
    )
    if side in ("long", "short"):
        query = query.filter(Signal.side == side)
    signals = query.order_by(Signal.id.desc()).limit(int(max_rows)).all()

    rows, skipped = [], 0
    for s in signals:
        row = _analyse(s)
        if row is None:
            skipped += 1
            continue
        if trade_mode and row["trade_mode"] != trade_mode:
            continue
        rows.append(row)

    stops = [r for r in rows if r["reason"] == "stop_loss"]
    # Самопроверка восстановленного стопа: траектория сделки, закрытой по
    # стопу, обязана доходить до него (с точностью до шага записи).
    consistent = [r for r in stops
                  if r["pre_tp1_low_pct"] <= -r["stop_dist_pct"] + r["traj_step_pct"]]
    winners = [r["depth_in_stops"] for r in rows if r["reached_tp1"]]

    return {
        "window_hours": float(window_hours),
        "trade_mode": trade_mode,
        "side": side,
        "analysed": len(rows),
        "skipped_no_data": skipped,
        "actual_sum_pct": round(sum(r["actual_pct"] for r in rows), 4),
        "actual_sum_usdt": round(sum(r["actual_pct"] * r["notional_usdt"] / 100.0
                                     for r in rows), 2),
        "median_stop_dist_pct": round(median([r["stop_dist_pct"] for r in rows]), 4)
        if rows else None,
        "consistency": {
            "stop_loss_trades": len(stops),
            "reach_derived_stop": len(consistent),
            "share": round(len(consistent) / len(stops), 4) if stops else None,
        },
        # Как глубоко против нас уходили сделки, которые потом дошли до TP1, —
        # в долях стопа. p90 = 0.5 значит: стоп вдвое ближе закрыл бы лишь
        # каждого десятого из них.
        "winners_depth_in_stops": {
            "n": len(winners),
            "p50": _quantile(winners, 0.50),
            "p75": _quantile(winners, 0.75),
            "p90": _quantile(winners, 0.90),
            "max": round(max(winners), 4) if winners else None,
        },
        "curve": _curve(rows),
        "note": (
            "stop_frac — доля нынешней дистанции стопа. Каждая сделка "
            "проигрывается по траектории до TP1: ниже −k·стоп — закрыта там на "
            "всю позицию, иначе исход прежний. vs_actual — разница сумм в % "
            "номинала (и в USDT) против факта; pessimistic — касание "
            "засчитывается, если записанный минимум был ближе шага траектории. "
            "consistency.share — доля сделок по стопу, чья траектория доходит до "
            "восстановленного стопа: если она заметно ниже 1, дистанция стопа "
            "восстановлена неверно и кривой верить нельзя."
        ),
    }
