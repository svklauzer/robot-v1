"""Цена входа: куда целились и где вошли (#entry-drift-2026-09-19).

ИСПРАВЛЕНО 19.09 после проверки по коду. Первая версия отчёта считала, что
бумага книжит вход по цене зоны и получает «фору», которой не будет в live. Это
неверно: `signal_lifecycle` держит сигнал в статусе `published`, ждёт, пока
ТЕКУЩАЯ цена попадёт в коридор зоны (`_price_in_entry_zone`), и открывает
позицию по ней же. Live в тот же момент шлёт рыночный ордер. Никакого
систематического подарка бумаге нет, и вычитать из результата нечего.

Что есть на самом деле: цена входит в коридор с одной стороны — лонг падает к
нему сверху, шорт поднимается снизу. Поэтому факт входа систематически
оказывается у ДАЛЬНЕЙ от цели границы зоны, то есть хуже самой цели. Замер по
сигналам 602–613: вход хуже цели на 0.06–0.10%.

Отсюда и смысл отчёта: показать, что даст переход на лимитный ордер по цене
цели. Две величины, обе по фактам плана:

  • `vs_target_pct` — насколько факт входа хуже (или лучше) цели зоны. Это и
    есть выигрыш, который забрал бы лимит, если бы исполнился;
  • `vs_mid_pct` — насколько факт входа лучше рынка на момент планирования.
    Это то, что ожидание коридора уже даёт, и оно достаётся и бумаге, и live.

Ничего не меняет — только измеряет.
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
    """Цена, по которой сделка ФАКТИЧЕСКИ открылась."""
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
    """Номинал сделки — база, к которой относятся и выигрыш, и стоимость оборота."""
    return (_entry_price(signal, plan) or 0.0) * (_f(signal.qty) or 0.0)


def _better_by_pct(side: str, reference: float | None, actual: float | None) -> float | None:
    """На сколько процентов `actual` выгоднее `reference` для этой стороны.

    Лонгу выгоднее купить дешевле, шорту — продать дороже. Знак одинаково
    означает «в нашу пользу», иначе строки лонгов и шортов нельзя складывать.
    """
    if not reference or not actual or reference <= 0:
        return None
    gain = (reference - actual) if str(side or "").lower() == "long" else (actual - reference)
    return gain / reference * 100.0


def entry_vs_target_pct(signal: Signal, plan: dict) -> float | None:
    """Факт входа против цели зоны. Отрицательное — вошли хуже цели."""
    target = _f((plan.get("entry_zone_plan") or {}).get("entry_price"))
    return _better_by_pct(signal.side, target, _entry_price(signal, plan))


def entry_vs_mid_pct(signal: Signal, plan: dict) -> float | None:
    """Факт входа против рынка на момент планирования. Плюс — ожидание помогло."""
    mid = _f((plan.get("entry_depth") or {}).get("mid"))
    return _better_by_pct(signal.side, mid, _entry_price(signal, plan))


def _round_trip_pct(plan: dict) -> float | None:
    market = ((plan.get("config") or {}).get("market") or {})
    return _f(market.get("round_trip_pct"))


def _bucket() -> dict[str, Any]:
    return {"trades": 0, "notional_sum": 0.0, "net_pnl_usdt": 0.0,
            "vs_target": [], "vs_mid": [], "limit_gain_usdt": 0.0}


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _finish(bucket: dict[str, Any]) -> dict[str, Any]:
    trades = bucket["trades"]
    if not trades:
        return {"trades": 0}
    return {
        "trades": trades,
        "entry_vs_target_pct": _avg(bucket["vs_target"]),
        "entry_vs_mid_pct": _avg(bucket["vs_mid"]),
        "limit_gain_usdt": round(bucket["limit_gain_usdt"], 4),
        "limit_gain_per_trade_usdt": round(bucket["limit_gain_usdt"] / trades, 4),
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
    round_trips: list[float] = []

    for signal in signals:
        plan = signal.plan_json or {}
        mode = entry_mode(plan)
        notional = entry_notional_usdt(signal, plan)
        net_pnl = _f(signal.closed_net_pnl) or 0.0
        vs_target = entry_vs_target_pct(signal, plan)
        vs_mid = entry_vs_mid_pct(signal, plan)

        # Выигрыш лимита: сколько принесла бы цена цели вместо фактической.
        # Отрицательный `vs_target` (вошли хуже цели) даёт положительный выигрыш.
        gain = (-vs_target / 100.0 * notional) if (vs_target is not None and notional) else 0.0

        rt = _round_trip_pct(plan)
        if rt is not None:
            round_trips.append(rt)

        for bucket in (overall, by_mode.setdefault(mode, _bucket())):
            bucket["trades"] += 1
            bucket["notional_sum"] += notional
            bucket["net_pnl_usdt"] += net_pnl
            bucket["limit_gain_usdt"] += gain
            if vs_target is not None:
                bucket["vs_target"].append(vs_target)
            if vs_mid is not None:
                bucket["vs_mid"].append(vs_mid)

    trades = overall["trades"]
    limit_trades = sum(b["trades"] for m, b in by_mode.items() if m not in (MARKET, UNKNOWN))
    avg_round_trip = round(sum(round_trips) / len(round_trips), 4) if round_trips else None
    maker_saving_pct = None
    from core.config import settings

    taker = _f(getattr(settings, "FUTURES_TAKER_FEE", None))
    maker = _f(getattr(settings, "FUTURES_MAKER_FEE", None))
    if taker is not None and maker is not None:
        maker_saving_pct = round((taker - maker) * 100, 4)

    finished = _finish(overall)
    result: dict[str, Any] = {
        "status": "ok",
        "sample_count": trades,
        "window_hours": window_hours,
        "overall": {
            **finished,
            "limit_trades": limit_trades,
            "limit_share_pct": round(limit_trades / trades * 100, 2) if trades else 0.0,
            "market_trades": by_mode.get(MARKET, {}).get("trades", 0),
            "unknown_trades": by_mode.get(UNKNOWN, {}).get("trades", 0),
            "avg_round_trip_pct": avg_round_trip,
            "maker_saving_pct": maker_saving_pct,
        },
        "by_mode": {mode: _finish(bucket) for mode, bucket in sorted(by_mode.items())},
    }

    note = ("Сигнал ждёт, пока цена сама придёт в коридор зоны, и открывается по ней "
            "же — и в бумаге, и в live. Цена входит в коридор с одной стороны, поэтому "
            "факт входа оказывается у дальней от цели границы: это и есть выигрыш, "
            "который забрал бы лимитный ордер по цене цели, если бы исполнился.")
    if finished.get("entry_vs_target_pct") is not None and maker_saving_pct:
        note += (f" Сейчас вход {finished['entry_vs_target_pct']:+.3f}% к цели; "
                 f"лимит добавил бы к этому мейкерскую ставку "
                 f"(−{maker_saving_pct:.3f}% от номинала на входе).")
    result["note"] = note
    return result
