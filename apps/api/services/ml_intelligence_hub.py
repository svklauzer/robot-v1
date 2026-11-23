"""ML Intelligence Hub — единая точка ML-решений. fail-open.

#ml-audit-2026-11-23:
 - ансамбль доверяет валидированному мета-лейблеру; grade-статистика
   (антипредсказательна) НЕ смешивается в скор;
 - логирование features/feature_version на входе (train/serve alignment);
 - DriftMonitor реализован (PSI против train-baseline);
 - полномочия через resolve_effective_mode (live-гейт).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from core.config import settings


@dataclass
class MLDecision:
    allow: bool = True
    size_multiplier: float = 1.0
    ml_score: float | None = None
    action: str = "passthrough"
    confidence: float = 0.0
    model_version: str | None = None
    ensemble_weights: dict[str, float] = field(default_factory=dict)
    feature_contributions: dict[str, float] = field(default_factory=dict)
    decision_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    is_exploration: bool = False
    uncertainty: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    feature_version: int | None = None
    features: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "allow": self.allow, "size_multiplier": self.size_multiplier,
            "ml_score": self.ml_score, "action": self.action, "confidence": self.confidence,
            "model_version": self.model_version, "ensemble_weights": self.ensemble_weights,
            "feature_contributions": self.feature_contributions,
            "decision_reason": self.decision_reason, "warnings": self.warnings,
            "is_exploration": self.is_exploration, "uncertainty": self.uncertainty,
            "metadata": self.metadata,
            "feature_version": self.feature_version, "features": self.features,
        }


class MLIntelligenceHub:
    def __init__(self):
        self._meta_labeler = None
        self._outcome_stats = None
        self._scorer = None
        self._drift_monitor = DriftMonitor()
        self._last_demote_reason = None

    def _get_meta_labeler(self):
        if self._meta_labeler is None:
            try:
                from services.ml_meta_labeler import MetaLabeler
                self._meta_labeler = MetaLabeler()
            except Exception:
                self._meta_labeler = None
        return self._meta_labeler

    def _get_outcome_stats(self):
        if self._outcome_stats is None:
            try:
                from services.ml_outcome_stats import MLOutcomeStatsService
                self._outcome_stats = MLOutcomeStatsService()
            except Exception:
                self._outcome_stats = None
        return self._outcome_stats

    def _get_scorer(self):
        if self._scorer is None:
            try:
                from services.ml_scorer import MLScorer
                self._scorer = MLScorer()
            except Exception:
                self._scorer = None
        return self._scorer

    def _mode(self) -> str:
        from services.ml_controller import resolve_effective_mode
        configured = str(getattr(settings, "ML_MODE", "off")).lower().strip()
        metrics = None
        labeler = self._get_meta_labeler()
        if labeler:
            try:
                metrics = labeler.status().get("metrics") or {}
            except Exception:
                metrics = None
        mode, reason = resolve_effective_mode(configured, metrics)
        self._last_demote_reason = reason
        return mode

    def evaluate_candidate(self, candidate: dict) -> MLDecision:
        mode = self._mode()
        # (#ml-audit) вектор на входе — логируем всегда (train/serve alignment).
        try:
            from services.ml_features import features_dict, FEATURE_VERSION
            feat = features_dict(candidate)
            fv = FEATURE_VERSION
        except Exception:
            feat, fv = {}, None
        if mode == "off":
            return MLDecision(action="passthrough", decision_reason="ml_off",
                              feature_version=fv, features=feat)
        predictions: dict[str, float] = {}
        explanations: dict = {}
        labeler = self._get_meta_labeler()
        if labeler:
            try:
                sc = labeler.predict(candidate)
                if sc is not None:
                    predictions["meta_labeler"] = sc
                    contrib = self._get_feature_contributions(labeler, candidate)
                    if contrib:
                        explanations["meta_labeler"] = contrib
            except Exception:
                pass
        # (#ml-audit) grade-статистика антипредсказательна — в скор НЕ мешаем.
        # Эвристический scorer — только fallback, когда модели нет.
        if "meta_labeler" not in predictions:
            scorer = self._get_scorer()
            if scorer:
                try:
                    feats = self._extract_features_for_scorer(candidate)
                    sr = scorer.score(feats, str(candidate.get("regime") or ""),
                                      str(candidate.get("grade") or ""), None)
                    predictions["scorer"] = sr.probability
                except Exception:
                    pass
        if not predictions:
            return MLDecision(action="passthrough", decision_reason="no_models_available",
                              warnings=["ML not ready, rule-based only"],
                              feature_version=fv, features=feat)
        ensemble_score, weights = self._ensemble_predictions(predictions)
        uncertainty = self._calculate_uncertainty(predictions)
        decision = self._make_decision(ensemble_score, mode, uncertainty, weights, explanations)
        decision.model_version = "meta_v6" if "meta_labeler" in predictions else "heuristic_fallback"
        decision.ensemble_weights = weights
        decision.feature_contributions = self._merge_explanations(explanations)
        decision.uncertainty = uncertainty
        decision.feature_version = fv
        decision.features = feat
        # дрейф
        try:
            self._drift_monitor.record(feat)
            drift = self._drift_monitor.check_drift(labeler)
            if drift.get("drift_detected"):
                decision.warnings.append(f"covariate_drift PSI={drift.get('psi')}")
                if mode in ("advisory", "full_auto"):
                    decision.action = "log_only"; decision.allow = True
                    decision.size_multiplier = 1.0
                    decision.decision_reason = "drift_demote_to_shadow"
        except Exception:
            pass
        decision.metadata = {
            "individual_predictions": predictions,
            "candidate_summary": {"grade": candidate.get("grade"), "side": candidate.get("side"),
                                  "regime": candidate.get("regime")},
        }
        return decision

    def _ensemble_predictions(self, predictions: dict[str, float]) -> tuple[float, dict[str, float]]:
        # (#ml-audit) валидированная модель — единственный авторитет; иначе fallback.
        if "meta_labeler" in predictions:
            weights = {"meta_labeler": 1.0}
        elif "scorer" in predictions:
            weights = {"scorer": 1.0}
        else:
            weights = {k: 1.0 / len(predictions) for k in predictions}
        total = sum(weights.values())
        score = sum(predictions[k] * w for k, w in weights.items()) / total if total else 0.5
        return score, {k: round(w / total, 3) for k, w in weights.items()} if total else {}

    def _calculate_uncertainty(self, predictions: dict[str, float]) -> float:
        if len(predictions) < 2:
            return 0.5
        scores = list(predictions.values())
        mean = sum(scores) / len(scores)
        var = sum((s - mean) ** 2 for s in scores) / len(scores)
        return min(1.0, var / 0.25)

    def _make_decision(self, score, mode, uncertainty, weights, explanations) -> MLDecision:
        min_score = float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.50))
        if mode == "shadow":
            return MLDecision(action="log_only", allow=True, size_multiplier=1.0,
                              ml_score=round(score, 4), confidence=1.0 - uncertainty,
                              decision_reason="shadow_mode_logging_only")
        if mode == "advisory":
            rec = "take" if score >= min_score else "skip"
            return MLDecision(action="advise", allow=True, size_multiplier=1.0,
                              ml_score=round(score, 4), confidence=1.0 - uncertainty,
                              decision_reason=f"advisory_{rec}")
        if mode == "full_auto":
            if score < min_score:
                return MLDecision(action="block", allow=False, size_multiplier=0.0,
                                  ml_score=round(score, 4), confidence=1.0 - uncertainty,
                                  decision_reason=f"ml_score_below_min:{score:.3f}<{min_score}")
            s_min = float(getattr(settings, "ML_SIZE_MULT_MIN", 0.7))
            s_max = float(getattr(settings, "ML_SIZE_MULT_MAX", 1.25))
            span = max(0.85 - min_score, 1e-6)
            frac = max(0.0, min(1.0, (score - min_score) / span))
            return MLDecision(action="size", allow=True,
                              size_multiplier=round(s_min + (s_max - s_min) * frac, 3),
                              ml_score=round(score, 4), confidence=1.0 - uncertainty,
                              decision_reason=f"ml_score_ok:{score:.3f}")
        return MLDecision(action="passthrough", allow=True, size_multiplier=1.0)

    def _get_feature_contributions(self, labeler, candidate: dict) -> dict[str, float]:
        try:
            import numpy as np
            from services.ml_features import FEATURE_NAMES
            model = labeler._ensure_model() if hasattr(labeler, "_ensure_model") else getattr(labeler, "_model", None)
            if model is None:
                return {}
            cache = getattr(self, "_imp_cache", None)
            if cache and cache[0] == id(model):
                return dict(cache[1])
            def _imp(est):
                step = est.named_steps.get("clf") if hasattr(est, "named_steps") else est
                return getattr(step, "feature_importances_", None)
            imps = []
            for cc in getattr(model, "calibrated_classifiers_", None) or []:
                base = getattr(cc, "estimator", None) or getattr(cc, "base_estimator", None)
                v = _imp(base) if base is not None else None
                if v is not None:
                    imps.append(np.asarray(v, dtype=float))
            if not imps:
                v = _imp(model)
                if v is not None:
                    imps.append(np.asarray(v, dtype=float))
            if not imps:
                return {}
            mean = np.mean(imps, axis=0)
            out = {n: float(x) for n, x in zip(FEATURE_NAMES, mean)}
            self._imp_cache = (id(model), out)
            return dict(out)
        except Exception:
            return {}

    def _extract_features_for_scorer(self, candidate: dict) -> dict:
        d = candidate.get("entry_depth", {}) or {}
        return {"last_close": d.get("last_price", 0), "ema20": d.get("ema20", 0),
                "ema50": d.get("ema50", 0), "volume": d.get("volume", 0),
                "volume_ma": d.get("volume_ma", 1), "rsi": d.get("rsi", 50.0),
                "macd_hist": d.get("macd_hist", 0), "macd_hist_prev": d.get("macd_hist_prev", 0)}

    def _merge_explanations(self, explanations: dict) -> dict[str, float]:
        merged = {}
        if "meta_labeler" in explanations:
            for feat, contrib in explanations["meta_labeler"].items():
                merged[f"ml_{feat}"] = contrib
        return merged

    def health(self) -> dict:
        configured = str(getattr(settings, "ML_MODE", "off")).lower().strip()
        effective = self._mode()
        labeler = self._get_meta_labeler()
        model_info = {"model_exists": False, "trained_at": None, "samples": None,
                      "min_train_samples": int(getattr(settings, "ML_MIN_TRAIN_SAMPLES", 150)),
                      "win_rate": None, "label_kind": None}
        metrics = {"val_auc": None, "val_acc": None, "auc_is_reliable": None, "baseline_acc": None,
                   "acc_beats_baseline": None, "events_per_feature": None, "positives": None,
                   "features_used": None, "warnings": [], "live_auc": None, "live_econ_diff": None}
        last_attempt = None
        if labeler:
            try:
                st = labeler.status()
                model_info.update({"model_exists": bool(st.get("model_exists")),
                                   "trained_at": st.get("trained_at"), "samples": st.get("samples"),
                                   "win_rate": st.get("win_rate"), "label_kind": st.get("label_kind")})
                last_attempt = st.get("last_attempt")
                m = st.get("metrics") or {}
                # (#ml-card-binding-metrics-2026-11-24) Карточка показывала
                # events_per_feature (по ВСЕМ данным) и val_auc, а полномочия
                # решает train_events_per_feature и wf_auc. Показываем оба:
                # «9 из 10» на экране при связывающих 6.85 — это ввод в заблуждение.
                for k in ("train_events_per_feature", "train_positives",
                          "wf_auc", "wf_auc_ci95", "wf_oos_positives",
                          "live_n", "live_auc", "live_econ_diff"):
                    metrics[k] = m.get(k)            
                for k in metrics:
                    if k in m:
                        metrics[k] = m[k]
            except Exception:
                pass
        model_info["last_attempt"] = last_attempt
        model_info["metrics"] = metrics
        return {
            "configured_mode": configured, "effective_mode": effective,
            "demoted": configured != effective,
            "demote_reason": self._last_demote_reason if configured != effective else None,
            "val_auc": metrics["val_auc"],
            "min_auc_for_auto": float(getattr(settings, "ML_MIN_AUC_FOR_AUTO", 0.55)),
            "auto_demote_enabled": bool(getattr(settings, "ML_AUTO_DEMOTE_ENABLED", True)),
            "models_available": {"meta_labeler": labeler is not None,
                                 "outcome_stats": self._get_outcome_stats() is not None,
                                 "scorer": self._get_scorer() is not None},
            "drift_status": self._drift_monitor.status(labeler),
            "model": model_info, "metrics": metrics,
            "min_score_to_trade": float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.50)),
            "size_mult_range": [float(getattr(settings, "ML_SIZE_MULT_MIN", 0.7)),
                                float(getattr(settings, "ML_SIZE_MULT_MAX", 1.25))],
        }


class DriftMonitor:
    """PSI-мониторинг covariate shift против train-baseline (реализован)."""

    def __init__(self):
        self._window: list[dict] = []
        self._window_size = 50
        self._last: dict = {}

    def record(self, feat: dict):
        if not feat:
            return
        self._window.append(dict(feat))
        if len(self._window) > self._window_size:
            self._window = self._window[-self._window_size:]

    def _baseline(self, labeler):
        try:
            return labeler.drift_baseline() if labeler and hasattr(labeler, "drift_baseline") else None
        except Exception:
            return None

    def check_drift(self, labeler) -> dict:
        from services.ml_features import FEATURE_NAMES
        baseline = self._baseline(labeler)
        if not baseline or len(self._window) < 20:
            self._last = {"drift_detected": False, "psi": None, "status": "warming"}
            return self._last
        q10, q90 = baseline.get("q10") or [], baseline.get("q90") or []
        if len(q10) != len(FEATURE_NAMES):
            self._last = {"drift_detected": False, "psi": None, "status": "baseline_mismatch"}
            return self._last
        psi_total = 0.0
        n = len(self._window)
        for j, name in enumerate(FEATURE_NAMES):
            obs = [0, 0, 0]
            for row in self._window:
                v = float(row.get(name, 0.0))
                obs[0 if v < q10[j] else (2 if v > q90[j] else 1)] += 1
            for o, e in zip(obs, (0.10, 0.80, 0.10)):
                p = max(o / n, 1e-4)
                psi_total += (p - e) * math.log(p / e)
        psi = round(psi_total / len(FEATURE_NAMES), 4)
        thr = float(getattr(settings, "ML_DRIFT_PSI_MAX", 0.25))
        self._last = {"drift_detected": psi > thr, "psi": psi, "threshold": thr, "status": "ok"}
        return self._last

    def status(self, labeler=None) -> dict:
        baseline = self._baseline(labeler)
        return {"enabled": bool(getattr(settings, "ML_DRIFT_ENABLED", True)),
                "baseline_available": baseline is not None,
                "window_size": self._window_size, "implemented": True,
                "status": (self._last or {}).get("status", "no_baseline" if baseline is None else "warming"),
                "psi": (self._last or {}).get("psi"),
                "drift_detected": (self._last or {}).get("drift_detected", False)}


_ml_hub_instance: MLIntelligenceHub | None = None


def get_ml_hub() -> MLIntelligenceHub:
    global _ml_hub_instance
    if _ml_hub_instance is None:
        _ml_hub_instance = MLIntelligenceHub()
    return _ml_hub_instance
