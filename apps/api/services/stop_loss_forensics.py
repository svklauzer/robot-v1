"""Чем сделка, дошедшая до стопа, отличалась НА ВХОДЕ от остальных
(#stop-forensics-2026-09-04).

Замер 04.09 по 91 закрытой сделке двух основных режимов:

    stop_loss        37 сделок   −53.82 USDT   (−1.45 на сделку)
    всё остальное    54 сделки   +34.16 USDT   (+0.63 на сделку)

Без стопов система прибыльна. Весь минус в одном ведре, и сопровождение тут ни
при чём: tz_kama закрывает в среднем по −0.38, breakeven_stop вообще в плюс.
Убивают сделки, которые идут против сразу и доходят до стопа, не дав логике
выхода ни одного шанса. Значит вопрос к ОТБОРУ ВХОДОВ.

Что здесь считается
-------------------
Закрытые сделки делятся на две группы — дошедшие до `stop_loss` и все прочие, —
и по каждому признаку входа сравниваются распределения.

Мера разделения — AUC (доля пар, в которых значение у стопнутой сделки выше,
чем у выжившей). 0.5 — признак не различает группы вовсе; 0 или 1 — различает
идеально. Выбрана намеренно: она ранговая, не требует предположений о
распределении и не ломается от выбросов, которых в 37 наблюдениях достаточно,
чтобы среднее врало.

Знак имеет значение
-------------------
`obi` и `cvd_ratio` — направленные величины: «поток в сторону сделки» для лонга
и шорта имеет ПРОТИВОПОЛОЖНЫЙ знак. Без нормировки по стороне лонги и шорты
взаимно гасятся, и разделение пропадает даже там, где оно есть. Такие признаки
приводятся к виду «больше — значит благоприятнее для ЭТОЙ сделки».

Оговорка о выборке
------------------
37 против 54 — это мало. AUC в пределах 0.40–0.60 здесь не значит ничего, и
отчёт помечает такие признаки как неразличимые явно, чтобы их не приняли за
находку. Цель — не доказать, а сузить круг поиска.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from models.signal import Signal

STOP_REASON = "stop_loss"

# Признаки, у которых благоприятная сторона зависит от направления сделки.
_SIDE_SIGNED = {"entry_depth.obi", "entry_depth.cvd_ratio", "entry_depth.cvd"}

_NUMERIC: tuple[tuple[str, str], ...] = (
    # микроструктура на момент входа
    ("entry_depth.spread_pct", "спред в стакане"),
    ("entry_depth.obi", "перекос стакана В СТОРОНУ сделки"),
    ("entry_depth.cvd_ratio", "поток сделок В СТОРОНУ сделки"),
    ("entry_depth.cvd_trades", "сделок в окне CVD"),
    ("entry_zone_plan.drift_pct", "снос входа от рынка"),
    ("entry_zone_plan.depth.near_depth_share", "объём вблизи входа"),
    # приборы тренда
    ("tz_shadow.adx", "ADX на входе"),
    ("tz_shadow.di_spread", "разведение DI по стороне"),
    ("tz_shadow.stoch_k", "Stoch %K"),
    ("trend_trigger.extension_atr", "растянутость от опоры, ATR"),
    # качество сетапа
    ("setup_quality.final_score", "итоговый скоринг сетапа"),
    ("setup_quality.trend_alignment", "согласованность ТФ"),
    ("setup_quality.entry_timing", "тайминг входа"),
    ("setup_quality.volume_confirmation", "подтверждение объёмом"),
    ("setup_quality.structure_quality", "качество структуры"),
    ("setup_quality.penalty", "штраф сетапа"),
    # экономика плана и ML
    ("ml.ml_score", "ML score"),
    ("tp_reach.tp1_dist_pct", "дистанция до TP1"),
    ("tp_reach.tp2_dist_pct", "дистанция до TP2"),
    ("sizing.conviction", "conviction сайзинга"),
)

_CATEGORICAL: tuple[tuple[str, str], ...] = (
    ("grade", "грейд"),
    ("trade_mode", "режим сделки"),
    ("regime", "режим рынка"),
    ("entry_reason", "причина входа"),
    ("entry_zone_plan.mode", "способ входа"),
    ("tz_shadow.would_pass", "условия ТЗ пройдены"),
    ("performance_guard.reason", "вердикт performance guard"),
)


def _dig(source: dict, path: str):
    node = source
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _auc(a: list[float], b: list[float]) -> float | None:
    """Доля пар (a_i, b_j), где a_i > b_j; ничьи считаются половиной.

    Это ровно статистика Манна–Уитни, нормированная на число пар. Ранговая:
    один выброс не сдвигает её так, как сдвинул бы среднее.
    """
    if not a or not b:
        return None
    wins = 0.0
    for x in a:
        for y in b:
            if x > y:
                wins += 1.0
            elif x == y:
                wins += 0.5
    return wins / (len(a) * len(b))


def _feature_value(signal: Signal, plan: dict, path: str):
    if path in ("grade", "trade_mode", "regime", "entry_reason"):
        return getattr(signal, path, None) if path == "grade" else plan.get(path)
    return _dig(plan, path)


def _oriented(value: float, side: str) -> float:
    """Приводит знаковый признак к виду «больше — благоприятнее для сделки»."""
    return value if str(side).lower() == "long" else -value


def build(db: Session, *, window_hours: float = 720.0, regime: str | None = None,
          max_rows: int = 4000, min_group: int = 5) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))

    query = db.query(Signal).filter(
        Signal.status == "closed",
        Signal.closed_at.isnot(None),
        Signal.closed_at >= cutoff,
    )
    rows = query.order_by(Signal.id.desc()).limit(int(max_rows)).all()

    stopped: list[Signal] = []
    survived: list[Signal] = []
    for s in rows:
        plan = s.plan_json or {}
        if regime and str(plan.get("regime") or "") != regime:
            continue
        (stopped if str(s.closed_reason or "") == STOP_REASON else survived).append(s)

    numeric = [
        _numeric_row(path, label, stopped, survived, min_group)
        for path, label in _NUMERIC
    ]
    numeric = [r for r in numeric if r is not None]
    # Сильнее всего разделяющие — наверх, вне зависимости от направления.
    numeric.sort(key=lambda r: abs(r["auc"] - 0.5), reverse=True)

    categorical = [
        _categorical_row(path, label, stopped, survived)
        for path, label in _CATEGORICAL
    ]
    categorical = [r for r in categorical if r is not None]

    return {
        "window_hours": float(window_hours),
        "regime": regime,
        "stopped": _group_summary(stopped),
        "survived": _group_summary(survived),
        "numeric": numeric,
        "categorical": categorical,
        "note": (
            "auc — доля пар, где значение у стопнутой сделки выше, чем у "
            "выжившей. 0.5 = признак не различает группы. При выборке ~37/54 "
            "диапазон 0.40–0.60 не значит ничего (verdict=indistinguishable); "
            "смотреть стоит только на края. Знаковые признаки (obi, cvd) "
            "приведены к виду «больше = благоприятнее для этой сделки»."
        ),
    }


def _group_summary(signals: list[Signal]) -> dict:
    net = 0.0
    for s in signals:
        try:
            net += float(s.closed_net_pnl or 0.0)
        except (TypeError, ValueError):
            pass
    return {
        "n": len(signals),
        "net_usdt": round(net, 6),
        "avg_usdt": round(net / len(signals), 4) if signals else None,
    }


def _numeric_row(path: str, label: str, stopped: list[Signal],
                 survived: list[Signal], min_group: int) -> dict | None:
    def collect(items: list[Signal]) -> list[float]:
        out: list[float] = []
        for s in items:
            plan = s.plan_json or {}
            value = _num(_dig(plan, path))
            if value is None:
                continue
            if path in _SIDE_SIGNED:
                value = _oriented(value, s.side)
            out.append(value)
        return out

    a, b = collect(stopped), collect(survived)
    if len(a) < min_group or len(b) < min_group:
        return None

    auc = _auc(a, b)
    if auc is None:
        return None

    gap = abs(auc - 0.5)
    verdict = (
        "separates_strongly" if gap >= 0.20
        else "separates_weakly" if gap >= 0.15
        else "indistinguishable"
    )

    return {
        "feature": path,
        "label": label,
        "n_stopped": len(a),
        "n_survived": len(b),
        "median_stopped": round(_median(a), 6),
        "median_survived": round(_median(b), 6),
        "auc": round(auc, 4),
        # >0.5 — у стопнутых значение ВЫШЕ; <0.5 — ниже.
        "higher_in": "stopped" if auc > 0.5 else "survived",
        "verdict": verdict,
    }


def _categorical_row(path: str, label: str, stopped: list[Signal],
                     survived: list[Signal]) -> dict | None:
    levels: dict[str, dict] = {}

    for group, items in (("stopped", stopped), ("survived", survived)):
        for s in items:
            plan = s.plan_json or {}
            raw = getattr(s, path, None) if path == "grade" else _dig(plan, path)
            if raw is None:
                continue
            key = str(raw)
            slot = levels.setdefault(key, {"stopped": 0, "survived": 0, "net_usdt": 0.0})
            slot[group] += 1
            try:
                slot["net_usdt"] = round(slot["net_usdt"] + float(s.closed_net_pnl or 0.0), 6)
            except (TypeError, ValueError):
                pass

    if not levels:
        return None

    for slot in levels.values():
        total = slot["stopped"] + slot["survived"]
        slot["n"] = total
        slot["stop_rate"] = round(slot["stopped"] / total, 4) if total else None

    return {
        "feature": path,
        "label": label,
        "levels": dict(sorted(levels.items(), key=lambda kv: kv[1]["n"], reverse=True)),
    }
