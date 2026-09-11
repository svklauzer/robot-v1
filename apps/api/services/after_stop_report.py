"""Что бывает со следующей сделкой после стопа на том же символе (#after-stop-2026-09-12).

Разбор стопов 12.09 (471 сделка, база 34% стопов): сильного признака входа нет,
но в разрезе performance guard сделки после стопа на символе
(`small_history_last_stop_reduce_risk`, 89 шт.) уходили в стоп в 43.8% против
27.8% у сделок без истории по символу. Защита на это сейчас только уменьшает
размер, а пауза после стопа — 3 часа (`reentry_cooldown`).

Метка performance guard — грубый заместитель: она смешивает «истории мало» и
«последняя — стоп». Здесь то же прямо: для каждой сделки берётся последняя
ЗАКРЫТАЯ до её открытия сделка по тому же символу (по любой стороне или по той
же — `same_side`), и сделки раскладываются по тому, чем та закончилась и сколько
часов прошло. Если после стопа доля стопов выше базы и держится часами — это
основание для паузы или более строгого входа после стопа; если только в первые
часы — нынешней паузы достаточно.

Ничего не блокирует — только показания.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from models.signal import Signal
from services.stop_loss_forensics import STOP_REASON, _honest_net, _wilson

_GAPS: tuple[tuple[float, float, str], ...] = (
    (0.0, 6.0, "0-6h"),
    (6.0, 24.0, "6-24h"),
    (24.0, 72.0, "24-72h"),
    (72.0, float("inf"), "72h+"),
)
_LOOKBACK_EXTRA_HOURS = 24.0 * 14


def _aware(moment):
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _start(signal: Signal):
    return _aware(signal.opened_at) or _aware(signal.created_at)


def _bucket(prev: Signal | None, start) -> str:
    if prev is None:
        return "no_previous"
    if str(prev.closed_reason or "") != STOP_REASON:
        return "after_non_stop"
    gap = (start - _aware(prev.closed_at)).total_seconds() / 3600.0
    for lo, hi, label in _GAPS:
        if lo <= gap < hi:
            return f"after_stop_{label}"
    return "after_stop_72h+"


def _summary(items: list[Signal], total_stops: int, total: int) -> dict:
    stops = sum(1 for s in items if str(s.closed_reason or "") == STOP_REASON)
    net = sum(_honest_net(s) for s in items)
    return {
        "n": len(items),
        "stops": stops,
        "stop_rate": round(stops / len(items), 4) if items else None,
        "stop_rate_ci": _wilson(stops, len(items)),
        "net_usdt": round(net, 2),
        "avg_usdt": round(net / len(items), 4) if items else None,
    }


def build(db: Session, *, window_hours: float = 2160.0, same_side: bool = False,
          max_rows: int = 6000) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=float(window_hours))
    context_cutoff = cutoff - timedelta(hours=_LOOKBACK_EXTRA_HOURS)
    signals = (
        db.query(Signal)
        .filter(Signal.status == "closed", Signal.closed_at.isnot(None),
                Signal.closed_at >= context_cutoff)
        .order_by(Signal.id.desc())
        .limit(int(max_rows))
        .all()
    )

    by_key: dict[tuple, list[Signal]] = {}
    for s in signals:
        key = (s.symbol, s.side) if same_side else (s.symbol,)
        by_key.setdefault(key, []).append(s)
    for items in by_key.values():
        items.sort(key=lambda s: _aware(s.closed_at))

    buckets: dict[str, list[Signal]] = {}
    for s in signals:
        closed = _aware(s.closed_at)
        start = _start(s)
        if closed is None or start is None or closed < cutoff:
            continue
        key = (s.symbol, s.side) if same_side else (s.symbol,)
        # Последняя сделка по ключу, закрытая ДО открытия этой.
        prev = None
        for cand in by_key.get(key, []):
            if cand.id == s.id:
                continue
            cand_closed = _aware(cand.closed_at)
            if cand_closed is not None and cand_closed <= start:
                prev = cand
            elif cand_closed is not None and cand_closed > start:
                break
        buckets.setdefault(_bucket(prev, start), []).append(s)

    analysed = [s for items in buckets.values() for s in items]
    total_stops = sum(1 for s in analysed if str(s.closed_reason or "") == STOP_REASON)
    after_stop = [s for k, v in buckets.items() if k.startswith("after_stop_") for s in v]

    order = (["no_previous", "after_non_stop"]
             + [f"after_stop_{label}" for _, _, label in _GAPS])
    return {
        "window_hours": float(window_hours),
        "same_side": same_side,
        "analysed": len(analysed),
        "base": _summary(analysed, total_stops, len(analysed)),
        "after_stop_any": _summary(after_stop, total_stops, len(analysed)),
        "buckets": {k: _summary(buckets.get(k, []), total_stops, len(analysed)) for k in order},
        "note": (
            "Для каждой сделки — последняя закрытая до её открытия сделка по тому же "
            "символу (same_side=true — и той же стороне). after_stop_* — та "
            "закрылась стопом, суффикс — сколько часов прошло до открытия этой. "
            "Нынешняя пауза после стопа — 3 часа. stop_rate_ci — 95% Уилсона; "
            "разница с базой что-то значит, только если интервалы не перекрываются."
        ),
    }
