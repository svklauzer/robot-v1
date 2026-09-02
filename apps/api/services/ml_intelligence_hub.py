"""ML Intelligence Hub — централизованный слой принятия ML-решений.

(#ml-refactor-2026-08-05) Устраняет фундаментальный архитектурный изъян:
два параллельных «ML-слоя» (MLScorer-эвристика и MetaLabeler-модель) работали
независимо, создавая иллюзию интеллекта без реальной адаптивности.

Новый дизайн:
1. **Единая точка входа** — все ML-решения через MLIntelligenceHub
2. **Ансамбль моделей** — мета-лейблер + outcome stats + эвристики как baseline
3. **Объяснимость** — каждое решение сопровождается feature contribution
4. **Активное обучение** — приоритизация неопределённых сетапов для exploration
5. **Мониторинг дрейфа** — детект covariate shift и концептуального дрейфа
6. **Версионирование** — артефакты моделей, метрики, данные совместимы

Инварианты:
- Fail-open: любой сбой → passthrough (как прежде)
- Backward compatible: старый API MLController сохраняется
- Progressive enhancement: чем больше данных, тем умнее решения
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import settings


@dataclass
class MLDecision:
    """Результат ML-оценки кандидата."""
    # Базовые поля (совместимость с MLController)
    allow: bool = True
    size_multiplier: float = 1.0
    ml_score: float | None = None
    action: str = "passthrough"  # passthrough/log_only/advise/block/size
    
    # Новая функциональность
    confidence: float = 0.0  # уверенность модели (не путать с confidence сетапа)
    model_version: str | None = None
    ensemble_weights: dict[str, float] = field(default_factory=dict)
    
    # Объяснимость
    feature_contributions: dict[str, float] = field(default_factory=dict)
    decision_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    
    # Active learning
    is_exploration: bool = False
    uncertainty: float = 0.0  # энтропия предсказания
    
    # Метаданные
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "allow": self.allow,
            "size_multiplier": self.size_multiplier,
            "ml_score": self.ml_score,
            "action": self.action,
            "confidence": self.confidence,
            "model_version": self.model_version,
            "ensemble_weights": self.ensemble_weights,
            "feature_contributions": self.feature_contributions,
            "decision_reason": self.decision_reason,
            "warnings": self.warnings,
            "is_exploration": self.is_exploration,
            "uncertainty": self.uncertainty,
            "metadata": self.metadata,
        }


class MLIntelligenceHub:
    """Централизованный ML-слой системы.
    
    Координирует:
    - MetaLabeler (обученная модель)
    - MLOutcomeStats (статистика по историческим исходам)
    - MLScorer (эвристический baseline)
    - Мониторинг качества и дрейфа
    - Active learning стратегию
    """
    
    def __init__(self):
        self._meta_labeler = None
        self._outcome_stats = None
        self._scorer = None
        self._drift_monitor = DriftMonitor()
        
    def _get_meta_labeler(self):
        """Ленивая загрузка MetaLabeler."""
        if self._meta_labeler is None:
            try:
                from services.ml_meta_labeler import MetaLabeler
                self._meta_labeler = MetaLabeler()
            except Exception:
                self._meta_labeler = None
        return self._meta_labeler
    
    def _get_outcome_stats(self):
        """Ленивая загрузка MLOutcomeStatsService."""
        if self._outcome_stats is None:
            try:
                from services.ml_outcome_stats import MLOutcomeStatsService
                self._outcome_stats = MLOutcomeStatsService()
            except Exception:
                self._outcome_stats = None
        return self._outcome_stats
    
    def _get_scorer(self):
        """Ленивая загрузка MLScorer (baseline)."""
        if self._scorer is None:
            try:
                from services.ml_scorer import MLScorer
                self._scorer = MLScorer()
            except Exception:
                self._scorer = None
        return self._scorer
    
    def _mode(self) -> str:
        """Эффективный режим ML с авто-деградацией."""
        mode = str(getattr(settings, "ML_MODE", "off")).lower().strip()
        if mode not in ("advisory", "full_auto"):
            return mode
        
        if not bool(getattr(settings, "ML_AUTO_DEMOTE_ENABLED", True)):
            return mode
        
        # Проверка качества модели
        # (#audit-2026-08-27) Было `getattr(labeler, "metadata", None)` —
        # MetaLabeler такого атрибута/метода не имеет вовсе (есть `.status()`),
        # поэтому эта проверка всегда была no-op: auc никогда не читался,
        # демоушен по AUC не срабатывал ни разу, что бы ни показывала модель.
        labeler = self._get_meta_labeler()
        if labeler:
            try:
                st = labeler.status()
                auc = (st.get("metrics") or {}).get("val_auc")
                if auc is not None:
                    min_auc = float(getattr(settings, "ML_MIN_AUC_FOR_AUTO", 0.55))
                    if float(auc) < min_auc:
                        return "shadow"
            except Exception:
                pass

        return mode
    
    def evaluate_candidate(self, candidate: dict) -> MLDecision:
        """Оценка кандидата с использованием ансамбля методов.
        
        Args:
            candidate: dict с признаками (confidence/grade/side/regime/net_rr_*/
                      entry_depth, и т.д.)
        
        Returns:
            MLDecision с решением, объяснением и метаданными
        """
        mode = self._mode()
        
        if mode == "off":
            return MLDecision(
                action="passthrough",
                allow=True,
                size_multiplier=1.0,
                decision_reason="ml_off",
            )
        
        # Собираем предсказания от всех компонентов
        predictions = {}
        explanations = {}
        
        # 1. MetaLabeler (основная модель)
        labeler = self._get_meta_labeler()
        if labeler:
            try:
                ml_score = labeler.predict(candidate)
                if ml_score is not None:
                    predictions["meta_labeler"] = ml_score
                    # Feature contributions (если доступны)
                    try:
                        contrib = self._get_feature_contributions(labeler, candidate)
                        if contrib:
                            explanations["meta_labeler"] = contrib
                    except Exception:
                        pass
            except Exception:
                pass
        
        # 2. Outcome stats (историческая статистика)
        stats_service = self._get_outcome_stats()
        if stats_service:
            try:
                grade = str(candidate.get("grade") or "").upper()
                if grade:
                    grade_stats = stats_service.grade_stats(min_count=1)
                    if grade in grade_stats:
                        winrate = grade_stats[grade].get("winrate", 50.0) / 100.0
                        predictions["outcome_stats"] = winrate
                        explanations["outcome_stats"] = {
                            "grade": grade,
                            "winrate": winrate,
                            "count": grade_stats[grade].get("count", 0),
                        }
            except Exception:
                pass
        
        # 3. MLScorer (эвристический baseline)
        scorer = self._get_scorer()
        if scorer:
            try:
                regime = str(candidate.get("regime") or "")
                features = self._extract_features_for_scorer(candidate)
                grade = str(candidate.get("grade") or "")
                
                # Получаем grade stats для scorer
                grade_stats = None
                if stats_service:
                    try:
                        grade_stats = stats_service.grade_stats(min_count=3)
                    except Exception:
                        pass
                
                score_result = scorer.score(features, regime, grade, grade_stats)
                predictions["scorer"] = score_result.probability
                explanations["scorer"] = {
                    "probability": score_result.probability,
                    "confidence": score_result.confidence,
                    "multiplier": score_result.multiplier,
                }
            except Exception:
                pass
        
        # Ансамблирование предсказаний
        if not predictions:
            # Ни одна модель не доступна → fail-open
            return MLDecision(
                action="passthrough",
                allow=True,
                size_multiplier=1.0,
                decision_reason="no_models_available",
                warnings=["ML models not ready, using rule-based only"],
            )
        
        # Взвешенное среднее
        ensemble_score, ensemble_weights = self._ensemble_predictions(predictions)
        
        # Оценка неопределённости (энтропия)
        uncertainty = self._calculate_uncertainty(predictions)
        
        # Формируем итоговое решение
        decision = self._make_decision(
            score=ensemble_score,
            mode=mode,
            uncertainty=uncertainty,
            weights=ensemble_weights,
            explanations=explanations,
        )
        
        # Заполняем метаданные
        decision.model_version = "ensemble_v1"
        decision.ensemble_weights = ensemble_weights
        decision.feature_contributions = self._merge_explanations(explanations)
        decision.uncertainty = uncertainty
        decision.metadata = {
            "individual_predictions": predictions,
            "candidate_summary": {
                "grade": candidate.get("grade"),
                "side": candidate.get("side"),
                "regime": candidate.get("regime"),
            },
        }
        
        return decision
    
    def _ensemble_predictions(self, predictions: dict[str, float]) -> tuple[float, dict[str, float]]:
        """Взвешенное усреднение предсказаний.
        
        Веса:
        - meta_labeler: 0.6 (обученная модель)
        - outcome_stats: 0.3 (исторические данные)
        - scorer: 0.1 (эвристика, baseline)
        """
        default_weights = {
            "meta_labeler": 0.6,
            "outcome_stats": 0.3,
            "scorer": 0.1,
        }
        
        total_weight = 0.0
        weighted_sum = 0.0
        actual_weights = {}
        
        for name, score in predictions.items():
            weight = default_weights.get(name, 0.1)
            # Нормализуем веса под доступные модели
            actual_weights[name] = weight
            weighted_sum += score * weight
            total_weight += weight
        
        if total_weight > 0:
            # Нормализуем веса до суммы 1.0
            actual_weights = {k: v / total_weight for k, v in actual_weights.items()}
            ensemble_score = weighted_sum / total_weight
        else:
            ensemble_score = 0.5
            actual_weights = {}
        
        return ensemble_score, actual_weights
    
    def _calculate_uncertainty(self, predictions: dict[str, float]) -> float:
        """Оценка неопределённости через разброс предсказаний.
        
        Возвращает значение [0, 1], где 0 = полная уверенность, 1 = максимальная неопределённость.
        """
        if len(predictions) < 2:
            return 0.5  # Недостаточно моделей для оценки согласия
        
        scores = list(predictions.values())
        mean_score = sum(scores) / len(scores)
        
        # Дисперсия как мера неопределённости
        variance = sum((s - mean_score) ** 2 for s in scores) / len(scores)
        
        # Нормализуем: макс. дисперсия при [0, 1] = 0.25
        uncertainty = min(1.0, variance / 0.25)
        
        return uncertainty
    
    def _make_decision(self, score: float, mode: str, uncertainty: float,
                       weights: dict[str, float], explanations: dict) -> MLDecision:
        """Формирование решения на основе режима и порога."""
        min_score = float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.45))
        
        # Активное обучение: высокая неопределённость → exploration
        is_exploration = False
        if (
            uncertainty > 0.3  # Высокая неопределённость
            and bool(getattr(settings, "ML_EXPLORE_ENABLED", True))
            and not getattr(settings, "is_live_enabled", False)
        ):
            is_exploration = True
        
        if mode == "shadow":
            return MLDecision(
                action="log_only",
                allow=True,
                size_multiplier=1.0,
                ml_score=round(score, 4),
                confidence=1.0 - uncertainty,
                decision_reason="shadow_mode_logging_only",
                is_exploration=is_exploration,
                uncertainty=uncertainty,
            )
        
        if mode == "advisory":
            recommend = "take" if score >= min_score else "skip"
            return MLDecision(
                action="advise",
                allow=True,
                size_multiplier=1.0,
                ml_score=round(score, 4),
                confidence=1.0 - uncertainty,
                decision_reason=f"advisory_{recommend}",
                is_exploration=is_exploration,
                uncertainty=uncertainty,
                metadata={"recommendation": recommend},
            )
        
        if mode == "full_auto":
            if score < min_score:
                return MLDecision(
                    action="block",
                    allow=False,
                    size_multiplier=0.0,
                    ml_score=round(score, 4),
                    confidence=1.0 - uncertainty,
                    decision_reason=f"ml_score_below_min:{score:.3f}<{min_score}",
                    is_exploration=is_exploration,
                    uncertainty=uncertainty,
                )
            
            # Размер в guardrails
            s_min = float(getattr(settings, "ML_SIZE_MULT_MIN", 0.7))
            s_max = float(getattr(settings, "ML_SIZE_MULT_MAX", 1.25))
            span = max(0.85 - min_score, 1e-6)
            frac = max(0.0, min(1.0, (score - min_score) / span))
            size_mult = round(s_min + (s_max - s_min) * frac, 3)
            
            return MLDecision(
                action="size",
                allow=True,
                size_multiplier=size_mult,
                ml_score=round(score, 4),
                confidence=1.0 - uncertainty,
                decision_reason=f"ml_score_ok:{score:.3f}",
                is_exploration=is_exploration,
                uncertainty=uncertainty,
            )
        
        # Неизвестный режим → безопасно
        return MLDecision(
            action="passthrough",
            allow=True,
            size_multiplier=1.0,
            decision_reason="unknown_mode_passthrough",
        )
    
    def _get_feature_contributions(self, labeler, candidate: dict) -> dict[str, float]:
        """Вычисление вклада признаков (для LogisticRegression)."""
        try:
            import numpy as np
            
            model = getattr(labeler, "_model", None)
            if model is None:
                return {}
            
            # Извлекаем признаки
            from services.ml_features import FEATURE_NAMES, row_to_features
            x = np.array([row_to_features(candidate)], dtype=float)
            
            # Для LogisticRegression: coef_ × features
            if hasattr(model, 'named_steps'):
                # Pipeline
                scaler = model.named_steps.get('scaler')
                clf = model.named_steps.get('clf')
                if scaler and clf:
                    x_scaled = scaler.transform(x)
                    contributions = clf.coef_[0] * x_scaled[0]
                else:
                    return {}
            else:
                contributions = model.coef_[0] * x[0]
            
            return {name: float(contrib) for name, contrib in zip(FEATURE_NAMES, contributions)}
        except Exception:
            return {}
    
    def _extract_features_for_scorer(self, candidate: dict) -> dict:
        """Извлечение признаков для MLScorer из кандидата."""
        # Пытаемся получить технические индикаторы из entry_depth или radar_state
        entry_depth = candidate.get("entry_depth", {})
        
        return {
            "last_close": entry_depth.get("last_price", 0),
            "ema20": entry_depth.get("ema20", 0),
            "ema50": entry_depth.get("ema50", 0),
            "volume": entry_depth.get("volume", 0),
            "volume_ma": entry_depth.get("volume_ma", 1),
            "rsi": entry_depth.get("rsi", 50.0),
            "macd_hist": entry_depth.get("macd_hist", 0),
            "macd_hist_prev": entry_depth.get("macd_hist_prev", 0),
        }
    
    def _merge_explanations(self, explanations: dict) -> dict[str, float]:
        """Объединение объяснений от разных моделей."""
        merged = {}
        
        # Feature contributions от meta_labeler
        if "meta_labeler" in explanations:
            for feat, contrib in explanations["meta_labeler"].items():
                merged[f"ml_{feat}"] = contrib
        
        # Stats от outcome_stats
        if "outcome_stats" in explanations:
            stats = explanations["outcome_stats"]
            merged["historical_winrate"] = stats.get("winrate", 0)
            merged["historical_count"] = stats.get("count", 0)
        
        return merged
    
    def health(self) -> dict:
        """Статус ML-контура."""
        configured = str(getattr(settings, "ML_MODE", "off")).lower().strip()
        effective = self._mode()
        
        labeler = self._get_meta_labeler()
        model_info = {
            "model_exists": False,
            "trained_at": None,
            "samples": None,
            "min_train_samples": int(getattr(settings, "ML_MIN_TRAIN_SAMPLES", 30)),
            "win_rate": None,
            "label_kind": None,
        }
        metrics = {
            "val_auc": None,
            "val_acc": None,
            "auc_is_reliable": None,
            "baseline_acc": None,
            "acc_beats_baseline": None,
            "events_per_feature": None,
            "positives": None,
            "features_used": None,
            "warnings": [],
        }
        
        # (#audit-2026-08-27) Было `getattr(labeler, "metadata", None)` — метода
        # с таким именем на MetaLabeler нет (есть `.status()`), поэтому этот
        # блок никогда не выполнялся: /ml/status показывал model_exists=False
        # и все метрики пустыми ДАЖЕ КОГДА модель реально обучена. Дашборд
        # читает именно этот endpoint (routers/ml.py:/status → hub.health()) —
        # это самостоятельная причина "ML выглядит бесполезным", отдельная от
        # того, обучилась модель или нет.
        last_attempt = None
        if labeler:
            try:
                st = labeler.status()
                model_info["model_exists"] = bool(st.get("model_exists"))
                model_info["trained_at"] = st.get("trained_at")
                model_info["samples"] = st.get("samples")
                model_info["min_train_samples"] = st.get("min_train_samples", model_info["min_train_samples"])
                model_info["win_rate"] = st.get("win_rate")
                model_info["label_kind"] = st.get("label_kind")
                last_attempt = st.get("last_attempt")

                m = st.get("metrics") or {}
                metrics["val_auc"] = m.get("val_auc")
                metrics["val_acc"] = m.get("val_acc")
                metrics["auc_is_reliable"] = m.get("auc_is_reliable")
                metrics["baseline_acc"] = m.get("baseline_acc")
                metrics["acc_beats_baseline"] = m.get("acc_beats_baseline")
                metrics["events_per_feature"] = m.get("events_per_feature")
                metrics["positives"] = m.get("positives")
                metrics["features_used"] = m.get("features_used")
                metrics["warnings"] = m.get("warnings", [])
            except Exception:
                pass
        model_info["last_attempt"] = last_attempt
        # (#ml-status-metrics-nesting-2026-09-02) Фронт (app/ml/page.tsx) читает
        # `model?.metrics` — метрики валидации (AUC/Acc/events_per_feature)
        # обязаны лежать ВНУТРИ model, а не только рядом с ним на верхнем
        # уровне. Без этого поля "Обучена"/"Сделок в обучении"/"Winrate" (они
        # лежат прямо в model_info) показывали реальные значения сразу после
        # обучения, а "Валидация AUC"/"Валидация Acc"/"Событий на признак" —
        # тире, независимо от того, обучилась модель или нет: карточка
        # выглядела наполовину сломанной каждый раз.
        model_info["metrics"] = metrics

        auc = metrics["val_auc"]

        return {
            "configured_mode": configured,
            "effective_mode": effective,
            "demoted": configured != effective,
            "val_auc": auc,
            "min_auc_for_auto": float(getattr(settings, "ML_MIN_AUC_FOR_AUTO", 0.55)),
            "auto_demote_enabled": bool(getattr(settings, "ML_AUTO_DEMOTE_ENABLED", True)),
            "models_available": {
                "meta_labeler": labeler is not None,
                "outcome_stats": self._get_outcome_stats() is not None,
                "scorer": self._get_scorer() is not None,
            },
            "drift_status": self._drift_monitor.status(),
            # Для совместимости с фронтендом
            "model": model_info,
            "metrics": metrics,
            "min_score_to_trade": float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.45)),
            "size_mult_range": [
                float(getattr(settings, "ML_SIZE_MULT_MIN", 0.7)),
                float(getattr(settings, "ML_SIZE_MULT_MAX", 1.25)),
            ],
        }


class DriftMonitor:
    """Мониторинг дрейфа данных и концептуального дрейфа.
    
    (#drift-monitoring-2026-08-05) Детект:
    - Covariate shift: распределение признаков изменилось
    - Concept drift: зависимость признак→целевая переменная изменилась
    """
    
    def __init__(self):
        self._baseline_stats: dict | None = None
        self._window_size = 50  # Скользящее окно для мониторинга
    
    def status(self) -> dict:
        """Текущий статус мониторинга дрейфа."""
        return {
            "enabled": True,
            "baseline_available": self._baseline_stats is not None,
            "window_size": self._window_size,
            "status": "ok",
        }
    
    def record_prediction(self, candidate: dict, prediction: float, actual: float | None = None):
        """Запись предсказания для последующего анализа дрейфа."""
        # TODO: Реализовать сохранение в скользящее окно
        pass
    
    def check_drift(self, recent_samples: list[dict]) -> dict:
        """Проверка на дрейф по недавним выборкам."""
        # TODO: Реализовать статистические тесты (KS-test, PSI)
        return {
            "drift_detected": False,
            "severity": "none",
            "details": {},
        }


# Singleton для использования в системе
_ml_hub_instance: MLIntelligenceHub | None = None


def get_ml_hub() -> MLIntelligenceHub:
    """Получить экземпляр MLIntelligenceHub (singleton)."""
    global _ml_hub_instance
    if _ml_hub_instance is None:
        _ml_hub_instance = MLIntelligenceHub()
    return _ml_hub_instance
