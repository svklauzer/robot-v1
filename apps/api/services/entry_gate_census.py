"""Что НА САМОМ ДЕЛЕ не пускает входы (#entry-gate-census-2026-09-04).

Повод. 04.09, окно 11:34–12:44 при активном рынке: ни один вход не прошёл, и
ни одна блокировка не связана с уверенностью — то есть правки оценки поток не
резали. Резали два гейта: `tz_entry_conditions` (в подавляющем большинстве —
условие `adx_rising`) и `tp2_reached_too_rarely`.

Что здесь считается
-------------------
1. Перепись блокировок по `decision` — какой гейт вообще держит поток.
2. Распределение `adx_delta` — впервые доступное, поле пишется с 04.09.
3. Главное число: у скольких событий `adx_rising` — ЕДИНСТВЕННЫЙ enforce-блокер.
   Ослабление условия имеет смысл только для них: там, где параллельно не
   прошли DI, OBV или KAMA, допуск по ADX не изменит ничего.

Почему это нужно измерить, а не решить
--------------------------------------
Условие строгое: `adx <= adx_prev` → блок, допуска нет. В журнале видно
дельты −0.0955, −0.1101, −0.2581 при ADX около 29.5. Это плоский ADX, а не
затухающий тренд. Но «плоский» — оценка на глаз; порог обязан ставиться по
распределению.

И в той же кодовой базе «растёт ли ADX» проверяется ДВАЖДЫ с разными порогами:
здесь строго больше нуля, в анти-чопе `ANTI_CHOP_YOUNG_ADX_RISE_MIN` = 0.5.
В одном дампе видно, как это расходится: AVAX 34.6→34.7 — ADX вырос, но не
дотянул до 0.5 и был отмечен как `adx_not_rising`. Оба порога взяты на глаз,
и отчёт печатает их рядом, чтобы расхождение нельзя было не заметить.

Отчёт ничего не блокирует и ничего не меняет — только показания.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core.config import settings
from models.intelligence_event import IntelligenceEvent

TZ_DECISION = "tz_entry_conditions"
ADX_FAMILY = "adx_rising"

# Допуски, на которых считается «сколько бы прошло». Не рекомендация, а шкала:
# 0.1 — заведомо шум, 0.5 — порог, уже используемый анти-чопом.
TOLERANCES: tuple[float, ...] = (0.05, 0.1, 0.2, 0.3, 0.5)


def _num(value) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _blocked_families(payload: dict) -> list[str]:
    """Семейства, которые РЕАЛЬНО заблокировали (enforce), а не просто не сошлись.

    `failed` содержит все несошедшиеся условия, включая наблюдательные. Ослабление
    наблюдательного условия не откроет вход, поэтому считать надо по enforce.
    """
    reason = str(payload.get("enforce_reason") or "")
    if not reason.startswith("blocked_by:"):
        return []
    return [f for f in reason.split(":", 1)[1].split(",") if f]


def _percentiles(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)

    def at(share: float) -> float:
        idx = min(len(ordered) - 1, max(0, int(round(share * (len(ordered) - 1)))))
        return round(ordered[idx], 4)

    return {"p10": at(0.10), "p25": at(0.25), "median": at(0.50),
            "p75": at(0.75), "p90": at(0.90),
            "min": round(ordered[0], 4), "max": round(ordered[-1], 4)}


def build(db: Session, *, window_hours: float = 24.0, max_rows: int = 20000) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))

    rows = (
        db.query(IntelligenceEvent)
        .filter(IntelligenceEvent.created_at >= cutoff)
        .order_by(IntelligenceEvent.id.desc())
        .limit(int(max_rows))
        .all()
    )

    by_decision: dict[str, int] = {}
    for row in rows:
        key = str(row.decision or "unknown")
        by_decision[key] = by_decision.get(key, 0) + 1

    deltas_all: list[float] = []
    deltas_failed: list[float] = []
    sole_blocker_deltas: list[float] = []
    tz_evaluated = 0
    adx_failed = 0
    adx_enforced = 0

    for row in rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        if str(row.decision or "") != TZ_DECISION or not payload.get("evaluated"):
            continue
        tz_evaluated += 1

        delta = _num(payload.get("adx_delta"))
        if delta is not None:
            deltas_all.append(delta)

        failed = payload.get("failed") or []
        if not any(str(code).startswith("adx_not_rising") for code in failed):
            continue
        adx_failed += 1
        if delta is not None:
            deltas_failed.append(delta)

        families = _blocked_families(payload)
        if ADX_FAMILY not in families:
            continue
        adx_enforced += 1
        # Единственный enforce-блокер: только здесь допуск способен открыть вход.
        if families == [ADX_FAMILY] and delta is not None:
            sole_blocker_deltas.append(delta)

    would_pass = {
        f"{tol:g}": sum(1 for d in sole_blocker_deltas if d > -tol)
        for tol in TOLERANCES
    }

    return {
        "window_hours": float(window_hours),
        "events": len(rows),
        "by_decision": dict(sorted(by_decision.items(), key=lambda kv: kv[1], reverse=True)),
        "adx_rising": {
            "tz_evaluated": tz_evaluated,
            "adx_not_rising_failed": adx_failed,
            "adx_enforced_block": adx_enforced,
            "sole_enforce_blocker": len(sole_blocker_deltas),
            "delta_all": _percentiles(deltas_all),
            "delta_failed": _percentiles(deltas_failed),
            "delta_sole_blocker": _percentiles(sole_blocker_deltas),
            "would_pass_at_tolerance": would_pass,
            "thresholds_in_use": {
                # Два порога одного и того же вопроса, оба взяты на глаз.
                "tz_entry_shadow": "adx > adx_prev (строго, допуск 0)",
                "anti_chop_young_trend": float(
                    getattr(settings, "ANTI_CHOP_YOUNG_ADX_RISE_MIN", 0.5)
                ),
            },
        },
        "note": (
            "sole_enforce_blocker — события, где adx_rising единственное "
            "enforce-условие, не прошедшее проверку. Допуск способен открыть "
            "вход ТОЛЬКО для них: там, где параллельно не прошли di/obv/kama, "
            "он не изменит ничего. would_pass_at_tolerance считает, сколько из "
            "них прошло бы при условии adx_delta > -tolerance."
        ),
    }
