"""MLController — control plane ML. fail-open: любой сбой → passthrough.

#ml-audit-2026-11-23: полномочия advisory/full_auto требуют LIVE- edge
(live_auc + экономический сплит), а не только офлайн val_auc/wf.
"""
from __future__ import annotations

from core.config import settings

_PASSTHROUGH = {"mode": "off", "ml_score": None, "action": "passthrough",
                "allow": True, "size_multiplier": 1.0, "reason": "ml_off"}


def resolve_effective_mode(configured: str, metrics: dict | None) -> tuple[str, str | None]:
    mode = str(configured or "off").lower().strip()
    if mode not in ("advisory", "full_auto"):
        return mode, None
    if not bool(getattr(settings, "ML_AUTO_DEMOTE_ENABLED", True)):
        return mode, None
    m = metrics or {}
    reasons: list[str] = []
    auc_raw = m.get("val_auc")
    if auc_raw is None:
        return "shadow", "нет val_auc — полномочия отозваны до shadow"
    try:
        auc = float(auc_raw)
    except (TypeError, ValueError):
        return "shadow", f"val_auc={auc_raw!r} не число — до shadow"
    if auc < float(getattr(settings, "ML_MIN_AUC_FOR_AUTO", 0.55)):
        reasons.append(f"val_auc={auc:.4f} ниже порога")
    try:
        epv = float(m.get("events_per_feature") or 0.0)
    except (TypeError, ValueError):
        epv = 0.0
    if epv < 3.0:
        reasons.append(f"events_per_feature={epv} < 3.0")
    if m.get("auc_is_reliable") is False:
        reasons.append("обучение пометило метрику НЕНАДЁЖНОЙ")
    ci = m.get("wf_auc_ci95")
    if isinstance(ci, (list, tuple)) and len(ci) == 2 and ci[0] is not None:
        try:
            if float(ci[0]) <= 0.5:
                reasons.append(f"нижняя граница wf ДИ {ci[0]} ≤ 0.5")
        except (TypeError, ValueError):
            pass
    # (#ml-audit) LIVE-гейт: офлайн AUC не переносится в бой автоматически.
    live_auc = m.get("live_auc")
    live_econ = m.get("live_econ_diff")
    min_live = float(getattr(settings, "ML_PROMOTION_MIN_LIVE_AUC", 0.55))
    min_econ = float(getattr(settings, "ML_PROMOTION_MIN_ECON_DIFF", 0.0))
    if live_auc is None:
        reasons.append("нет живой валидации (live_auc) — полномочия не выданы")
    else:
        try:
            if float(live_auc) < min_live:
                reasons.append(f"live_auc={live_auc} < {min_live}")
        except (TypeError, ValueError):
            reasons.append("live_auc не число")
    if live_econ is not None:
        try:
            if float(live_econ) <= min_econ:
                reasons.append(f"live_econ_diff={live_econ} ≤ {min_econ}")
        except (TypeError, ValueError):
            pass
    if reasons:
        return "shadow", "; ".join(reasons) + " — до shadow"
    return mode, None


