"""Фора лимитного входа: чем бумага лучше того, что получит live
(#entry-drift-2026-09-19).

Зачем. `services/entry_zone.py` переносит вход к стенке или микро-VWAP и берёт
цену, ВЫГОДНУЮ нам: для лонга ниже рынка, для шорта выше (там прямо так и
написано — «вход должен ждать рынок, а не догонять его»). Бумага книжит сделку
по этой цене и всегда считает её исполненной.

Live так не умеет: `LiveExecutor` шлёт рыночный ордер (`create_order_once(...,
"market", ...)`), и учёт берёт фактическую среднюю цену филла. То есть на каждом
перенесённом входе бумажный результат лучше живого ровно на `drift_pct` — и это
не комиссия, которую можно сэкономить мейкерской ставкой, а фора, которой в live
не будет вовсе, пока вход не станет настоящим лимитным ордером.

Отчёт считает размер этой форы по закрытым сделкам: сколько бумага получила
сверх рыночного входа, в процентах и в USDT, и каким был бы результат без неё.
Ничего не меняет — только измеряет, чтобы разрыв был виден ДО включения live.

Сделки, уже исполненные в live (`plan_json.execution.mode == "live"`), в форе не
участвуют: у них в учёте стоит реальная цена филла.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from models.signal import Signal

MARKET = "market"
# Сделки старше зоны входа плана не несут вовсе. Записывать их в «рыночные»
# значило бы приписать им способ входа, которого тогда ещё не существовало, и
# разбавить бакет, по которому потом сравнивают режимы.
UNKNOWN = "unknown"


def _f(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _entry_price(signal: Signal, plan: dict) -> float | None:
    lifecycle = plan.get("lifecycle") or {}
    price = _f(lifecycle.get("entry_price"))
    if price:
        return price
    zone = signal.entry_zone_json or {}
    low, high = _f(zone.get("from")), _f(zone.get("to"))
    if low and high:
        return (low + high) / 2.0
    return low or high


def entry_mode(plan: dict) -> str:
    """Способ входа сделки: market, limit_wall, limit_vwap или unknown."""
    zone_plan = plan.get("entry_zone_plan")
    if not isinstance(zone_plan, dict) or not zone_plan.get("mode"):
        return UNKNOWN
    return str(zone_plan["mode"])


def entry_notional_usdt(signal: Signal, plan: dict) -> float:
    """Номинал сделки — база, к которой относятся и фора, и стоимость оборота."""
    return (_entry_price(signal, plan) or 0.0) * (_f(signal.qty) or 0.0)


def entry_edge_usdt(signal: Signal, plan: dict) -> float:
    """Фора перенесённого входа в USDT — одна формула на все отчёты.

    Возникает только там, где бумага взяла цену лучше рынка: рыночный вход
    берёт то, что есть, а сделка, уже исполненная в live, записана по факту
    филла.
    """
    mode = entry_mode(plan)
    drift = _f((plan.get("entry_zone_plan") or {}).get("drift_pct")) or 0.0
    if mode in (MARKET, UNKNOWN) or drift <= 0:
        return 0.0
    if str((plan.get("execution") or {}).get("mode") or "") == "live":
        return 0.0
    return entry_notional_usdt(signal, plan) * drift / 100.0


def _round_trip_pct(plan: dict) -> float | None:
    market = ((plan.get("config") or {}).get("market") or {})
    return _f(market.get("round_trip_pct"))


def _bucket() -> dict[str, Any]:
    return {"trades": 0, "drift_sum": 0.0, "edge_usdt": 0.0, "notional_sum": 0.0, "net_pnl_usdt": 0.0}


def _finish(bucket: dict[str, Any]) -> dict[str, Any]:
    trades = bucket["trades"]
    if not trades:
        return {"trades": 0}
    return {
        "trades": trades,
        "avg_drift_pct": round(bucket["drift_sum"] / trades, 4),
        "edge_usdt": round(bucket["edge_usdt"], 4),
        "edge_per_trade_usdt": round(bucket["edge_usdt"] / trades, 4),
        "avg_notional_usdt": round(bucket["notional_sum"] / trades, 2),
        "net_pnl_usdt": round(bucket["net_pnl_usdt"], 4),
    }


def report(db, limit: int = 500, window_hours: float | None = None) -> dict[str, Any]:
    limit = min(max(int(limit or 500), 20), 5000)
    signals = (
        db.query(Signal)
        .filter(Signal.status == "closed")
        .order_by(Signal.id.desc())
        .limit(limit)
        .all()
    )
    if window_hours is not None and float(window_hours) > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))
        signals = [
            s for s in signals
            if s.closed_at is not None
            and (s.closed_at if s.closed_at.tzinfo else s.closed_at.replace(tzinfo=timezone.utc)) >= cutoff
        ]

    overall = _bucket()
    by_mode: dict[str, dict[str, Any]] = {}
    live_trades = 0
    round_trips: list[float] = []

    for signal in signals:
        plan = signal.plan_json or {}
        zone_plan = plan.get("entry_zone_plan") or {}
        mode = entry_mode(plan)
        drift = _f(zone_plan.get("drift_pct")) or 0.0
        executed_live = str((plan.get("execution") or {}).get("mode") or "") == "live"
        notional = entry_notional_usdt(signal, plan)
        net_pnl = _f(signal.closed_net_pnl) or 0.0

        edge = entry_edge_usdt(signal, plan)
        if executed_live:
            live_trades += 1

        rt = _round_trip_pct(plan)
        if rt is not None:
            round_trips.append(rt)

        for bucket in (overall, by_mode.setdefault(mode, _bucket())):
            bucket["trades"] += 1
            bucket["drift_sum"] += drift if mode not in (MARKET, UNKNOWN) else 0.0
            bucket["edge_usdt"] += edge
            bucket["notional_sum"] += notional
            bucket["net_pnl_usdt"] += net_pnl

    trades = overall["trades"]
    limit_trades = sum(b["trades"] for m, b in by_mode.items() if m not in (MARKET, UNKNOWN))
    edge_usdt = overall["edge_usdt"]
    net_pnl = overall["net_pnl_usdt"]
    avg_round_trip = round(sum(round_trips) / len(round_trips), 4) if round_trips else None
    # Фора на сделку в процентах — то, что сравнимо с round_trip_pct: обе цифры
    # про долю номинала, и владельцу важно именно это сопоставление.
    avg_drift_all = round(overall["drift_sum"] / trades, 4) if trades else None

    result: dict[str, Any] = {
        "status": "ok",
        "sample_count": trades,
        "window_hours": window_hours,
        "overall": {
            **_finish(overall),
            "limit_trades": limit_trades,
            "limit_share_pct": round(limit_trades / trades * 100, 2) if trades else 0.0,
            "market_trades": by_mode.get(MARKET, {}).get("trades", 0),
            "unknown_trades": by_mode.get(UNKNOWN, {}).get("trades", 0),
            "live_trades": live_trades,
            "avg_drift_pct_all_trades": avg_drift_all,
            "avg_round_trip_pct": avg_round_trip,
            "net_pnl_usdt": round(net_pnl, 4),
            "net_pnl_without_edge_usdt": round(net_pnl - edge_usdt, 4),
        },
        "by_mode": {mode: _finish(bucket) for mode, bucket in sorted(by_mode.items())},
    }

    note = ("Бумага книжит перенесённый вход по цене лучше рынка и всегда считает его "
            "исполненным. Live шлёт рыночный ордер и пишет фактический филл, поэтому "
            "этой форы там не будет. Мейкерской ставкой её не вернуть — вход должен "
            "стать настоящим лимитным ордером.")
    if avg_drift_all is not None and avg_round_trip:
        note += (f" Фора {avg_drift_all:.3f}% от номинала на сделку против "
                 f"round-trip {avg_round_trip:.3f}% — сопоставимые величины.")
    result["note"] = note
    return result
