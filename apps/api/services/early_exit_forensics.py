"""Почему «безубыток» теряет деньги (#be-lock-leak-2026-09-12).

Деньги по причинам закрытия за 500 сделок (12.09): `breakeven_lock` — 46 сделок,
−30.25 USDT, −0.66 на сделку. Замок обязан закрываться около нуля: он
срабатывает после MFE ≥ arm, когда цена откатила к полу, а пол задан так,
чтобы покрыть круг издержек:

    floor = max(BREAKEVEN_LOCK_FLOOR_PCT, круг издержек + буфер)

При выключенном подтверждении потоком (EXIT_REQUIRE_FLOW_CONFIRM=false) выход
идёт по рынку на первом тике ниже пола. Минус в среднем значит одно из двух:

  * тик выхода проваливается далеко под пол (быстрый откат между проверками);
  * пол посчитан по неверной комиссии — например, по своповой для спотовой
    сделки: спот платит круг ~0.44%, а пол 0.18% его не покрывает.

Отчёт раскладывает сделки с заданной причиной закрытия по рынку (спот/своп) и
для каждой считает ожидаемый пол по её же комиссии и насколько ниже пола
случился выход. Первая гипотеза даёт большой `below_floor` на обоих рынках,
вторая — отрицательный нетто при выходе у самого пола и только на споте.

Ничего не меняет — только показания.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean, median

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal
from services.stop_loss_forensics import _honest_net
from services.tp1_overshoot import _actual_cost_pct, _entry_price, _num


def _side_pct(side: str, entry: float, price: float) -> float:
    raw = (price - entry) / entry * 100.0
    return raw if str(side).lower() == "long" else -raw


def _market_type(plan: dict) -> str:
    routing = plan.get("routing") or {}
    market = (plan.get("config") or {}).get("market") or {}
    return str(routing.get("market_type") or market.get("market_type") or "unknown")


def _row(signal: Signal) -> dict | None:
    plan = signal.plan_json or {}
    lifecycle = plan.get("lifecycle") or {}
    entry = _entry_price(signal, lifecycle)
    exit_price = _num(signal.closed_exit_price)
    qty = _num(signal.qty) or _num(plan.get("qty"))
    if not entry or not exit_price or not qty:
        return None
    notional = qty * entry
    cfg = plan.get("config") or {}
    market, cfg_exit = cfg.get("market") or {}, cfg.get("exit") or {}

    slip = _num(market.get("slippage_buffer_pct"))
    if slip is None:
        slip = float(getattr(settings, "SLIPPAGE_BUFFER_PCT", 0.0002))
    cost = _actual_cost_pct(signal, entry)
    fee = _num(market.get("taker_fee"))
    fee_source = "snapshot"
    if fee is None and cost is not None:
        fee = max((cost / 100.0 - slip) / 2.0, 0.0)
        fee_source = "derived_from_trade_cost"

    floor_cfg = _num(cfg_exit.get("breakeven_lock_floor_pct"))
    if floor_cfg is None:
        floor_cfg = float(getattr(settings, "BREAKEVEN_LOCK_FLOOR_PCT", 0.18))
    buffer = _num(cfg_exit.get("breakeven_lock_cost_buffer_pct"))
    if buffer is None:
        buffer = float(getattr(settings, "BREAKEVEN_LOCK_COST_BUFFER_PCT", 0.05))
    floor = (max(floor_cfg, (fee * 2 + slip) * 100 + buffer)
             if fee is not None else None)

    gross = _side_pct(signal.side, entry, exit_price)
    return {
        "id": signal.id,
        "symbol": signal.symbol,
        "side": signal.side,
        "market_type": _market_type(plan),
        "mfe_pct": _num(lifecycle.get("mfe_pct")),
        "exit_gross_pct": round(gross, 4),
        "net_pct": round(_honest_net(signal) / notional * 100.0, 4),
        "net_usdt": round(_honest_net(signal), 4),
        "cost_pct": round(cost, 4) if cost is not None else None,
        "fee_rate": round(fee, 6) if fee is not None else None,
        "fee_source": fee_source,
        "expected_floor_pct": round(floor, 4) if floor is not None else None,
        # Насколько ниже пола случился выход: 0 — ровно у пола.
        "below_floor_pct": round(floor - gross, 4) if floor is not None else None,
    }


def _summary(rows: list[dict]) -> dict:
    def _m(key, fn=mean):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(fn(vals), 4) if vals else None

    return {
        "n": len(rows),
        "net_usdt": round(sum(r["net_usdt"] for r in rows), 2),
        "mean_net_pct": _m("net_pct"),
        "mean_exit_gross_pct": _m("exit_gross_pct"),
        "mean_cost_pct": _m("cost_pct"),
        "mean_expected_floor_pct": _m("expected_floor_pct"),
        "median_below_floor_pct": _m("below_floor_pct", median),
        "mean_below_floor_pct": _m("below_floor_pct"),
        "net_negative": sum(1 for r in rows if r["net_pct"] < 0),
    }


def build(db: Session, *, reason: str = "breakeven_lock", window_hours: float = 2160.0,
          max_rows: int = 4000) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))
    signals = (
        db.query(Signal)
        .filter(Signal.status == "closed", Signal.closed_at.isnot(None),
                Signal.closed_at >= cutoff, Signal.closed_reason == reason)
        .order_by(Signal.id.desc())
        .limit(int(max_rows))
        .all()
    )
    rows, skipped = [], 0
    for s in signals:
        row = _row(s)
        if row is None:
            skipped += 1
            continue
        rows.append(row)

    by_market: dict[str, list[dict]] = {}
    for r in rows:
        by_market.setdefault(r["market_type"], []).append(r)

    return {
        "reason": reason,
        "window_hours": float(window_hours),
        "analysed": len(rows),
        "skipped_no_data": skipped,
        "all": _summary(rows),
        "by_market_type": {k: _summary(v) for k, v in sorted(by_market.items())},
        "trades": rows[:60],
        "note": (
            "expected_floor_pct — пол замка по комиссии самой сделки: "
            "max(BREAKEVEN_LOCK_FLOOR_PCT, круг издержек + буфер). "
            "below_floor_pct — насколько ниже пола случился выход. Большой "
            "below_floor на обоих рынках — тик проваливается между проверками; "
            "минус у самого пола только на споте — пол не покрывает круг "
            "издержек рынка сделки."
        ),
    }