class MLController:
    def __init__(self):
        self._labeler = None
        self._last_demote_reason = None

    def _get_labeler(self):
        if self._labeler is None:
            try:
                from services.ml_meta_labeler import MetaLabeler
                self._labeler = MetaLabeler()
            except Exception:
                self._labeler = None
        return self._labeler

    def _metrics_block(self) -> dict | None:
        try:
            labeler = self._get_labeler()
            st = labeler.status() if labeler else {}
            return st.get("metrics") or {}
        except Exception:
            return None

    def _mode(self) -> str:
        configured = str(getattr(settings, "ML_MODE", "off")).lower().strip()
        mode, reason = resolve_effective_mode(configured, self._metrics_block())
        self._last_demote_reason = reason
        return mode

    def _metric(self, key: str):
        try:
            labeler = self._get_labeler()
            st = labeler.status() if labeler else {}
            metrics = st.get("metrics") or {}
            return metrics.get(key) if key in metrics else st.get(key)
        except Exception:
            return None

    def _val_auc(self) -> float | None:
        try:
            labeler = self._get_labeler()
            st = labeler.status() if labeler else {}
            v = (st.get("metrics") or {}).get("val_auc")
            return float(v) if v is not None else None
        except Exception:
            return None

    def effective_mode(self) -> str:
        return self._mode()

    def health(self) -> dict:
        configured = str(getattr(settings, "ML_MODE", "off")).lower().strip()
        effective = self._mode()
        return {
            "configured_mode": configured, "effective_mode": effective,
            "demoted": configured != effective,
            "demote_reason": self._last_demote_reason if configured != effective else None,
            "val_auc": self._val_auc(),
            "min_auc_for_auto": float(getattr(settings, "ML_MIN_AUC_FOR_AUTO", 0.55)),
            "auto_demote_enabled": bool(getattr(settings, "ML_AUTO_DEMOTE_ENABLED", True)),
            "auc_is_reliable": self._metric("auc_is_reliable"),
            "events_per_feature": self._metric("events_per_feature"),
            "val_positives": self._metric("val_positives"),
            "live_auc": self._metric("live_auc"),
            "live_econ_diff": self._metric("live_econ_diff"),
        }

    def _model_version(self) -> dict:
        try:
            labeler = self._get_labeler()
            st = labeler.status() if labeler else {}
            fv = st.get("feature_version")
            if fv is None:
                from services.ml_features import FEATURE_VERSION
                fv = FEATURE_VERSION
            return {"model_trained_at": st.get("trained_at"), "feature_version": fv}
        except Exception:
            return {"model_trained_at": None, "feature_version": None}

    def evaluate_candidate(self, candidate: dict) -> dict:
        mode = self._mode()
        if mode == "off":
            return dict(_PASSTHROUGH)
        try:
            labeler = self._get_labeler()
            ml_score = labeler.predict(candidate) if labeler else None
        except Exception:
            ml_score = None
        # (#ml-audit) логируем вектор на входе — на нём потом обучаемся.
        try:
            from services.ml_features import features_dict, FEATURE_VERSION
            feat_log = {"feature_version": FEATURE_VERSION, "features": features_dict(candidate)}
        except Exception:
            feat_log = {}
        if ml_score is None:
            return {**feat_log, "mode": mode, "ml_score": None, "action": "passthrough",
                    "allow": True, "size_multiplier": 1.0, "reason": "ml_not_ready"}
        ver = self._model_version()
        base = {**ver, **feat_log, "ml_score": round(ml_score, 4)}
        if mode == "shadow":
            return {**base, "mode": "shadow", "action": "log_only", "allow": True,
                    "size_multiplier": 1.0, "reason": "shadow_logged"}
        if mode == "advisory":
            min_score = float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.45))
            rec = "take" if ml_score >= min_score else "skip"
            return {**base, "mode": "advisory", "action": "advise", "allow": True,
                    "size_multiplier": 1.0, "recommend": rec, "reason": f"advisory_{rec}"}
        if mode == "full_auto":
            min_score = float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.45))
            if ml_score < min_score:
                return {**base, "mode": "full_auto", "action": "block", "allow": False,
                        "size_multiplier": 0.0, "reason": f"ml_score_below_min:{ml_score:.3f}<{min_score}"}
            s_min = float(getattr(settings, "ML_SIZE_MULT_MIN", 0.7))
            s_max = float(getattr(settings, "ML_SIZE_MULT_MAX", 1.25))
            span = max(0.85 - min_score, 1e-6)
            frac = max(0.0, min(1.0, (ml_score - min_score) / span))
            return {**base, "mode": "full_auto", "action": "size", "allow": True,
                    "size_multiplier": round(s_min + (s_max - s_min) * frac, 3),
                    "reason": f"ml_score_ok:{ml_score:.3f}"}
        return dict(_PASSTHROUGH)
