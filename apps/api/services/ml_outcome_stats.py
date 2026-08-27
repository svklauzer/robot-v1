import json
import math
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

from core.config import settings
from services.ml_features import row_to_label

def sanitize_float(value, default=0.0) -> float:
    """Санитизация float-значений для JSON: nan/inf -> default."""
    if value is None:
        return default
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default

class MLOutcomeStatsService:
    """
    Читает storage/ml/trade_outcomes.jsonl и строит статистику по закрытым сделкам.

    Пока это не ML-модель, а статистический слой памяти:
    - какие symbol/side чаще дают stop_loss
    - какие symbol/side дают protected profit
    - где positive_then_negative слишком часто
    - где net pnl отрицательный
    """

    def __init__(self, path: str | Path | None = None, stale_hours: int | None = None):
        self.file_path = Path(path) if path is not None else Path(getattr(settings, "TRADE_OUTCOMES_PATH", "storage/ml/trade_outcomes.jsonl"))
        self.stale_hours = int(stale_hours or getattr(settings, "ML_OUTCOMES_STALE_HOURS", 72) or 72)

    def safe_summary(self) -> dict:
        try:
            return self.summary()
        except Exception as e:
            return {
                "status": "degraded",
                "reason": "ml_outcome_stats_failed",
                "error": f"{type(e).__name__}: {e}",
                "source_path": str(self.file_path),
            }

    def _load_rows(self) -> list[dict]:
        if not self.file_path.exists():
            return []

        rows = []
        self._parse_errors = 0

        with self.file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()

                if not line:
                    continue

                try:
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        rows.append(payload)
                    else:
                        self._parse_errors = getattr(self, "_parse_errors", 0) + 1
                except Exception:
                    self._parse_errors = getattr(self, "_parse_errors", 0) + 1
                    continue

        return rows

    def summary(self) -> dict:
        rows = self._load_rows()

        total = len(rows)

        if total == 0:
            return {
                "status": "empty",
                "total": 0,
                "parse_errors": getattr(self, "_parse_errors", 0),
                "source_path": str(self.file_path),
                "groups": [],
                **self._freshness_payload(rows),
            }

        freshness = self._freshness_payload(rows)

        groups = defaultdict(lambda: {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "stop_loss": 0,
            "protected_profit": 0,
            "tp2": 0,
            "positive_then_negative": 0,
            "net_pnl": 0.0,
            "costs": 0.0,
            "mfe_sum": 0.0,
            "mae_sum": 0.0,
            "mfe_count": 0,
            "mae_count": 0,
        })

        for row in rows:
            symbol = row.get("symbol") or "unknown"
            side = row.get("side") or "unknown"
            key = f"{symbol}:{side}"

            labels = row.get("labels") or {}
            lifecycle = row.get("lifecycle") or {}

            group = groups[key]
            group["symbol"] = symbol
            group["side"] = side
            group["count"] += 1

            net_pnl = sanitize_float(row.get("closed_net_pnl"), 0.0)
            costs = sanitize_float(row.get("closed_total_cost"), 0.0)

            group["net_pnl"] += net_pnl
            group["costs"] += costs

            if net_pnl > 0:
                group["wins"] += 1
            elif net_pnl < 0:
                group["losses"] += 1

            if labels.get("hit_stop"):
                group["stop_loss"] += 1

            if labels.get("protected_profit"):
                group["protected_profit"] += 1

            if labels.get("hit_tp2"):
                group["tp2"] += 1

            if labels.get("positive_then_negative"):
                group["positive_then_negative"] += 1

            mfe = sanitize_float(lifecycle.get("mfe_pct"), 0.0)
            mae = sanitize_float(lifecycle.get("mae_pct"), 0.0)

            if lifecycle.get("mfe_pct") is not None:
                group["mfe_sum"] += mfe
                group["mfe_count"] += 1

            if lifecycle.get("mae_pct") is not None:
                group["mae_sum"] += mae
                group["mae_count"] += 1

        result_groups = []

        for key, group in groups.items():
            count = group["count"]

            wins = group["wins"]
            losses = group["losses"]

            winrate = round((wins / count) * 100, 2) if count else 0.0
            stop_rate = round((group["stop_loss"] / count) * 100, 2) if count else 0.0
            protected_rate = round((group["protected_profit"] / count) * 100, 2) if count else 0.0
            positive_then_negative_rate = round((group["positive_then_negative"] / count) * 100, 2) if count else 0.0

            avg_mfe = (
                round(group["mfe_sum"] / group["mfe_count"], 4)
                if group["mfe_count"]
                else 0.0
            )

            avg_mae = (
                round(group["mae_sum"] / group["mae_count"], 4)
                if group["mae_count"]
                else 0.0
            )

            net_pnl = round(group["net_pnl"], 6)
            costs = round(group["costs"], 6)

            risk_state = "neutral"

            if count >= 2 and net_pnl < 0 and stop_rate >= 50:
                risk_state = "penalize"

            if count >= 2 and net_pnl > 0 and protected_rate >= 50:
                risk_state = "reward"

            if count >= 3 and positive_then_negative_rate >= 50:
                risk_state = "protect_earlier"

            result_groups.append({
                "key": key,
                "symbol": group["symbol"],
                "side": group["side"],

                "count": count,
                "wins": wins,
                "losses": losses,
                "winrate": winrate,

                "stop_loss": group["stop_loss"],
                "stop_rate": stop_rate,

                "protected_profit": group["protected_profit"],
                "protected_rate": protected_rate,

                "tp2": group["tp2"],

                "positive_then_negative": group["positive_then_negative"],
                "positive_then_negative_rate": positive_then_negative_rate,

                "avg_mfe_pct": avg_mfe,
                "avg_mae_pct": avg_mae,

                "net_pnl": net_pnl,
                "costs": costs,

                "risk_state": risk_state,
            })

        result_groups.sort(
            key=lambda item: (
                item["risk_state"] != "penalize",
                item["net_pnl"],
            )
        )

        return {
            "status": "stale" if freshness["stale"] else "ok",
            "total": total,
            "parse_errors": getattr(self, "_parse_errors", 0),
            "source_path": str(self.file_path),
            "groups": result_groups,
            **freshness,
        }


    def _freshness_payload(self, rows: list[dict]) -> dict:
        latest = None
        for row in rows:
            logged_at = self._parse_datetime(row.get("logged_at"))
            if logged_at is not None and (latest is None or logged_at > latest):
                latest = logged_at

        if latest is None:
            return {
                "latest_logged_at": None,
                "latest_age_hours": None,
                "latest_age_days": None,
                "stale": False,
                "is_stale": False,
                "freshness_status": "empty" if not rows else "missing_logged_at",
                "stale_after_hours": self.stale_hours,
            }

        age_hours = max(round((datetime.now(timezone.utc) - latest).total_seconds() / 3600, 2), 0.0)
        stale = age_hours > self.stale_hours
        return {
            "latest_logged_at": latest.isoformat(),
            "latest_age_hours": age_hours,
            "latest_age_days": round(age_hours / 24, 2),
            "stale": stale,
            "is_stale": stale,
            "freshness_status": "stale" if stale else "fresh",
            "stale_after_hours": self.stale_hours,
        }

    def _parse_datetime(self, value) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def penalty_for(self, symbol: str, side: str) -> dict:
        stats = self.summary()

        if stats.get("status") != "ok":
            return {
                "penalty": 0.0,
                "reason": "no_ml_outcome_stats",
                "stats": None,
            }

        key = f"{symbol}:{side}"

        row = None

        for group in stats.get("groups", []):
            if group.get("key") == key:
                row = group
                break

        if not row:
            return {
                "penalty": 0.0,
                "reason": "no_symbol_side_history",
                "stats": None,
            }

        count = int(row.get("count") or 0)

        # Меньше 3 сделок — статистика ещё слабая.
        if count < 3:
            return {
                "penalty": 0.0,
                "reason": "not_enough_history",
                "stats": row,
            }

        penalty = 0.0
        reasons = []

        if row.get("net_pnl", 0) < 0:
            penalty += 4.0
            reasons.append("negative_net_pnl")

        if row.get("stop_rate", 0) >= 50:
            penalty += 5.0
            reasons.append("high_stop_rate")

        if row.get("positive_then_negative_rate", 0) >= 40:
            penalty += 4.0
            reasons.append("positive_then_negative_too_often")

        if row.get("avg_mae_pct", 0) <= -1.0:
            penalty += 3.0
            reasons.append("avg_mae_too_deep")

        reward = 0.0

        if row.get("net_pnl", 0) > 0:
            reward += 2.0

        if row.get("protected_rate", 0) >= 50:
            reward += 2.0

        final_penalty = max(0.0, penalty - reward)

        return {
            "penalty": round(final_penalty, 2),
            "reason": "_".join(reasons) if reasons else "ok",
            "stats": row,
        }

    def grade_stats(self, min_count: int = 3) -> dict[str, dict]:
        """
        Return per-grade performance stats computed from trade_outcomes.jsonl.

        Only grades with >= min_count closed trades are included.
        Used by MLScorer to apply a data-driven confidence multiplier.

        Returns dict keyed by grade (e.g. "A+", "A", "B", "C"):
          {
            "count": int,
            "wins": int,
            "winrate": float,          # 0-100
            "avg_net_pnl": float,
            "failed_setup_rate": float, # 0-100
            "tp2_rate": float,          # 0-100
          }
        """
        rows = self._load_rows()
        closed = [r for r in rows if str(r.get("status") or "") == "closed"]

        from collections import defaultdict
        buckets: dict[str, dict] = defaultdict(lambda: {
            "count": 0, "wins": 0, "net_pnl": 0.0,
            "failed_setup": 0, "tp2": 0,
        })

        for r in closed:
            grade = str(r.get("grade") or "unknown").upper()
            pnl = sanitize_float(r.get("closed_net_pnl"), 0.0)
            labels = r.get("labels") or {}
            b = buckets[grade]
            b["count"] += 1
            b["net_pnl"] += pnl
            if pnl > 0:
                b["wins"] += 1
            if r.get("closed_reason") == "failed_setup_exit":
                b["failed_setup"] += 1
            if labels.get("hit_tp2"):
                b["tp2"] += 1

        result = {}
        for grade, b in buckets.items():
            n = b["count"]
            if n < min_count:
                continue
            result[grade] = {
                "count": n,
                "wins": b["wins"],
                "winrate": round(b["wins"] / n * 100, 1),
                "avg_net_pnl": round(b["net_pnl"] / n, 4),
                "failed_setup_rate": round(b["failed_setup"] / n * 100, 1),
                "tp2_rate": round(b["tp2"] / n * 100, 1),
            }
        return result

    def shadow_report(self) -> dict:
        """Отчёт для Shadow mode: калибровка ml_score против фактических исходов.

        Возвращает:
        - live_auc: AUC против is_win (closed_net_pnl > 0) — для сравнения
        - live_auc_vs_train_label: AUC против ЦЕЛИ ОБУЧЕНИЯ (ML_LABEL_KIND,
          по умолчанию beats_costs) — то, что модель реально предсказывает.
          Вердикт считается по ЭТОЙ метрике.
        - base_winrate: базовый winrate выборки
        - threshold: текущий порог входа
        - buckets: калибровка по диапазонам ml_score (обе метки + РЕАЛЬНЫЙ net PnL)
        - threshold_impact: оценка выгоды от гейтирования по порогу (реальные USDT)

        (#audit-2026-08-27) Раньше эта функция мерила модель против is_win —
        цели, на которую модель НЕ обучена (обучена на beats_costs, см.
        ml_features.row_to_label — сделка +0.05R это win для is_win, но 0 для
        beats_costs). Плюс net_pnl_usdt/ml_gate_benefit_usdt были не суммой
        реального PnL, а TODO-заглушкой (всегда 0.0) и грубой формулой
        `(winrate_delta/100)*count*10` соответственно — не настоящие деньги.
        """
        rows = self._load_rows()

        # Фильтруем только закрытые сделки с ml_score
        closed_with_score = [
            r for r in rows
            if str(r.get("status") or "") == "closed"
            and r.get("ml_score") is not None
        ]

        if len(closed_with_score) < 5:
            return {
                "status": "insufficient_sample",
                "message": f"Нужно минимум 5 сделок с ml_score, сейчас {len(closed_with_score)}",
                "scored_closed": len(closed_with_score),
            }

        label_kind = str(getattr(settings, "ML_LABEL_KIND", "beats_costs"))
        min_r = float(getattr(settings, "ML_LABEL_MIN_R", 0.3))

        # Извлекаем y_true (is_win), y_pred (ml_score), net (реальный USDT) и
        # train_label (то, чему модель реально обучена) с санитизацией.
        y_true: list[int] = []
        y_pred: list[float] = []
        nets: list[float] = []
        train_labels: list[int | None] = []
        for r in closed_with_score:
            pnl = sanitize_float(r.get("closed_net_pnl"), 0.0)
            y_true.append(1 if pnl > 0 else 0)
            nets.append(pnl)
            score_raw = r.get("ml_score")
            y_pred.append(sanitize_float(score_raw, 0.5) if score_raw is not None else 0.5)
            train_labels.append(row_to_label(r, label_kind, min_r=min_r))

        # Проверка на единственный класс (предупреждение sklearn)
        n_pos = sum(y_true)
        n_neg = len(y_true) - n_pos
        if n_pos == 0 or n_neg == 0:
            # Только один класс - AUC не определен, возвращаем 0.5
            return {
                "status": "single_class",
                "message": "Все сделки одного класса (только прибыли или только убытки). AUC не определен.",
                "scored_closed": len(closed_with_score),
                "all_wins": n_pos == len(y_true),
                "all_losses": n_neg == len(y_true),
                "fallback_auc": 0.5,
            }

        live_auc = self._safe_auc(y_true, y_pred)

        # AUC против ЦЕЛИ ОБУЧЕНИЯ — только строки, где метка вообще считается
        # (beats_costs требует net_pnl_stop; строки без него исключены отсюда,
        # но остаются в is_win-метрике выше).
        labeled_idx = [i for i, lbl in enumerate(train_labels) if lbl is not None]
        yt_train = [train_labels[i] for i in labeled_idx]
        yp_train = [y_pred[i] for i in labeled_idx]
        live_auc_vs_train_label = (
            self._safe_auc(yt_train, yp_train)
            if len(set(yt_train)) >= 2 else None
        )

        # Базовый winrate
        base_winrate = round(sum(y_true) / len(y_true) * 100, 2) if y_true else 0.0

        # Порог входа (по умолчанию 0.45)
        threshold = float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.45))

        # Калибровка по бакетам (реальный net PnL + обе метки)
        buckets = self._calibration_buckets(y_true, y_pred, nets, train_labels, threshold)

        # Оценка выгоды гейта (реальные USDT: taken_net - total_net)
        threshold_impact = self._threshold_impact(y_true, y_pred, nets, threshold)

        # Вердикт — по AUC против цели обучения; если строк с меткой мало,
        # откатываемся на is_win-AUC, но помечаем источник.
        verdict_auc = live_auc_vs_train_label if live_auc_vs_train_label is not None else live_auc
        verdict_source = "train_label" if live_auc_vs_train_label is not None and len(labeled_idx) >= 20 else "is_win_fallback"
        verdict = "insufficient_sample"
        if len(closed_with_score) >= 20 and (verdict_source == "is_win_fallback" or len(labeled_idx) >= 20):
            if verdict_auc >= 0.60:
                verdict = "edge_visible"
            elif verdict_auc >= 0.52:
                verdict = "weak_signal"
            else:
                verdict = "no_edge_yet"

        return {
            "status": "ok",
            "scored_closed": len(closed_with_score),
            "live_auc": live_auc,
            "live_auc_vs_train_label": live_auc_vs_train_label,
            "train_label_kind": label_kind,
            "train_label_sample": len(labeled_idx),
            "base_winrate_pct": base_winrate,
            "threshold": threshold,
            "buckets": buckets,
            "threshold_impact": threshold_impact,
            "verdict": verdict,
            "verdict_source": verdict_source,
        }

    def _safe_auc(self, y_true: list[int], y_pred: list[float]) -> float:
        try:
            from sklearn.metrics import roc_auc_score
            raw = roc_auc_score(y_true, y_pred)
            if raw is None or (isinstance(raw, float) and (math.isnan(raw) or math.isinf(raw))):
                return 0.5
            return round(max(0.0, min(1.0, float(raw))), 4)
        except Exception:
            return self._simple_auc(y_true, y_pred)
    
    def _simple_auc(self, y_true: list[int], y_pred: list[float]) -> float:
        """Простая оценка AUC без sklearn."""
        n_pos = sum(y_true)
        n_neg = len(y_true) - n_pos
        
        if n_pos == 0 or n_neg == 0:
            return 0.5
        
        # Ранжируем предсказания
        ranked = sorted(zip(y_pred, y_true), reverse=True)
        
        # Считаем сумму рангов положительных
        rank_sum = 0
        for i, (_, label) in enumerate(ranked):
            if label == 1:
                rank_sum += (len(ranked) - i)
        
        auc = (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
        return round(max(0.0, min(1.0, auc)), 4)
    
    def _calibration_buckets(self, y_true: list[int], y_pred: list[float],
                             nets: list[float] | None = None,
                             train_labels: list[int | None] | None = None,
                             threshold: float = 0.45) -> list[dict]:
        """Калибровка: группировка по диапазонам ml_score.

        (#audit-2026-08-27) net_pnl_usdt раньше был TODO-заглушкой (всегда
        0.0) — теперь реальная сумма closed_net_pnl по бакету. Добавлен
        train_label_winrate_pct — доля сделок, оправдавших ЦЕЛЬ ОБУЧЕНИЯ
        (обычно beats_costs), не is_win.
        """
        nets = nets if nets is not None else [0.0] * len(y_pred)
        train_labels = train_labels if train_labels is not None else [None] * len(y_pred)
        # Диапазоны: [0-0.3), [0.3-0.4), [0.4-0.5), [0.5-0.6), [0.6-0.7), [0.7-1.0]
        ranges = [
            (0.0, 0.3, "0.00–0.30"),
            (0.3, 0.4, "0.30–0.40"),
            (0.4, 0.5, "0.40–0.50"),
            (0.5, 0.6, "0.50–0.60"),
            (0.6, 0.7, "0.60–0.70"),
            (0.7, 1.1, "0.70+"),
        ]

        buckets = []
        for low, high, label in ranges:
            indices = [i for i, score in enumerate(y_pred) if low <= score < high]

            if not indices:
                continue

            count = len(indices)
            wins = sum(y_true[i] for i in indices)
            winrate_pct = round(wins / count * 100, 1) if count > 0 else 0.0
            avg_score = round(sum(y_pred[i] for i in indices) / count, 3) if count > 0 else 0.0
            net_pnl_usdt = round(sum(nets[i] for i in indices), 4)

            tl_indices = [i for i in indices if train_labels[i] is not None]
            tl_count = len(tl_indices)
            tl_wins = sum(train_labels[i] for i in tl_indices)

            buckets.append({
                "range": label,
                "count": count,
                "winrate_pct": winrate_pct,
                "avg_score": avg_score,
                "net_pnl_usdt": net_pnl_usdt,
                "train_label_count": tl_count,
                "train_label_winrate_pct": round(tl_wins / tl_count * 100, 1) if tl_count else None,
            })

        return buckets

    def _threshold_impact(self, y_true: list[int], y_pred: list[float],
                          nets: list[float] | None = None, threshold: float = 0.45) -> dict:
        """Оценка выгоды от применения порога threshold — в РЕАЛЬНЫХ USDT.

        (#audit-2026-08-27) ml_gate_benefit_usdt раньше был грубой формулой
        `(winrate_delta/100)*count*10` — произвольная оценка "$10 за
        процентный пункт", не связанная с реальными деньгами сделок. Теперь
        это taken_net - total_net (та же формула, что в services/
        ml_shadow_report.py) — сколько РЕАЛЬНО заработали/потеряли бы взятые
        сделки относительно книги "все сделки".
        """
        nets = nets if nets is not None else [0.0] * len(y_pred)
        taken_indices = [i for i, score in enumerate(y_pred) if score >= threshold]
        skipped_indices = [i for i, score in enumerate(y_pred) if score < threshold]

        taken_count = len(taken_indices)
        skipped_count = len(skipped_indices)

        taken_winrate_pct = round(sum(y_true[i] for i in taken_indices) / taken_count * 100, 1) if taken_count > 0 else 0.0
        skipped_winrate_pct = round(sum(y_true[i] for i in skipped_indices) / skipped_count * 100, 1) if skipped_count > 0 else 0.0

        taken_net = round(sum(nets[i] for i in taken_indices), 4)
        skipped_net = round(sum(nets[i] for i in skipped_indices), 4)
        total_net = round(sum(nets), 4)
        # ВЫГОДА от ML-гейта = насколько книга "только взятые" лучше книги
        # "все" = taken_net - total_net = -skipped_net. >0 → гейт отрезал бы
        # чистый минус.
        ml_gate_benefit_usdt = round(taken_net - total_net, 4)

        return {
            "taken_count": taken_count,
            "taken_winrate_pct": taken_winrate_pct,
            "taken_net_usdt": taken_net,
            "skipped_count": skipped_count,
            "skipped_winrate_pct": skipped_winrate_pct,
            "skipped_net_usdt": skipped_net,
            "ml_gate_benefit_usdt": ml_gate_benefit_usdt,
        }