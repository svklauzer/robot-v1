"""MLController — контур управления ML-слоем (control plane).

Единственная точка, через которую rule-based движок взаимодействует с ML.
Читает ML_MODE и решает, КАК использовать предсказание мета-лейблера:

  off       — ничего (ML спит). Дефолт. Поведение системы = как сейчас.
  shadow    — считает ml_score и отдаёт для ЛОГИРОВАНИЯ; на сделки НЕ влияет.
  advisory  — считает + рекомендует; решение остаётся за rule-based/человеком.
  full_auto — ml_score ГЕЙТИТ и масштабирует сделку в пределах guardrails.

Дизайн-инвариант: fail-open. Любой сбой/неготовность → action="passthrough",
ml_score=None — вызывающий код работает ровно как без ML. ML не на крит-пути,
поэтому НЕ мешает запуску робота в live.
"""
from __future__ import annotations

from core.config import settings


_PASSTHROUGH = {"mode": "off", "ml_score": None, "action": "passthrough",
                "allow": True, "size_multiplier": 1.0, "reason": "ml_off"}


class MLController:
    def __init__(self):
        self._labeler = None

    def _mode(self) -> str:
        return str(getattr(settings, "ML_MODE", "off")).lower().strip()

    def _get_labeler(self):
        if self._labeler is None:
            try:
                from services.ml_meta_labeler import MetaLabeler
                self._labeler = MetaLabeler()
            except Exception:
                self._labeler = None
        return self._labeler

    def evaluate_candidate(self, candidate: dict) -> dict:
        """candidate: dict с признаками (confidence/grade/side/regime/net_rr_*/
        entry_depth). Возвращает решение контроллера (всегда безопасное)."""
        mode = self._mode()
        if mode == "off":
            return dict(_PASSTHROUGH)

        # fail-open: считаем score, но любой сбой → passthrough
        try:
            labeler = self._get_labeler()
            ml_score = labeler.predict(candidate) if labeler else None
        except Exception:
            ml_score = None

        if ml_score is None:
            # модель не готова / ошибка → ведём себя как rule-based
            return {"mode": mode, "ml_score": None, "action": "passthrough",
                    "allow": True, "size_multiplier": 1.0, "reason": "ml_not_ready"}

        if mode == "shadow":
            return {"mode": "shadow", "ml_score": round(ml_score, 4), "action": "log_only",
                    "allow": True, "size_multiplier": 1.0, "reason": "shadow_logged"}

        if mode == "advisory":
            min_score = float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.45))
            recommend = "take" if ml_score >= min_score else "skip"
            return {"mode": "advisory", "ml_score": round(ml_score, 4), "action": "advise",
                    "allow": True, "size_multiplier": 1.0, "recommend": recommend,
                    "reason": f"advisory_{recommend}"}

        if mode == "full_auto":
            min_score = float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.45))
            if ml_score < min_score:
                return {"mode": "full_auto", "ml_score": round(ml_score, 4), "action": "block",
                        "allow": False, "size_multiplier": 0.0,
                        "reason": f"ml_score_below_min:{ml_score:.3f}<{min_score}"}
            # размер в guardrails: линейно от ml_score, кэп [min,max]
            s_min = float(getattr(settings, "ML_SIZE_MULT_MIN", 0.7))
            s_max = float(getattr(settings, "ML_SIZE_MULT_MAX", 1.25))
            # 0.45→s_min, 0.85+→s_max
            span = max(0.85 - min_score, 1e-6)
            frac = max(0.0, min(1.0, (ml_score - min_score) / span))
            size_mult = round(s_min + (s_max - s_min) * frac, 3)
            return {"mode": "full_auto", "ml_score": round(ml_score, 4), "action": "size",
                    "allow": True, "size_multiplier": size_mult,
                    "reason": f"ml_score_ok:{ml_score:.3f}"}

        # неизвестный режим → безопасно
        return dict(_PASSTHROUGH)
