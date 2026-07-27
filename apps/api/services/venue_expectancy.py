"""Realized expectancy в разрезе площадки исполнения (#venue-expectancy-2026-07-27).

Зачем разделять. У spot и swap на HTX разная экономика оборота: тейкер 0.20%
против 0.05%, то есть round-trip 0.40% против 0.10% — вчетверо. Сводная
статистика их смешивает, и получается средняя температура: swap-сделки
выглядят хуже, чем они есть, spot — лучше. При средней победе +0.091% разница
в 0.30% на оборот определяет знак результата целиком, а не влияет на него.

Второй разрез — источник цены. Бумажная сделка, посчитанная по last, и живая,
исполненная в стакане, — это разные события, и складывать их в один ряд нельзя.

## Expected fill против achievable fill

Главная часть модуля. Фантомные филлы (#phantom-fill) возникли ровно из-за
отсутствия этой проверки: защитная ветка книжила цену, взятую из экономического
порога, и никто не сверял её с тем, что рынок вообще показывал. Историю пришлось
пересчитывать задним числом.

Теперь каждая сделка несёт три цены:

    expected   — та, на которую рассчитывали (план входа / уровень защиты)
    achievable — та, которую рынок реально давал в тот момент
    booked     — та, по которой записали результат

и два инварианта, нарушение которых означает баг, а не невезение:

    booked ≤ achievable   для выхода в плюс   (нельзя продать выше рынка)
    booked ≥ achievable   для выхода в минус  (нельзя откупить ниже рынка)

Расхождение expected и achievable — это цена исполнения, её надо знать и
закладывать. Расхождение achievable и booked — это ошибка учёта, и её надо
чинить. Раньше эти два случая были неразличимы.

Только чтение. На торговлю не влияет.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal


def market_type_of(signal: Any) -> str:
    """spot / swap по плану сделки, с честным «unknown» вместо догадки."""
    plan = (getattr(signal, "plan_json", None) or {})
    mt = str(plan.get("market_type") or plan.get("venue_market_type") or "").lower()
    if mt in ("spot", "swap", "futures", "perp"):
        return "spot" if mt == "spot" else "swap"
    exec_ = plan.get("execution") or {}
    mt = str(exec_.get("market_type") or "").lower()
    if mt:
        return "spot" if mt == "spot" else "swap"
    return "unknown"


def price_source_of(signal: Any) -> str:
    plan = (getattr(signal, "plan_json", None) or {})
    src = (plan.get("execution") or {}).get("price_source") or plan.get("price_source")
    return str(src or "unknown")


def fill_audit(signal: Any) -> dict[str, Any]:
    """Сверка expected / achievable / booked по одной сделке.

    `achievable` восстанавливается из траектории: максимум хода для выхода в
    плюс (лучше него рынок не давал) и последняя точка как факт на момент
    закрытия. Это тот же признак, что ловит детектор фантомов, но здесь он
    даёт величину расхождения, а не только флаг.
    """
    lc = ((getattr(signal, "plan_json", None) or {}).get("lifecycle") or {})
    out: dict[str, Any] = {
        "signal_id": getattr(signal, "id", None),
        "symbol": getattr(signal, "symbol", None),
        "market_type": market_type_of(signal),
        "price_source": price_source_of(signal),
    }

    try:
        booked = float(signal.result_pct)
    except (TypeError, ValueError):
        return {**out, "status": "no_result"}

    try:
        mfe = float(lc.get("mfe_pct"))
    except (TypeError, ValueError):
        return {**out, "status": "no_lifecycle", "booked_pct": booked}

    traj = lc.get("traj") or []
    try:
        last_traj = float(traj[-1][1])
    except (TypeError, ValueError, IndexError):
        last_traj = None

    # Достижимое: лучше пика рынок не давал. Для выхода в плюс это потолок.
    achievable = mfe if booked > 0 else (last_traj if last_traj is not None else booked)
    slippage = round(booked - achievable, 4) if achievable is not None else None

    # Инвариант: закрытие в плюс не может быть выше пика.
    violated = bool(booked > 0 and booked > mfe + 1e-9)

    expected = lc.get("planned_exit_pct")
    return {
        **out,
        "status": "ok",
        "expected_pct": float(expected) if expected is not None else None,
        "achievable_pct": round(achievable, 4) if achievable is not None else None,
        "booked_pct": round(booked, 4),
        "execution_gap_pct": slippage,
        "invariant_violated": violated,
        "note": ("booked выше достижимого — это ошибка УЧЁТА, а не проскальзывание"
                 if violated else None),
    }


def _agg(rows: list[Any]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"count": 0}
    net = 0.0
    costs = 0.0
    wins = 0
    with_money = 0
    gaps: list[float] = []
    violations = 0
    for s in rows:
        if s.closed_net_pnl is not None:
            v = float(s.closed_net_pnl)
            net += v
            with_money += 1
            wins += int(v > 0)
        if s.closed_total_cost is not None:
            costs += float(s.closed_total_cost)
        audit = fill_audit(s)
        if audit.get("status") == "ok":
            if audit.get("execution_gap_pct") is not None:
                gaps.append(float(audit["execution_gap_pct"]))
            violations += int(bool(audit.get("invariant_violated")))
    return {
        "count": n,
        "count_with_money": with_money,
        "expectancy_usdt": round(net / with_money, 6) if with_money else None,
        "net_pnl_usdt": round(net, 6),
        "costs_usdt": round(costs, 6),
        "avg_cost_usdt": round(costs / n, 6),
        "winrate_pct": round(wins / with_money * 100, 2) if with_money else None,
        "avg_execution_gap_pct": round(sum(gaps) / len(gaps), 4) if gaps else None,
        "worst_execution_gap_pct": round(min(gaps), 4) if gaps else None,
        "invariant_violations": violations,
    }


def by_venue(db: Session, *, window_hours: float = 720.0, limit: int = 2000) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))
    signals = (
        db.query(Signal)
        .filter(Signal.status == "closed", Signal.closed_at.isnot(None),
                Signal.closed_at >= cutoff)
        .order_by(Signal.id.desc())
        .limit(int(limit))
        .all()
    )
    if not signals:
        return {"status": "no_data", "window_hours": window_hours}

    by_market: dict[str, list] = {}
    by_source: dict[str, list] = {}
    for s in signals:
        by_market.setdefault(market_type_of(s), []).append(s)
        by_source.setdefault(price_source_of(s), []).append(s)

    violations = [
        a for a in (fill_audit(s) for s in signals)
        if a.get("invariant_violated")
    ]

    # Справочно — заявленные ставки. Расхождение фактической средней стоимости
    # оборота с этой таблицей означает, что реальный тариф отличается от
    # заложенного в модель, и вся экономика входа считается по неверной цифре.
    declared = {
        "spot": {
            "taker": float(getattr(settings, "SPOT_TAKER_FEE", 0.002)),
            "maker": float(getattr(settings, "SPOT_MAKER_FEE", 0.002)),
        },
        "swap": {
            "taker": float(getattr(settings, "FUTURES_TAKER_FEE", 0.0005)),
            "maker": float(getattr(settings, "FUTURES_MAKER_FEE", 0.0002)),
        },
        "slippage_buffer_pct": float(getattr(settings, "SLIPPAGE_BUFFER_PCT", 0.0002)),
    }

    return {
        "status": "ok",
        "window_hours": window_hours,
        "sample": len(signals),
        "declared_rates": declared,
        "by_market_type": [{"market_type": k, **_agg(v)} for k, v in sorted(by_market.items())],
        "by_price_source": [{"price_source": k, **_agg(v)} for k, v in sorted(by_source.items())],
        "invariant_violations": violations[:30],
        "invariant_violation_count": len(violations),
        "note": (
            "Round-trip spot ≈0.40% против swap ≈0.10% — вчетверо. При средней победе "
            "порядка 0.09% эта разница определяет знак результата, поэтому сводная "
            "статистика по площадкам бессмысленна. execution_gap_pct = booked − "
            "achievable: отрицательный это нормальное проскальзывание, ПОЛОЖИТЕЛЬНЫЙ "
            "означает запись результата лучше рынка, то есть ошибку учёта. Именно "
            "отсутствие этой проверки и породило фантомные филлы."
        ),
    }
