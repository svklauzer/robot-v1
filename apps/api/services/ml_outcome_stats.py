import json
import math
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

from core.config import settings


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
        - live_auc: AUC на реальных сделках
        - base_winrate: базовый winrate выборки
        - threshold: текущий порог входа
        - buckets: калибровка по диапазонам ml_score
        - threshold_impact: оценка выгоды от гейтирования по порогу
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
        
        # Извлекаем y_true и y_pred с санитизацией
        y_true = []
        y_pred = []
        for r in closed_with_score:
            pnl = sanitize_float(r.get("closed_net_pnl"), 0.0)
            y_true.append(1 if pnl > 0 else 0)
            
            score_raw = r.get("ml_score")
            if score_raw is None:
                continue
            score = sanitize_float(score_raw, 0.5)
            y_pred.append(score)
        
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
        
        # Считаем live AUC
        try:
            from sklearn.metrics import roc_auc_score
            live_auc_raw = roc_auc_score(y_true, y_pred)
            # Санитизация: nan/inf -> 0.5 (случайное угадывание)
            import math
            if live_auc_raw is None or (isinstance(live_auc_raw, float) and (math.isnan(live_auc_raw) or math.isinf(live_auc_raw))):
                live_auc = 0.5
            else:
                live_auc = round(max(0.0, min(1.0, live_auc_raw)), 4)
        except Exception:
            # Fallback: простая эвристика AUC
            live_auc = self._simple_auc(y_true, y_pred)
        
        # Базовый winrate
        base_winrate = round(sum(y_true) / len(y_true) * 100, 2) if y_true else 0.0
        
        # Порог входа (по умолчанию 0.45)
        threshold = float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.45))
        
        # Калибровка по бакетам
        buckets = self._calibration_buckets(y_true, y_pred, threshold)
        
        # Оценка выгоды гейта
        threshold_impact = self._threshold_impact(y_true, y_pred, threshold)
        
        # Вердикт
        verdict = "insufficient_sample"
        if len(closed_with_score) >= 20:
            if live_auc >= 0.60:
                verdict = "edge_visible"
            elif live_auc >= 0.52:
                verdict = "weak_signal"
            else:
                verdict = "no_edge_yet"
        
        return {
            "status": "ok",
            "scored_closed": len(closed_with_score),
            "live_auc": live_auc,
            "base_winrate_pct": base_winrate,
            "threshold": threshold,
            "buckets": buckets,
            "threshold_impact": threshold_impact,
            "verdict": verdict,
        }
    
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
    
    def _calibration_buckets(self, y_true: list[int], y_pred: list[float], threshold: float) -> list[dict]:
        """Калибровка: группировка по диапазонам ml_score."""
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
            
            # Net PnL (если доступно)
            net_pnl_usdt = 0.0  # TODO: восстановить из данных
            
            buckets.append({
                "range": label,
                "count": count,
                "winrate_pct": winrate_pct,
                "avg_score": avg_score,
                "net_pnl_usdt": net_pnl_usdt,
            })
        
        return buckets
    
    def _threshold_impact(self, y_true: list[int], y_pred: list[float], threshold: float) -> dict:
        """Оценка выгоды от применения порога threshold."""
        taken_indices = [i for i, score in enumerate(y_pred) if score >= threshold]
        skipped_indices = [i for i, score in enumerate(y_pred) if score < threshold]
        
        taken_count = len(taken_indices)
        skipped_count = len(skipped_indices)
        
        taken_winrate_pct = round(sum(y_true[i] for i in taken_indices) / taken_count * 100, 1) if taken_count > 0 else 0.0
        skipped_winrate_pct = round(sum(y_true[i] for i in skipped_indices) / skipped_count * 100, 1) if skipped_count > 0 else 0.0
        
        # Выгода: насколько улучшили winrate отсечением
        base_winrate = sum(y_true) / len(y_true) * 100 if y_true else 0.0
        improvement = taken_winrate_pct - base_winrate
        
        # ML gate benefit в USDT (оценка)
        # TODO: восстановить реальные суммы из данных
        ml_gate_benefit_usdt = round(improvement / 100 * taken_count * 10, 2)  # Грубая оценка
        
        return {
            "taken_count": taken_count,
            "taken_winrate_pct": taken_winrate_pct,
            "skipped_count": skipped_count,
            "skipped_winrate_pct": skipped_winrate_pct,
            "ml_gate_benefit_usdt": ml_gate_benefit_usdt,
        }