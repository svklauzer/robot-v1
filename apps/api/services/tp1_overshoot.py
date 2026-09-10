"""Что происходит с позицией ПОСЛЕ TP1 (#tp1-overshoot-2026-09-10).

Вопрос владельца перед постройкой гейта на TP1: как часто сделка перешагивает
TP1, уходит дальше в плюс — и потом отдаёт. Гейт на TP1 имеет смысл только если
понятно, что система делает с ценой после него: если перешагнувшие TP1 сделки
регулярно сползают в безубыток, то TP1 гарантирует лишь половину позиции, а
вторая половина едет «бесплатно» туда и обратно.

Отчёт идёт по ТРАЕКТОРИИ (`lifecycle.traj`), а не по итоговым полям: только
траектория показывает, что цена сделала между TP1 и закрытием.

Три части:

1. Перешагнули ли TP1 и насколько далеко ушли — корзины по отношению
   MFE / дистанция до TP1.
2. Сколько отдали: где закрылся остаток относительно TP1 и безубытка, какую долю
   хода ЗА TP1 вернули рынку.
3. Трейл после TP1 (`post_tp1_giveback_trail`): взводится при MFE ≥ 0.6% и
   откате ≥ 0.4·MFE. Отчёт проигрывает это правило по траектории и отмечает
   сделки, где условие наступило, а закрылись они ИНАЧЕ — поздним безубытком.
   Такие случаи — не рыночный риск, а неотработавшая ветка выхода.

Цены выхода честные: выход не может быть лучше пика траектории (ветка
tp2_reached книжит цену TP2, закрываясь на 92% пути — см. phantom-fill).

Ничего не блокирует и не меняет — только показания.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal

# Корзины перешагивания: отношение пика к дистанции TP1. 1.0 — ровно дотянули.
_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("1.00–1.25", 1.00, 1.25),
    ("1.25–1.50", 1.25, 1.50),
    ("1.50–2.00", 1.50, 2.00),
    ("2.00+", 2.00, float("inf")),
)

# Около безубытка остаток «вернулся в ноль». Порог — круг издержек с запасом,
# а не ноль: закрытие на +0.05% экономически то же, что на нуле.
_BE_EPS_PCT = 0.20


def _num(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _side_pct(side: str, entry: float, price: float) -> float:
    raw = (price - entry) / entry * 100.0
    return raw if str(side).lower() == "long" else -raw


def _entry_price(signal: Signal, lifecycle: dict) -> float | None:
    entry = _num(lifecycle.get("entry_price"))
    if entry and entry > 0:
        return entry
    zone = signal.entry_zone_json or {}
    lo, hi = _num(zone.get("from")), _num(zone.get("to"))
    if lo and hi:
        return (lo + hi) / 2.0
    return None


def _tp1_dist_pct(signal: Signal, entry: float) -> float | None:
    tp1 = _num((signal.tp_json or {}).get("tp1"))
    if not tp1 or entry <= 0:
        return None
    dist = _side_pct(signal.side, entry, tp1)
    return dist if dist > 0 else None


def _cost_pct(signal: Signal, entry: float) -> float:
    """Круг издержек в % номинала. Нет данных — типичный круг swap."""
    cost = _num(signal.closed_total_cost)
    qty = _num(signal.qty) or _num((signal.plan_json or {}).get("qty"))
    if cost and qty and entry > 0:
        return cost / (qty * entry) * 100.0
    return 0.14


def _remaining_share(plan: dict) -> float:
    """Доля позиции, которая пережила TP1. Без частичной фиксации — вся."""
    partial = plan.get("tp1_partial") or {}
    closed, rest = _num(partial.get("closed_qty")), _num(partial.get("remaining_qty"))
    if closed and rest is not None and (closed + rest) > 0:
        return rest / (closed + rest)
    return 1.0


def _trail_trigger(traj: list, tp1_dist: float, *, min_mfe: float,
                   share: float) -> tuple[float | None, float | None]:
    """Проигрывает post_tp1_giveback_trail по траектории.

    Возвращает (цена срабатывания в %, пик на момент срабатывания) — первый
    момент ПОСЛЕ пересечения TP1, когда пик ≥ min_mfe и откат от пика ≥
    share·пик. Цена срабатывания — рынок в этот момент, то есть там, где
    реальный стоп-ордер и исполнился бы.
    """
    crossed = False
    peak = 0.0
    for point in traj or []:
        try:
            pct = float(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        peak = max(peak, pct)
        if not crossed:
            crossed = pct >= tp1_dist
            continue
        if peak >= min_mfe and (peak - pct) >= peak * share:
            return pct, peak
    return None, None


def _retests_tp1(traj: list, tp1_dist: float) -> bool:
    """Вернулась ли цена ниже TP1 после того, как его пересекла.

    Нужна альтернативе «стоп остатка на TP1»: такой стоп закрывает остаток на
    ПЕРВОМ возврате к TP1, даже если потом цена ушла к TP2. Без этого
    альтернатива приписывает себе и защиту от отката, и весь последующий ход —
    то есть выигрывает у факта по построению.
    """
    crossed = False
    for point in traj or []:
        try:
            pct = float(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not crossed:
            crossed = pct >= tp1_dist
            continue
        if pct < tp1_dist:
            return True
    return False


def _trail_gate(plan: dict, entry: float, trig_pct: float,
                full_qty: float | None = None) -> dict:
    """Экономический гейт трейла после TP1, воспроизведённый по снимку сделки.

    (#tp1-trail-gate-2026-09-10) Ветка `post_tp1_giveback_trail` закрывает
    остаток только если `_estimated_net_usdt` ≥ MIN_PROTECTIVE_NET_USDT. Но нетто
    считается НЕ по рынку в момент срабатывания, а по уровню
    `min(net_safe_pct, рынок)` — то есть по полу издержек (0.30% swap / 0.60%
    спот), и на номинале ОСТАТКА после частичной фиксации (position.qty там уже
    уменьшен). При таком счёте нетто почти равно `номинал × 0.0018`, и остаток
    меньше ~140 USDT не проходит никогда — хотя по рынку выход дал бы в разы
    больше. Отчёт показывает обе цифры: чем гейт мерил и что было на самом деле.

    Нет ставки или порогов в снимке — поля None: угадывать гейт по сегодняшним
    настройкам значит повторить ошибку, исправленную выше для самого трейла.
    """
    out = {"remaining_notional_usdt": None, "gate_booked_pct": None,
           "gate_net_usdt": None, "market_net_usdt": None, "gate_pass": None}
    cfg = plan.get("config") or {}
    market, cfg_exit = cfg.get("market") or {}, cfg.get("exit") or {}
    fee = _num(market.get("taker_fee"))
    slip = _num(market.get("slippage_buffer_pct"))
    floor = _num(cfg_exit.get("net_safe_floor_pct"))
    min_net = _num(cfg_exit.get("min_protective_net_usdt"))
    # Без частичной фиксации position.qty не уменьшался, и гейт видел ВСЮ
    # позицию — так же считает `_position_notional_usdt` в ведении.
    partial = plan.get("tp1_partial") or {}
    rest_qty = _num(partial.get("remaining_qty")) if partial else _num(full_qty)
    out["notional_source"] = "remainder_after_partial" if partial else "full_position"
    if None in (fee, slip, floor, min_net, rest_qty) or rest_qty <= 0:
        return out

    notional = rest_qty * entry
    net_safe = max(fee * 2 * 100 + slip * 100 + 0.15, floor)
    booked = min(net_safe, trig_pct)
    drag = fee * 2 + slip
    gate_net = notional * (booked / 100.0 - drag)
    out.update({
        "remaining_notional_usdt": round(notional, 2),
        "gate_booked_pct": round(booked, 4),
        "gate_net_usdt": round(gate_net, 4),
        "market_net_usdt": round(notional * (trig_pct / 100.0 - drag), 4),
        "gate_pass": gate_net >= min_net,
    })
    return out


def _analyse(signal: Signal) -> dict | None:
    plan = signal.plan_json or {}
    lifecycle = plan.get("lifecycle") or {}
    mfe = _num(lifecycle.get("mfe_pct"))
    entry = _entry_price(signal, lifecycle)
    if mfe is None or not entry:
        return None
    tp1_dist = _tp1_dist_pct(signal, entry)
    if tp1_dist is None:
        return None

    exit_price = _num(signal.closed_exit_price)
    exit_pct = _side_pct(signal.side, entry, exit_price) if exit_price else None
    # Честный выход не выше пика: tp2_reached книжит цену TP2, закрываясь раньше.
    honest_exit = min(exit_pct, mfe) if exit_pct is not None else None

    row = {
        "id": signal.id,
        "symbol": signal.symbol,
        "side": signal.side,
        "reason": str(signal.closed_reason or "unknown"),
        "tp1_dist_pct": round(tp1_dist, 4),
        "mfe_pct": round(mfe, 4),
        "exit_pct": round(honest_exit, 4) if honest_exit is not None else None,
        "result_pct": _num(signal.result_pct),
        "net_usdt": _num(signal.closed_net_pnl),
        "partial": bool(plan.get("tp1_partial")),
        "remaining_share": round(_remaining_share(plan), 4),
        "cost_pct": round(_cost_pct(signal, entry), 4),
        "reached_tp1": mfe >= tp1_dist,
        "ratio": round(mfe / tp1_dist, 4) if tp1_dist > 0 else None,
    }

    if not row["reached_tp1"]:
        return row

    traj = lifecycle.get("traj") or []

    # (#tp1-overshoot-2026-09-10) Правило берётся из СНИМКА КОНФИГА самой
    # сделки, а не из текущих настроек. Трейл после TP1 появился 03.09 вечером:
    # проверять его на сделках, для которых его ещё не существовало, значило бы
    # насчитать «неотработавшую ветку» там, где ветки не было. Так и вышло в
    # первой версии отчёта — четыре из шести «пропусков» в ленте закрылись до
    # 03.09.
    cfg_exit = ((plan.get("config") or {}).get("exit") or {})
    trail_in_force = cfg_exit.get("post_tp1_trail_enabled") is True
    min_mfe = _num(cfg_exit.get("post_tp1_trail_min_mfe_pct"))
    share = _num(cfg_exit.get("post_tp1_trail_giveback_share"))
    if min_mfe is None:
        min_mfe = float(getattr(settings, "POST_TP1_TRAIL_MIN_MFE_PCT", 0.60))
    if share is None:
        share = float(getattr(settings, "POST_TP1_TRAIL_GIVEBACK_SHARE", 0.40))

    trig_pct, trig_peak = _trail_trigger(traj, tp1_dist, min_mfe=min_mfe, share=share)
    row["trail_in_force"] = trail_in_force
    row["trail_trigger_pct"] = round(trig_pct, 4) if trig_pct is not None else None
    row["trail_trigger_peak"] = round(trig_peak, 4) if trig_peak is not None else None
    row["trail_fired"] = row["reason"] == "post_tp1_giveback_trail"
    # Условие наступило, правило было в силе, а закрылась сделка иначе — и хуже,
    # чем дал бы трейл. Закрытия ЗА TP2 сюда не относятся: там своя лестница.
    row["trail_missed"] = bool(
        trail_in_force and trig_pct is not None and not row["trail_fired"]
        and not row["reason"].startswith("tp2")
        and honest_exit is not None and honest_exit < trig_pct
    )

    if trig_pct is not None:
        row["trail_gate"] = _trail_gate(plan, entry, trig_pct,
                                        full_qty=_num(signal.qty) or _num(plan.get("qty")))
    row["tp1_retest"] = _retests_tp1(traj, tp1_dist)
    # Частичная фиксация была включена в момент входа, а записи о ней нет —
    # значит, на TP1 она не исполнилась, и TP1 ничего не зафиксировал.
    row["partial_expected"] = cfg_exit.get("tp1_partial_enabled") is True

    beyond = mfe - tp1_dist
    row["beyond_tp1_pct"] = round(beyond, 4)
    if honest_exit is not None:
        row["exit_below_tp1"] = honest_exit < tp1_dist
        row["exit_near_breakeven"] = honest_exit <= _BE_EPS_PCT
        row["gave_back_of_excess"] = (
            round((mfe - honest_exit) / beyond, 4) if beyond > 1e-9 else None
        )
    return row


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _med(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(median(clean), 4) if clean else None


def _counterfactuals(rows: list[dict]) -> dict:
    """Альтернативы на ТЕХ ЖЕ сделках, дошедших до TP1, в % номинала за вычетом
    одного круга издержек. Сравнивать можно между собой: у всех вариантов вход
    оплачен один раз, доли выходов в сумме — единица.
    """
    actual, all_at_tp1, honest_trail, lock_tp1 = [], [], [], []
    for r in rows:
        if r.get("exit_pct") is None:
            continue
        cost = r["cost_pct"]
        tp1 = r["tp1_dist_pct"]
        booked = 1.0 - r["remaining_share"]      # доля, закрытая на TP1
        rest = r["remaining_share"]
        exit_ = r["exit_pct"]

        actual.append(booked * tp1 + rest * exit_ - cost)
        all_at_tp1.append(tp1 - cost)

        # (#tp1-overshoot-cf-2026-09-11) Первое событие решает. Трейл,
        # сработавший на откате, закрыл бы остаток ТАМ — даже если потом цена
        # ушла к TP2. Первая версия брала лучшее из двух (max(trig, exit)) и
        # приписывала трейлу и защиту от отката, и весь последующий ход: на 60
        # сделках +18.2 п.п. остатка вместо честных +3.5.
        trig = r.get("trail_trigger_pct")
        rest_trail = trig if trig is not None else exit_
        honest_trail.append(booked * tp1 + rest * rest_trail - cost)

        # Стоп остатка на уровне TP1: закрывает на первом возврате к TP1.
        rest_lock = tp1 if r.get("tp1_retest") else max(exit_, tp1)
        lock_tp1.append(booked * tp1 + rest * rest_lock - cost)

    def _pack(values: list[float]) -> dict:
        return {
            "n": len(values),
            "sum_pct": round(sum(values), 4),
            "mean_pct": round(sum(values) / len(values), 4) if values else None,
        }

    return {
        "actual": _pack(actual),
        "all_out_at_tp1": _pack(all_at_tp1),
        "partial_then_trail_at_market": _pack(honest_trail),
        "partial_then_stop_at_tp1": _pack(lock_tp1),
        "note": (
            "Все варианты в % номинала за вычетом одного круга издержек, на одних "
            "и тех же сделках, дошедших до TP1. Первое событие по траектории "
            "решает: сработавшее правило закрывает остаток, что бы цена ни "
            "делала потом. partial_then_trail_at_market — трейл после TP1 по "
            "рынку в момент срабатывания; partial_then_stop_at_tp1 — стоп "
            "остатка на уровне TP1 вместо безубытка, на первом возврате к TP1 "
            "(исполнение ровно по TP1, без проскальзывания)."
        ),
    }


def build(db: Session, *, window_hours: float = 720.0, side: str | None = None,
          max_rows: int = 4000) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))
    query = (
        db.query(Signal)
        .filter(Signal.status == "closed", Signal.closed_at.isnot(None),
                Signal.closed_at >= cutoff)
    )
    if side in ("long", "short"):
        query = query.filter(Signal.side == side)
    signals = query.order_by(Signal.id.desc()).limit(int(max_rows)).all()

    rows = [r for r in (_analyse(s) for s in signals) if r is not None]
    reached = [r for r in rows if r["reached_tp1"]]
    missed = [r for r in rows if not r["reached_tp1"]]

    buckets = []
    for label, lo, hi in _BUCKETS:
        inside = [r for r in reached if lo <= (r["ratio"] or 0) < hi]
        buckets.append({
            "ratio": label,
            "n": len(inside),
            "share_of_reached": _share(len(inside), len(reached)),
            "exit_below_tp1": sum(1 for r in inside if r.get("exit_below_tp1")),
            "exit_near_breakeven": sum(1 for r in inside if r.get("exit_near_breakeven")),
            "median_gave_back_of_excess": _med([r.get("gave_back_of_excess") for r in inside]),
        })

    went_further = [r for r in reached if (r["ratio"] or 0) >= 1.25]
    below_tp1 = [r for r in reached if r.get("exit_below_tp1")]
    near_be = [r for r in reached if r.get("exit_near_breakeven")]
    net_loss = [r for r in reached if (r.get("result_pct") or 0) < 0]

    reasons: dict[str, int] = {}
    for r in reached:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1

    in_force = [r for r in reached if r.get("trail_in_force")]
    trail_eligible = [r for r in in_force if r.get("trail_trigger_pct") is not None]
    trail_fired = [r for r in in_force if r.get("trail_fired")]
    trail_missed = [r for r in in_force if r.get("trail_missed")]

    gates = [r.get("trail_gate") or {} for r in trail_missed]
    gate_blocked = [g for g in gates if g.get("gate_pass") is False]
    gate_passed = [g for g in gates if g.get("gate_pass") is True]

    near_miss = [r for r in missed if (r["ratio"] or 0) >= 0.80]

    partial_expected = [r for r in reached if r.get("partial_expected")]
    partial_missing = [r for r in partial_expected if not r["partial"]]

    return {
        "window_hours": window_hours,
        "side": side,
        "closed_analysed": len(rows),
        "reached_tp1": len(reached),
        "reached_rate": _share(len(reached), len(rows)),
        "reached_with_partial_fill": sum(1 for r in reached if r["partial"]),
        # Цена дошла, а частичной фиксации нет — расхождение двух признаков
        # само по себе диагноз (фиксация не сработала или ещё не существовала).
        "reached_without_partial_fill": sum(1 for r in reached if not r["partial"]),
        "near_miss_80pct": len(near_miss),
        # (#tp1-partial-health-2026-09-11) Фиксация на TP1 была включена при
        # входе, а в плане её нет: исполнение молча не случилось (ошибка
        # глотается в ведении). Такая сделка на TP1 только двигает стоп.
        "tp1_partial_health": {
            "expected": len(partial_expected),
            "missing": len(partial_missing),
            "missing_ids": [r["id"] for r in partial_missing][:30],
        },
        "overshoot": {
            "went_further_1_25x": len(went_further),
            "went_further_share": _share(len(went_further), len(reached)),
            "median_ratio": _med([r["ratio"] for r in reached]),
            "median_beyond_tp1_pct": _med([r.get("beyond_tp1_pct") for r in reached]),
            "buckets": buckets,
        },
        "giveback": {
            "exit_below_tp1": len(below_tp1),
            "exit_below_tp1_share": _share(len(below_tp1), len(reached)),
            "exit_near_breakeven": len(near_be),
            "exit_near_breakeven_share": _share(len(near_be), len(reached)),
            "ended_net_loss": len(net_loss),
            "median_gave_back_of_excess": _med([r.get("gave_back_of_excess") for r in reached]),
            "close_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        },
        "post_tp1_trail": {
            "min_mfe_pct": float(getattr(settings, "POST_TP1_TRAIL_MIN_MFE_PCT", 0.60)),
            "giveback_share": float(getattr(settings, "POST_TP1_TRAIL_GIVEBACK_SHARE", 0.40)),
            # Сколько дошедших до TP1 сделок вообще жили при включённом трейле.
            # Остальные закрыты до 03.09 и в пропуски не засчитываются.
            "in_force": len(in_force),
            "not_yet_in_force": len(reached) - len(in_force),
            "condition_met": len(trail_eligible),
            "fired": len(trail_fired),
            "missed": len(trail_missed),
            "missed_ids": [r["id"] for r in trail_missed][:30],
            "median_trigger_pct": _med([r["trail_trigger_pct"] for r in trail_missed]),
            "median_actual_exit_pct": _med([r["exit_pct"] for r in trail_missed]),
            # Почему пропуск случился. blocked_by_net_gate — гейт нетто считал
            # выход по полу издержек на остатке и не пропустил; would_pass —
            # гейт пропускал, значит причина в другом (порядок веток, тики).
            "missed_blocked_by_net_gate": len(gate_blocked),
            "missed_gate_would_pass": len(gate_passed),
            "missed_gate_unknown": len(gates) - len(gate_blocked) - len(gate_passed),
            "median_missed_remaining_notional_usdt": _med(
                [g.get("remaining_notional_usdt") for g in gates]),
            "median_missed_gate_net_usdt": _med([g.get("gate_net_usdt") for g in gates]),
            "median_missed_market_net_usdt": _med([g.get("market_net_usdt") for g in gates]),
            "note": (
                "Правило берётся из снимка конфига каждой сделки. in_force — "
                "сделки, жившие при включённом трейле (с 03.09). condition_met — "
                "после TP1 наступило условие трейла (пик ≥ min_mfe, откат ≥ "
                "share·пик). missed — условие наступило, а сделка закрылась иначе "
                "и хуже: ветка выхода не отработала. Цена срабатывания — рынок в "
                "тот момент, где реальный стоп исполнился бы."
            ),
        },
        "counterfactuals": _counterfactuals(reached),
        "trades": reached[:60],
    }
