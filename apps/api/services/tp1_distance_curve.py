"""Где ставить TP1 (#tp1-distance-2026-09-12).

Рычаги геометрии по очереди:

  * гейт на TP1 — оценка не разделяет, закрыт 11.09;
  * ширина стопа — `/analytics/stop-width` после калибровки исполнения стопа:
    урезание не даёт ничего (оптимистично около нуля без направления,
    пессимистично минус на всех уровнях), закрыт 12.09;
  * дистанция TP1 — этот отчёт.

32 сделки из 469 за 90 дней прошли 80–99% пути к TP1 и не дотянули. С TP1 ближе
часть из них стала бы маленьким выигрышем: половина фиксируется на новом TP1,
стоп остатка встаёт туда же. Зато дошедшие до прежнего TP1 зарабатывают
меньше. Какая сторона перевешивает, показывает только проигрыш траекторий.

Мир проигрыша — нынешние правила: частичная фиксация на TP1 и стоп остатка на
самом TP1 (`POST_TP1_LOCK_FRAC=1.0`). TP1 ставится на долю j нынешней
дистанции, и каждая сделка проходит по своей траектории, первое событие решает:

  * цена до нового TP1 не дошла — исход прежний: путь тот же, до прежнего TP1
    (он дальше) она не доходила тоже;
  * дошла — доля фиксируется на новом TP1, стоп остатка встаёт туда же;
    вернулась ниже — остаток закрыт на нём (с недостачей исполнения стопа),
    не вернулась — остаток доезжает до фактического выхода (не выше пика).

Сравнивать нужно со строкой j = 1.0 того же проигрыша (`vs_same_rules`), а не с
фактом: факт прожит по старым правилам после TP1 (безубыток, трейл), и разница
с ним смешивает эффект дистанции с эффектом стопа остатка.

Ничего не меняет и не блокирует — только показания.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal
from services.stop_loss_forensics import _honest_net
from services.stop_width_curve import _fill_residual, _notional, _stop_dist_pct
from services.tp1_overshoot import _actual_cost_pct, _entry_price, _num, _tp1_dist_pct

_FRACS: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
_DEFAULT_COST_PCT = 0.14


def _points(traj: list) -> list[float]:
    out: list[float] = []
    for point in traj or []:
        try:
            out.append(float(point[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _replay(path: list[float], target: float, step: float, *,
            pessimistic: bool) -> tuple[bool, bool]:
    """(дошла до цели, вернулась ниже неё после пересечения).

    Пессимистично возврат засчитывается, если записанный минимум после
    пересечения — вместе с самой точкой пересечения — был ближе шага к цели:
    стоп на цели встаёт вплотную к цене, и незаписанный тик его снимает.
    """
    for i, pct in enumerate(path):
        if pct < target:
            continue
        after = path[i + 1:]
        if pessimistic:
            low = min([pct] + after)
            return True, low - step < target
        return True, any(v < target for v in after)
    return False, False


def _analyse(signal: Signal) -> dict | None:
    plan = signal.plan_json or {}
    lifecycle = plan.get("lifecycle") or {}
    path = _points(lifecycle.get("traj") or [])
    entry = _entry_price(signal, lifecycle)
    if not path or not entry:
        return None
    tp1_dist = _tp1_dist_pct(signal, entry)
    stop_dist = _stop_dist_pct(signal, entry)
    notional = _notional(signal, entry)
    mfe = _num(lifecycle.get("mfe_pct"))
    if tp1_dist is None or stop_dist is None or not notional or mfe is None:
        return None

    exit_price = _num(signal.closed_exit_price)
    if exit_price:
        raw = (exit_price - entry) / entry * 100.0
        exit_pct = raw if str(signal.side).lower() == "long" else -raw
    else:
        exit_pct = path[-1]
    cost = _actual_cost_pct(signal, entry)
    share = _num(((plan.get("config") or {}).get("exit") or {}).get("tp1_partial_share"))
    return {
        "id": signal.id,
        "reason": str(signal.closed_reason or "unknown"),
        "trade_mode": str(plan.get("trade_mode") or "unknown"),
        "path": path,
        "tp1_dist_pct": tp1_dist,
        "stop_dist_pct": stop_dist,
        # Выход не выше пика: tp2_reached книжит цену TP2, закрываясь раньше.
        "exit_pct": min(exit_pct, mfe),
        "actual_pct": _honest_net(signal) / notional * 100.0,
        "cost_pct": cost if cost is not None else _DEFAULT_COST_PCT,
        "notional_usdt": notional,
        "share": share if share and 0 < share < 1 else float(
            getattr(settings, "TP1_PARTIAL_CLOSE_SHARE", 0.5)),
        "step": _num(lifecycle.get("traj_step")) or float(
            getattr(settings, "TRAJ_MIN_STEP_PCT", 0.05)),
    }


def _outcome(r: dict, j: float, fill: float, *, pessimistic: bool) -> tuple[float, bool]:
    """(результат сделки в % номинала при TP1 на доле j, дошла ли до нового TP1)."""
    target = j * r["tp1_dist_pct"]
    reached, retest = _replay(r["path"], target, r["step"], pessimistic=pessimistic)
    if not reached:
        return r["actual_pct"], False
    share = r["share"]
    rest = (target + fill) if retest else max(r["exit_pct"], target)
    return share * target + (1 - share) * rest - r["cost_pct"], True


def _curve(rows: list[dict], fill: float) -> list[dict]:
    actual_sum = sum(r["actual_pct"] for r in rows)
    base = {p: sum(_outcome(r, 1.0, fill, pessimistic=p)[0] for r in rows)
            for p in (False, True)}
    out = []
    for j in _FRACS:
        opt = [_outcome(r, j, fill, pessimistic=False) for r in rows]
        pess = [_outcome(r, j, fill, pessimistic=True) for r in rows]
        converted = sum(1 for r, (_, hit) in zip(rows, opt)
                        if hit and r["reason"] == "stop_loss")
        usdt = sum((v - r["actual_pct"]) * r["notional_usdt"] / 100.0
                   for r, (v, _) in zip(rows, opt))
        base_usdt = sum((_outcome(r, 1.0, fill, pessimistic=False)[0] - r["actual_pct"])
                        * r["notional_usdt"] / 100.0 for r in rows)
        out.append({
            "tp1_frac": j,
            "n": len(rows),
            "reach_rate": round(sum(1 for _, hit in opt if hit) / len(rows), 4) if rows else None,
            # Главное сравнение: тот же мир правил, другая дистанция TP1.
            "vs_same_rules_pct": round(sum(v for v, _ in opt) - base[False], 4),
            "vs_same_rules_pessimistic_pct": round(sum(v for v, _ in pess) - base[True], 4),
            "vs_same_rules_usdt": round(usdt - base_usdt, 2),
            "vs_actual_pct": round(sum(v for v, _ in opt) - actual_sum, 4),
            # Сделки, закрытые по стопу, которые с таким TP1 успели бы
            # зафиксировать долю и поставить стоп остатка в плюс.
            "stops_converted": converted,
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

    fill = _fill_residual(rows)
    return {
        "window_hours": float(window_hours),
        "trade_mode": trade_mode,
        "side": side,
        "analysed": len(rows),
        "skipped_no_data": skipped,
        "actual_sum_usdt": round(sum(r["actual_pct"] * r["notional_usdt"] / 100.0
                                     for r in rows), 2),
        "stop_fill_residual": fill,
        "curve": _curve(rows, fill["median_pct"]),
        "note": (
            "tp1_frac — доля нынешней дистанции TP1. Мир проигрыша — нынешние "
            "правила: частичная фиксация на TP1 и стоп остатка на самом TP1. "
            "Не дошла до нового TP1 — исход прежний; дошла — доля фиксируется "
            "там, остаток закрывается на первом возврате ниже, иначе доезжает до "
            "фактического выхода (не выше пика). vs_same_rules — против строки "
            "1.0 того же проигрыша: это эффект дистанции TP1 в чистом виде. "
            "vs_actual смешивает его с эффектом стопа остатка. pessimistic — "
            "возврат засчитывается, если минимум после пересечения был ближе "
            "шага траектории к цели."
        ),
    }
