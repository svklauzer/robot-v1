"""Где внутри CRT деньги (#crt-quality-2026-09-20).

Зачем. На чистой неделе OKX CRT — единственный убыточный движок: 16 сделок,
−15.48 USDT против +21.66 у остальных семидесяти пяти. При этом пять его порогов
в проде ослаблены против дефолта: `CRT_MIN_SETUP_SCORE` 45 вместо 55,
`CRT_MIN_RR_TP1` 0.5 вместо 1.0, `CRT_REQUIRE_CISD` выключён, `CRT_TP2_RR` 1.5
вместо 2.0, `CRT_STOP_BUFFER_PCT` 0.08 вместо 0.05.

Соблазн — выключить движок целиком. Но «CRT теряет» и «теряет определённая
часть CRT» ведут к разным решениям, и отличить их можно только замером. У CRT
своя геометрия, и она вся записана в план сделки: глубина съёма ликвидности,
подтверждения (MSS, FVG), ширина диапазона, положение цены в нём, итоговая
оценка сетапа. Отчёт раскладывает результат по этим осям.

Главный вопрос, на который он отвечает: обоснован ли порог оценки 55, то есть
отличаются ли сделки ниже него от сделок выше. Если разделения нет ни по одной
оси — движок плох целиком, и это аргумент другого рода.

Ничего не меняет — только измеряет.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from models.signal import Signal

STRATEGY = "crt_3candle"
# Дефолт `CRT_MIN_SETUP_SCORE`, от которого прод отступил вниз. Разрез строится
# вокруг него: ровно этот порог и проверяется на данных.
DEFAULT_MIN_SCORE = 55.0


def _f(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _notional(signal: Signal, plan: dict) -> float:
    entry = _f((plan.get("lifecycle") or {}).get("entry_price"))
    if not entry:
        zone = signal.entry_zone_json or {}
        low, high = _f(zone.get("from")), _f(zone.get("to"))
        entry = ((low + high) / 2.0) if (low and high) else (low or high)
    return (entry or 0.0) * (_f(signal.qty) or 0.0)


def _bucket() -> dict[str, Any]:
    return {"trades": 0, "net_pnl_usdt": 0.0, "notional_sum": 0.0,
            "wins": 0, "ptn": 0, "mfe": [], "mae": []}


def _add(bucket: dict[str, Any], *, net_pnl: float, notional: float,
         mfe: float | None, mae: float | None, ptn: bool) -> None:
    bucket["trades"] += 1
    bucket["net_pnl_usdt"] += net_pnl
    bucket["notional_sum"] += notional
    bucket["wins"] += int(net_pnl > 0)
    bucket["ptn"] += int(ptn)
    if mfe is not None:
        bucket["mfe"].append(mfe)
    if mae is not None:
        bucket["mae"].append(mae)


def _finish(bucket: dict[str, Any]) -> dict[str, Any] | None:
    n = bucket["trades"]
    if not n:
        return None
    avg_mfe = sum(bucket["mfe"]) / len(bucket["mfe"]) if bucket["mfe"] else None
    avg_mae = sum(bucket["mae"]) / len(bucket["mae"]) if bucket["mae"] else None
    notional = bucket["notional_sum"]
    return {
        "trades": n,
        "net_pnl_usdt": round(bucket["net_pnl_usdt"], 4),
        # Нормировка на номинал: бакеты могут отличаться средним размером сделки,
        # и сравнивать их в USDT напрямую нельзя.
        "net_pnl_per_notional_pct": round(bucket["net_pnl_usdt"] / notional * 100, 4) if notional else None,
        "winrate_pct": round(bucket["wins"] / n * 100, 1),
        "ptn_rate_pct": round(bucket["ptn"] / n * 100, 1),
        "avg_mfe_pct": round(avg_mfe, 4) if avg_mfe is not None else None,
        "avg_mae_pct": round(avg_mae, 4) if avg_mae is not None else None,
        "edge_ratio": round(avg_mfe / abs(avg_mae), 3) if (avg_mfe and avg_mae and avg_mae < 0) else None,
    }


def _score_bucket(score: float | None) -> str:
    if score is None:
        return "без оценки"
    if score < DEFAULT_MIN_SCORE:
        return f"score < {DEFAULT_MIN_SCORE:.0f} (прод пускает, дефолт нет)"
    if score < 70:
        return f"score {DEFAULT_MIN_SCORE:.0f}–70"
    return "score ≥ 70"


def _depth_bucket(depth: float | None) -> str:
    if depth is None:
        return "без глубины"
    if depth < 10:
        return "съём < 10%"
    if depth < 25:
        return "съём 10–25%"
    return "съём ≥ 25%"


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
    axes: dict[str, dict[str, dict]] = {
        "by_score": {}, "by_confirmation": {}, "by_sweep_side": {},
        "by_sweep_depth": {}, "by_symbol": {},
    }

    for signal in signals:
        plan = signal.plan_json or {}
        setup = plan.get("setup_quality") or {}
        if str(setup.get("strategy") or "") != STRATEGY:
            continue

        lifecycle = plan.get("lifecycle") or {}
        row = {
            "net_pnl": _f(signal.closed_net_pnl) or 0.0,
            "notional": _notional(signal, plan),
            "mfe": _f(lifecycle.get("mfe_pct")),
            "mae": _f(lifecycle.get("mae_pct")),
            "ptn": bool(lifecycle.get("positive_then_negative")),
        }
        _add(overall, **row)

        mss, fvg = bool(setup.get("mss")), bool(setup.get("fvg"))
        confirmation = ("MSS+FVG" if mss and fvg else
                        "только MSS" if mss else
                        "только FVG" if fvg else "без подтверждения")
        keys = {
            "by_score": _score_bucket(_f(setup.get("final_score"))),
            "by_confirmation": confirmation,
            "by_sweep_side": str(setup.get("sweep") or "без съёма"),
            "by_sweep_depth": _depth_bucket(_f(setup.get("sweep_depth_pct"))),
            "by_symbol": str(signal.symbol),
        }
        for axis, key in keys.items():
            _add(axes[axis].setdefault(key, _bucket()), **row)

    result: dict[str, Any] = {
        "status": "ok",
        "strategy": STRATEGY,
        "sample_count": overall["trades"],
        "window_hours": window_hours,
        "overall": _finish(overall) or {"trades": 0},
        "live_thresholds": {
            "min_setup_score": _f(_setting("CRT_MIN_SETUP_SCORE")),
            "default_min_setup_score": DEFAULT_MIN_SCORE,
            "min_rr_tp1": _f(_setting("CRT_MIN_RR_TP1")),
            "require_cisd": bool(_setting("CRT_REQUIRE_CISD")),
            "tp2_rr": _f(_setting("CRT_TP2_RR")),
            "stop_buffer_pct": _f(_setting("CRT_STOP_BUFFER_PCT")),
        },
    }
    for axis, buckets in axes.items():
        rows = []
        for key, bucket in buckets.items():
            finished = _finish(bucket)
            if finished:
                rows.append({"key": key, **finished})
        rows.sort(key=lambda r: r["net_pnl_per_notional_pct"] if r["net_pnl_per_notional_pct"] is not None else 0.0)
        result[axis] = rows

    result["note"] = (
        "Разрез по собственной геометрии CRT. Главная ось — оценка сетапа: прод "
        f"пускает от {result['live_thresholds']['min_setup_score']}, дефолт — от "
        f"{DEFAULT_MIN_SCORE:.0f}. Если сделки ниже дефолтного порога не отличаются "
        "от остальных, возврат порога ничего не даст и вопрос не в нём."
    )
    return result


def _setting(name: str):
    from core.config import settings

    return getattr(settings, name, None)
