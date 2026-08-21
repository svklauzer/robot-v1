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
        """Эффективный режим с АВТО-ДЕГРАДАЦИЕЙ по качеству модели.

        (#ml-auto-demote-2026-07-27) Модель имеет право влиять на деньги только
        пока доказывает качество. Ретрейн 16.07 дал val AUC 0.5067 — монетку, —
        и переключение в shadow пришлось делать руками. Это неправильно: провал
        качества обязан отзывать полномочия САМ.

        Правило: full_auto/advisory требуют val AUC >= ML_MIN_AUC_FOR_AUTO.
        Ниже — режим автоматически понижается до shadow (score виден рядом с
        сигналами, на сделки не влияет). Обратное повышение — только вручную:
        модель возвращает полномочия, доказав качество на ретрейне, а не
        случайным скачком метрики.

        Fail-open сохранён: если метрику прочитать не удалось, режим не трогаем —
        ML не должен ломать торговлю из-за проблемы с чтением файла.
        """
        mode = str(getattr(settings, "ML_MODE", "off")).lower().strip()
        if mode not in ("advisory", "full_auto"):
            return mode
        if not bool(getattr(settings, "ML_AUTO_DEMOTE_ENABLED", True)):
            return mode

        auc = self._val_auc()
        if auc is None:
            return mode
        min_auc = float(getattr(settings, "ML_MIN_AUC_FOR_AUTO", 0.55))
        if auc < min_auc:
            self._last_demote_reason = (
                f"val_auc={auc:.4f} < {min_auc} — модель не подтверждает качество, "
                f"полномочия отозваны до shadow"
            )
            return "shadow"

        # (#ml-unreliable-auc-2026-08-21) Порога по AUC НЕДОСТАТОЧНО. Ретрейн
        # 21.08 дал val_auc 0.6612 — выше порога 0.55 — при 13 положительных в
        # валидации и 2.17 события на признак. Обучение само пометило метрику
        # `auc_is_reliable=false`: доверительный интервал ±0.15, то есть 0.66
        # неотличим от монетки. Переобученная модель на малой выборке даёт
        # ЗАВЫШЕННЫЙ AUC — ровно тот случай, когда старый гейт пропускал бы её
        # к деньгам. Полномочия требуют не «красивого числа», а числа, которому
        # можно верить.
        reliable = self._metric("auc_is_reliable")
        if reliable is False:
            epv = self._metric("events_per_feature")
            vpos = self._metric("val_positives")
            self._last_demote_reason = (
                f"val_auc={auc:.4f} формально выше {min_auc}, но обучение пометило "
                f"метрику НЕНАДЁЖНОЙ (events_per_feature={epv}, val_positives={vpos}) — "
                f"переобучение по построению, полномочия отозваны до shadow"
            )
            return "shadow"
        return mode

    def _metric(self, key: str):
        """Значение из блока metrics последнего обучения. None — нет метрики."""
        try:
            labeler = self._get_labeler()
            meta = getattr(labeler, "metadata", None)
            if callable(meta):
                meta = meta()
            if not isinstance(meta, dict):
                return None
            metrics = meta.get("metrics")
            if isinstance(metrics, dict) and key in metrics:
                return metrics.get(key)
            return meta.get(key)
        except Exception:  # noqa: BLE001 — метрика не критична, fail-open
            return None

    def _val_auc(self) -> float | None:
        """Валидационный AUC последнего обучения. None — метрики нет (fail-open)."""
        try:
            labeler = self._get_labeler()
            meta = getattr(labeler, "metadata", None)
            if callable(meta):
                meta = meta()
            if not isinstance(meta, dict):
                return None
            for key in ("val_auc", "auc_val", "validation_auc", "auc"):
                if meta.get(key) is not None:
                    return float(meta[key])
        except Exception:  # noqa: BLE001 — метрика не критична
            return None
        return None

    def health(self) -> dict:
        """Состояние ML-контура для /ml/status и отчётов."""
        configured = str(getattr(settings, "ML_MODE", "off")).lower().strip()
        effective = self._mode()
        auc = self._val_auc()
        return {
            "configured_mode": configured,
            "effective_mode": effective,
            "demoted": configured != effective,
            "demote_reason": getattr(self, "_last_demote_reason", None) if configured != effective else None,
            "val_auc": auc,
            "min_auc_for_auto": float(getattr(settings, "ML_MIN_AUC_FOR_AUTO", 0.55)),
            "auto_demote_enabled": bool(getattr(settings, "ML_AUTO_DEMOTE_ENABLED", True)),
            # Надёжность метрики видна рядом с самой метрикой: AUC выше порога
            # при auc_is_reliable=false — не допуск к деньгам, а повод ждать данных.
            "auc_is_reliable": self._metric("auc_is_reliable"),
            "events_per_feature": self._metric("events_per_feature"),
            "val_positives": self._metric("val_positives"),
        }

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
