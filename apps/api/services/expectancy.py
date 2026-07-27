"""Net expectancy как критерий отбора вместо win-rate (#expectancy-2026-07-27).

Почему win-rate — плохой критерий, на наших же числах. Боевые #264–282:

    win-rate 67%   (лучше, чем 65% у эталонного копи-трейдера)
    P/L ratio 0.11 (средняя победа +0.091%, средний убыток −0.822%)
    expectancy −0.251% на сделку

Символ с 70% побед по +0.05% и 30% убытков по −0.9% выглядит отличным по
win-rate и при этом систематически съедает депозит. Гвард судил именно по
win-rate (`winrate < block_max_winrate`) и такой символ пропускал.

Expectancy отвечает на единственный вопрос, который имеет значение: сколько
денег приносит ОДНА средняя сделка после всех издержек.

    expectancy = Σ net_pnl / n            (в USDT, издержки уже внутри)
    expectancy_pct = Σ result_pct / n     (в % хода, для сравнения символов
                                           с разным размером позиции)

`closed_net_pnl` уже содержит комиссии и проскальзывание (см. CostEngine),
поэтому expectancy в USDT — величина «после всех издержек» по построению.
Отдельно считается expectancy_gross_pct, чтобы видеть, какую долю съедают
издержки: расхождение gross и net — это цена оборота.

Разрезы: по символу, по причине входа, по паре символ×причина. Причина входа
пишется в `plan_json.entry_reason` (добавлено этой же правкой) — до неё все
трендовые сетапы лежали в одной куче и разделить их было нечем.

Только чтение. На торговлю напрямую не влияет — влияет гвард, который эти
числа использует.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal


def _plan(signal: Any) -> dict:
    try:
        return signal.plan_json or {}
    except Exception:  # noqa: BLE001
        return {}


def entry_reason_of(signal: Any) -> str:
    """Причина входа с честным фолбэком для старых записей.

    До #expectancy-2026-07-27 поле не писалось. Помечаем такие сделки явно —
    `legacy_<regime>` — вместо того чтобы валить их в «unknown» вперемешку с
    новыми: иначе разрез по причинам будет выглядеть заполненным, хотя данных
    в нём нет.
    """
    plan = _plan(signal)
    reason = str(plan.get("entry_reason") or "").strip()
    if reason and reason != "unknown":
        return reason
    regime = str(plan.get("regime") or plan.get("trade_mode") or "unknown")
    return f"legacy_{regime}"


def _row(signals: Iterable[Any]) -> dict[str, Any]:
    """Метрики одной группы сделок."""
    rows = list(signals)
    n = len(rows)
    if n == 0:
        return {"count": 0}

    net_total = 0.0
    gross_total = 0.0
    cost_total = 0.0
    wins = 0
    win_sum = 0.0
    loss_sum = 0.0
    ptn = 0
    with_money = 0

    for s in rows:
        result_pct = float(s.result_pct or 0.0)
        gross_total += result_pct
        if s.closed_net_pnl is not None:
            net = float(s.closed_net_pnl)
            net_total += net
            with_money += 1
            if net > 0:
                wins += 1
                win_sum += net
            else:
                loss_sum += net
        if s.closed_total_cost is not None:
            cost_total += float(s.closed_total_cost)
        if (_plan(s).get("lifecycle") or {}).get("positive_then_negative"):
            ptn += 1

    losses = with_money - wins
    avg_win = (win_sum / wins) if wins else 0.0
    avg_loss = (loss_sum / losses) if losses else 0.0

    return {
        "count": n,
        "count_with_money": with_money,
        # ГЛАВНОЕ ЧИСЛО: сколько приносит средняя сделка после издержек.
        "expectancy_usdt": round(net_total / with_money, 6) if with_money else None,
        "expectancy_gross_pct": round(gross_total / n, 4),
        "net_pnl_usdt": round(net_total, 6),
        "costs_usdt": round(cost_total, 6),
        # Доля результата, съеденная оборотом. Если издержки сопоставимы с
        # валовым результатом, проблема не в направлении, а в частоте.
        "cost_share_of_gross_pct": (
            round(cost_total / abs(net_total + cost_total) * 100, 1)
            if abs(net_total + cost_total) > 1e-9 else None
        ),
        "winrate_pct": round(wins / with_money * 100, 2) if with_money else None,
        "avg_win_usdt": round(avg_win, 6),
        "avg_loss_usdt": round(avg_loss, 6),
        # P/L ratio: средняя победа к среднему убытку. Win-rate без него
        # бессмыслен — 67% побед при P/L 0.11 это убыточная система.
        "payoff_ratio": round(avg_win / abs(avg_loss), 4) if avg_loss < 0 else None,
        "positive_then_negative": ptn,
        "positive_then_negative_pct": round(ptn / n * 100, 1),
    }


def _fetch(db: Session, *, bot_id: int | None, window_hours: float,
           limit: int) -> list[Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))
    q = (
        db.query(Signal)
        .filter(
            Signal.status == "closed",
            Signal.closed_at.isnot(None),
            Signal.closed_at >= cutoff,
        )
    )
    if bot_id is not None:
        q = q.filter(Signal.bot_id == bot_id)
    return q.order_by(Signal.id.desc()).limit(int(limit)).all()


def symbol_expectancy(
    db: Session,
    *,
    bot_id: int | None = None,
    window_hours: float | None = None,
    limit: int = 2000,
) -> dict[str, Any]:
    """Expectancy по символам, причинам входа и их парам."""
    window_hours = float(
        window_hours if window_hours is not None
        else getattr(settings, "EXPECTANCY_WINDOW_HOURS", 720.0)
    )
    signals = _fetch(db, bot_id=bot_id, window_hours=window_hours, limit=limit)
    if not signals:
        return {"status": "no_data", "window_hours": window_hours,
                "note": "нет закрытых сделок в окне"}

    by_symbol: dict[str, list] = {}
    by_reason: dict[str, list] = {}
    by_pair: dict[tuple[str, str], list] = {}
    for s in signals:
        reason = entry_reason_of(s)
        by_symbol.setdefault(s.symbol, []).append(s)
        by_reason.setdefault(reason, []).append(s)
        by_pair.setdefault((s.symbol, reason), []).append(s)

    min_n = int(getattr(settings, "EXPECTANCY_MIN_HISTORY", 12))
    ptn_max = float(getattr(settings, "EXPECTANCY_PTN_MAX_PCT", 40.0))

    def _verdict(row: dict) -> dict:
        """Вердикт по правилу «отрицательное ожидание + отдаём прибыль»."""
        exp = row.get("expectancy_usdt")
        n = row.get("count_with_money") or 0
        if exp is None or n < min_n:
            return {"verdict": "insufficient", "demote": False,
                    "why": f"нужно {min_n} закрытий с деньгами, есть {n}"}
        ptn = row.get("positive_then_negative_pct") or 0.0
        if exp < 0 and ptn >= ptn_max:
            return {"verdict": "demote", "demote": True,
                    "why": (f"ожидание {exp} USDT на сделку при {ptn}% "
                            "positive→negative: минус и есть отданная прибыль")}
        if exp < 0:
            return {"verdict": "negative", "demote": True,
                    "why": f"отрицательное ожидание {exp} USDT на сделку"}
        if ptn >= ptn_max:
            return {"verdict": "gives_back", "demote": False,
                    "why": (f"ожидание положительное, но {ptn}% сделок отдают "
                            "набранный плюс — тянуть должна exit-политика, не отбор")}
        return {"verdict": "ok", "demote": False, "why": ""}

    def _pack(groups: dict, key_name: str) -> list[dict]:
        items = []
        for key, rows in groups.items():
            row = _row(rows)
            if key_name == "pair":
                row["symbol"], row["entry_reason"] = key
            else:
                row[key_name] = key
            row.update(_verdict(row))
            items.append(row)
        items.sort(key=lambda r: (r.get("expectancy_usdt") is None,
                                 r.get("expectancy_usdt") or 0.0))
        return items

    overall = _row(signals)
    return {
        "status": "ok",
        "window_hours": window_hours,
        "sample": len(signals),
        "min_history": min_n,
        "ptn_max_pct": ptn_max,
        "overall": overall,
        "by_symbol": _pack(by_symbol, "symbol"),
        "by_entry_reason": _pack(by_reason, "entry_reason"),
        "by_symbol_reason": _pack(by_pair, "pair"),
        "note": (
            "expectancy_usdt — сколько приносит ОДНА средняя сделка после комиссий и "
            "проскальзывания; это единственное число, по которому осмысленно "
            "сравнивать символы и сетапы. Win-rate рядом справочно: 67% побед при "
            "payoff_ratio 0.11 — убыточная система. Причины вида legacy_* — сделки, "
            "закрытые до того, как причина входа стала записываться; разрез по ним "
            "неинформативен и накопится заново."
        ),
    }
