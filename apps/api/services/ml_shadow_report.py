"""Shadow-валидация мета-лейблера: ПРОГНОЗ vs ФАКТ на ЖИВЫХ закрытых сделках.

В режиме ML_MODE=shadow контроллер пишет ml_score (P(win)) в сигнал, НЕ влияя на
торговлю. Этот отчёт сравнивает те предсказания с реальным исходом (closed_net_pnl),
чтобы ЧЕСТНО ответить: бьёт ли модель реальность, прежде чем давать ей руль.

Метрики:
  - live-AUC (rank-based, без sklearn): P(score у победителя > score у лузера).
    0.5 = модель не отличает win от loss; >0.55 = есть сигнал.
  - калибровка по бакетам ml_score: реальный winrate в каждом диапазоне.
    Хорошая модель монотонна: выше score → выше winrate.
  - эффект порога ML_MIN_SCORE_TO_TRADE: что было бы, если бы full_auto отрезал
    score < порога (сколько убытка избежали бы / прибыли потеряли бы).

Источник истины — БД сигналов (plan_json.ml.ml_score + closed_net_pnl). Off-режим
даёт ml_score=null и в выборку не попадает. Никаких внешних зависимостей.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal


def _auc(scores_win: list[float], scores_loss: list[float]) -> float | None:
    """Rank-based AUC = P(score_win > score_loss), ничьи = 0.5. Без sklearn."""
    n_pos, n_neg = len(scores_win), len(scores_loss)
    if n_pos == 0 or n_neg == 0:
        return None
    greater = ties = 0
    for w in scores_win:
        for l in scores_loss:
            if w > l:
                greater += 1
            elif w == l:
                ties += 1
    return round((greater + 0.5 * ties) / (n_pos * n_neg), 4)


def build(db: Session, limit: int = 2000) -> dict[str, Any]:
    mode = str(getattr(settings, "ML_MODE", "off")).lower().strip()
    thr = float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.45))

    signals = (
        db.query(Signal)
        .filter(Signal.status == "closed")
        .order_by(Signal.id.desc())
        .limit(int(limit))
        .all()
    )

    rows: list[dict] = []
    for s in signals:
        plan = s.plan_json or {}
        ml = plan.get("ml") if isinstance(plan, dict) else None
        score = (ml or {}).get("ml_score")
        if score is None:
            continue
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        net = s.closed_net_pnl
        net = float(net) if net is not None else 0.0
        rows.append({
            "id": s.id, "symbol": s.symbol, "side": s.side, "grade": s.grade,
            "score": score, "win": 1 if net > 0 else 0, "net": net,
            "ml_mode": (ml or {}).get("mode"),
        })

    n = len(rows)
    if n == 0:
        return {
            "status": "no_shadow_data",
            "ml_mode": mode,
            "scored_closed": 0,
            "threshold": thr,
            "message": (
                "Нет закрытых сделок с ml_score. Включи ML_MODE=shadow (env) после "
                "обучения модели и дождись закрытий — тогда здесь появится прогноз vs факт."
            ),
        }

    wins = [r for r in rows if r["win"] == 1]
    losses = [r for r in rows if r["win"] == 0]
    auc = _auc([r["score"] for r in wins], [r["score"] for r in losses])

    # ── калибровка по бакетам ──────────────────────────────────────────────────
    edges = [0.0, 0.30, thr, 0.60, 0.75, 1.0001]
    edges = sorted(set(edges))
    buckets = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        b = [r for r in rows if lo <= r["score"] < hi]
        cnt = len(b)
        bw = sum(r["win"] for r in b)
        buckets.append({
            "range": f"{lo:.2f}–{hi if hi <= 1.0 else 1.0:.2f}",
            "count": cnt,
            "wins": bw,
            "winrate_pct": round(bw / cnt * 100, 1) if cnt else None,
            "avg_score": round(sum(r["score"] for r in b) / cnt, 4) if cnt else None,
            "net_pnl_usdt": round(sum(r["net"] for r in b), 4),
        })

    # ── эффект порога (full_auto бы отрезал score < thr) ───────────────────────
    taken = [r for r in rows if r["score"] >= thr]
    skipped = [r for r in rows if r["score"] < thr]
    taken_net = round(sum(r["net"] for r in taken), 4)
    skipped_net = round(sum(r["net"] for r in skipped), 4)
    total_net = round(sum(r["net"] for r in rows), 4)
    threshold_impact = {
        "threshold": thr,
        "taken_count": len(taken),
        "taken_winrate_pct": round(sum(r["win"] for r in taken) / len(taken) * 100, 1) if taken else None,
        "taken_net_usdt": taken_net,
        "skipped_count": len(skipped),
        "skipped_winrate_pct": round(sum(r["win"] for r in skipped) / len(skipped) * 100, 1) if skipped else None,
        "skipped_net_usdt": skipped_net,
        # ВЫГОДА от ML-гейта = насколько книга «только взятые» лучше книги «все».
        # = taken_net − total_net = −skipped_net. >0 → ML отрезал бы убыточные.
        "ml_gate_benefit_usdt": round(taken_net - total_net, 4),
    }

    base_winrate = round(len(wins) / n * 100, 1)
    taken_wr = threshold_impact["taken_winrate_pct"]

    # ── вердикт (с оговоркой на размер выборки) ────────────────────────────────
    if n < 30:
        verdict = "insufficient_sample"
    elif auc is not None and auc >= 0.58 and (taken_wr or 0) > base_winrate and threshold_impact["ml_gate_benefit_usdt"] > 0:
        verdict = "edge_visible"
    elif auc is not None and auc >= 0.53:
        verdict = "weak_signal"
    else:
        verdict = "no_edge_yet"

    return {
        "status": "ok",
        "ml_mode": mode,
        "scored_closed": n,
        "wins": len(wins),
        "losses": len(losses),
        "base_winrate_pct": base_winrate,
        "live_auc": auc,
        "threshold": thr,
        "buckets": buckets,
        "threshold_impact": threshold_impact,
        "verdict": verdict,
        "note": (
            "live_auc>0.55 и монотонные бакеты (выше score → выше winrate) = модель "
            "видит сигнал. avoided_net>0 = порог отрезал бы убыточные. Выборка мала "
            "(<30) → не доверять. Это shadow — на сделки НЕ влияет."
        ),
    }
