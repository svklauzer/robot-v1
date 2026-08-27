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
import json
from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal
from services.ml_features import row_to_label


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


def _parse_plan_json(plan_raw: Any) -> dict:
    """Безопасно парсит plan_json: если строка — десериализует, иначе возвращает как dict."""
    if plan_raw is None:
        return {}
    if isinstance(plan_raw, dict):
        return plan_raw
    if isinstance(plan_raw, str):
        try:
            return json.loads(plan_raw)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


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

    # (#audit-2026-08-27) Модель обучается НЕ на is_win (net_pnl>0), а на
    # ML_LABEL_KIND (по умолчанию "beats_costs" — вернула ли сделка хотя бы
    # ML_LABEL_MIN_R риска, см. ml_features.row_to_label). Раньше этот отчёт
    # сравнивал прогноз с is_win — другой целью, на которую модель не
    # обучалась: сделка +0.05R (label=0 при обучении, модель ПРАВИЛЬНО ставит
    # её низко) засчитывалась здесь как "win" и штрафовала верный прогноз.
    # is_win остаётся как есть — привычная метрика, но `train_label`/
    # `live_auc_vs_train_label` ниже меряют модель против того, чему её
    # реально учили.
    label_kind = str(getattr(settings, "ML_LABEL_KIND", "beats_costs"))
    min_r = float(getattr(settings, "ML_LABEL_MIN_R", 0.3))

    rows: list[dict] = []
    for s in signals:
        plan = _parse_plan_json(s.plan_json)
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
        train_label = row_to_label(
            {"closed_net_pnl": s.closed_net_pnl, "net_pnl_stop": s.net_pnl_stop},
            label_kind, min_r,
        )
        rows.append({
            "id": s.id, "symbol": s.symbol, "side": s.side, "grade": s.grade,
            "score": score, "win": 1 if net > 0 else 0, "net": net,
            "train_label": train_label,
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

    # ── AUC против ЦЕЛИ ОБУЧЕНИЯ (train_label), не против is_win ───────────────
    labeled = [r for r in rows if r["train_label"] is not None]
    train_wins = [r for r in labeled if r["train_label"] == 1]
    train_losses = [r for r in labeled if r["train_label"] == 0]
    auc_train_label = _auc([r["score"] for r in train_wins], [r["score"] for r in train_losses])

    # ── калибровка по бакетам ──────────────────────────────────────────────────
    edges = [0.0, 0.30, thr, 0.60, 0.75, 1.0001]
    edges = sorted(set(edges))
    buckets = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        b = [r for r in rows if lo <= r["score"] < hi]
        cnt = len(b)
        bw = sum(r["win"] for r in b)
        b_labeled = [r for r in b if r["train_label"] is not None]
        bl_cnt = len(b_labeled)
        bl_wins = sum(r["train_label"] for r in b_labeled)
        buckets.append({
            "range": f"{lo:.2f}–{hi if hi <= 1.0 else 1.0:.2f}",
            "count": cnt,
            "wins": bw,
            "winrate_pct": round(bw / cnt * 100, 1) if cnt else None,
            "avg_score": round(sum(r["score"] for r in b) / cnt, 4) if cnt else None,
            "net_pnl_usdt": round(sum(r["net"] for r in b), 4),
            # Доля сделок, реально оправдавших label_kind (по умолчанию
            # beats_costs) в этом бакете — то, на что модель обучена отвечать.
            "train_label_count": bl_cnt,
            "train_label_winrate_pct": round(bl_wins / bl_cnt * 100, 1) if bl_cnt else None,
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
    # Вердикт считаем по auc_train_label — метрике против ЦЕЛИ ОБУЧЕНИЯ, а не
    # против is_win. Если размеченных строк (есть net_pnl_stop) не хватает,
    # откатываемся на is_win-AUC, но помечаем это в ответе.
    verdict_auc = auc_train_label if auc_train_label is not None else auc
    verdict_source = "train_label" if auc_train_label is not None else "is_win_fallback"
    if n < 30 or len(labeled) < 30:
        verdict = "insufficient_sample"
    elif verdict_auc is not None and verdict_auc >= 0.58 and (taken_wr or 0) > base_winrate and threshold_impact["ml_gate_benefit_usdt"] > 0:
        verdict = "edge_visible"
    elif verdict_auc is not None and verdict_auc >= 0.53:
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
        # (#audit-2026-08-27) AUC против label_kind, на который модель РЕАЛЬНО
        # обучена (по умолчанию beats_costs, см. ML_LABEL_KIND) — не против
        # is_win. `live_auc` выше остаётся для сравнения/совместимости, но
        # решение (`verdict`) теперь принимается по этой метрике.
        "live_auc_vs_train_label": auc_train_label,
        "train_label_kind": label_kind,
        "train_label_sample": len(labeled),
        "threshold": thr,
        "buckets": buckets,
        "threshold_impact": threshold_impact,
        "verdict": verdict,
        "verdict_source": verdict_source,
        "note": (
            "live_auc_vs_train_label>0.55 и монотонные train_label_winrate_pct по бакетам "
            "= модель видит сигнал в том, чему её реально учили. live_auc (против is_win) "
            "оставлен для сравнения — он не то, что модель предсказывает, и может расходиться "
            "с live_auc_vs_train_label. avoided_net>0 = порог отрезал бы убыточные. "
            "Выборка мала (<30) → не доверять. Это shadow — на сделки НЕ влияет."
        ),
    }