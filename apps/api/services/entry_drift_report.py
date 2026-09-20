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

from core.config import settings
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
    """Куда зона перенесла ЦЕЛЬ входа: market, limit_wall, limit_vwap, unknown.

    Это НЕ тип ордера. Имена режимов начинаются с `limit_`, потому что цель
    берётся из книги, но войти по ней можно и рыночным ордером — так и было
    все 27 сделок 19–20.09.
    """
    zone_plan = plan.get("entry_zone_plan")
    if not isinstance(zone_plan, dict) or not zone_plan.get("mode"):
        return UNKNOWN
    return str(zone_plan["mode"])


def entry_order_type(plan: dict) -> str:
    """Чем вход отправлялся: limit, market или unknown у сделок до 20.09."""
    zone_plan = plan.get("entry_zone_plan")
    if not isinstance(zone_plan, dict) or not zone_plan.get("order_type"):
        return UNKNOWN
    return str(zone_plan["order_type"]).lower()


def entered_by_limit(plan: dict) -> bool:
    """Лимитом вход идёт только когда И настройка limit, И зона цель перенесла:
    при `mode == market` цели нет, и ордер уходит рыночным при любой настройке."""
    return entry_order_type(plan) == "limit" and entry_mode(plan) not in (MARKET, UNKNOWN)


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


def _fill_rate(db, window_hours: float | None, limit: int) -> dict[str, Any]:
    """Доля сигналов, дошедших до сделки (#limit-entry-2026-09-19).

    Лимитный вход платит за лучшую цену тем, что исполняется не всегда: цена
    должна дойти до самой цели. Без этой доли переход на лимит нечем оценивать —
    выигрыш на цене может не покрыть потерянные сделки.
    """
    query = db.query(Signal).order_by(Signal.id.desc()).limit(limit)
    rows = query.all()
    if window_hours is not None and float(window_hours) > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))
        rows = [
            s for s in rows
            if s.created_at is not None
            and (s.created_at if s.created_at.tzinfo else s.created_at.replace(tzinfo=timezone.utc)) >= cutoff
        ]

    published = len(rows)
    reached = sum(1 for s in rows if str(s.status) not in ("published", "expired", "rejected"))
    unfilled_limit = sum(1 for s in rows
                         if str(s.status) == "expired" and str(s.closed_reason or "") == "limit_not_filled")
    expired_other = sum(1 for s in rows if str(s.status) == "expired") - unfilled_limit
    return {
        "signals": published,
        "reached_entry": reached,
        "fill_rate_pct": round(reached / published * 100, 2) if published else None,
        "limit_not_filled": unfilled_limit,
        "expired_other": expired_other,
        "still_waiting": sum(1 for s in rows if str(s.status) == "published"),
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
    by_order: dict[str, int] = {}
    entered_limit = 0
    round_trips: list[float] = []

    for signal in signals:
        plan = signal.plan_json or {}
        mode = entry_mode(plan)
        order = entry_order_type(plan)
        by_order[order] = by_order.get(order, 0) + 1
        entered_limit += int(entered_by_limit(plan))
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
    # Зона перенесла цель — это ещё не лимитный вход (см. entry_mode).
    zone_moved_trades = sum(b["trades"] for m, b in by_mode.items() if m not in (MARKET, UNKNOWN))
    avg_round_trip = round(sum(round_trips) / len(round_trips), 4) if round_trips else None
    maker_saving_pct = None
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
            # Цель перенесена (режим зоны) — и ОТДЕЛЬНО чем по ней входили.
            # Раньше здесь стояло одно число под именем limit_trades, и 27
            # рыночных входов читались как лимитные.
            "zone_moved_trades": zone_moved_trades,
            "zone_moved_share_pct": round(zone_moved_trades / trades * 100, 2) if trades else 0.0,
            "limit_order_trades": by_order.get("limit", 0),
            "market_order_trades": by_order.get(MARKET, 0),
            "order_type_unknown_trades": by_order.get(UNKNOWN, 0),
            "entered_by_limit_trades": entered_limit,
            "zone_market_trades": by_mode.get(MARKET, {}).get("trades", 0),
            "zone_unknown_trades": by_mode.get(UNKNOWN, {}).get("trades", 0),
            "avg_round_trip_pct": avg_round_trip,
            "maker_saving_pct": maker_saving_pct,
        },
        "by_mode": {mode: _finish(bucket) for mode, bucket in sorted(by_mode.items())},
        "fill_rate": _fill_rate(db, window_hours, limit),
        "entry_order_type": str(getattr(settings, "ENTRY_ORDER_TYPE", "market")),
    }

    note = ("Сигнал ждёт, пока цена сама придёт в коридор зоны, и открывается по ней "
            "же — и в бумаге, и в live. Цена входит в коридор с одной стороны, поэтому "
            "факт входа оказывается у дальней от цели границы: это и есть выигрыш, "
            "который забрал бы лимитный ордер по цене цели, если бы исполнился.")
    if finished.get("entry_vs_target_pct") is not None and maker_saving_pct:
        note += (f" Сейчас вход {finished['entry_vs_target_pct']:+.3f}% к цели; "
                 f"лимит добавил бы к этому мейкерскую ставку "
                 f"(−{maker_saving_pct:.3f}% от номинала на входе).")
    # (#sync-reverted-the-entry-type-2026-09-20) Настройка «limit» и вход
    # лимитом — разные вещи, и 19–20.09 они разошлись почти на двое суток:
    # sync blueprint вернул ENTRY_ORDER_TYPE к market, а отчёт продолжал
    # показывать 85% «лимитных» сделок (это был режим ЗОНЫ). Расхождение
    # ловим здесь, иначе заметить его нечем.
    unknown_order = by_order.get(UNKNOWN, 0)
    if result["entry_order_type"] == "limit" and trades and unknown_order == trades:
        # Признак пишется в план при СОЗДАНИИ сигнала, поэтому у всего, что было
        # запланировано до деплоя, его нет. Это не «лимит не работает» — это
        # «судить не по чему», и путать одно с другим значит чинить исправное.
        result["warning"] = (
            f"Все {trades} сделок закрыты без признака типа входа: они планировались "
            f"до деплоя, в котором признак появился. Лимит проверяется только по "
            f"сделкам, СОЗДАННЫМ после него.")
    elif result["entry_order_type"] == "limit" and trades and not entered_limit:
        result["warning"] = (
            f"ENTRY_ORDER_TYPE=limit, но ни одна из {trades - unknown_order} сделок с "
            f"известным типом входа не вошла лимитом. Либо настройка не доехала до "
            f"процесса (sync blueprint возвращает ключи с value: к записанному), "
            f"либо зона не переносит вход.")
    elif result["entry_order_type"] != "limit" and zone_moved_trades:
        result["warning"] = (
            f"ENTRY_ORDER_TYPE={result['entry_order_type']}: {zone_moved_trades} сделок "
            f"имеют перенесённую цель зоны, но входят по рынку. Дрейф к цели ниже — "
            f"это цена рыночного входа, а не результат лимита.")
    result["note"] = note
    return result
