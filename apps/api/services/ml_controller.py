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


def resolve_effective_mode(configured: str, metrics: dict | None) -> tuple[str, str | None]:
    """(эффективный режим, причина понижения | None).

    advisory / full_auto сохраняются, только если ВСЕ условия выполнены:
      1. есть валидационная метрика val_auc;
      2. val_auc >= ML_MIN_AUC_FOR_AUTO;
      3. events_per_feature >= 3;
      4. метрика не помечена обучением как ненадёжная (auc_is_reliable is not False);
      5. если посчитан walk-forward — нижняя граница 95% ДИ его AUC > 0.5.
    Иначе — shadow. off / shadow возвращаются как есть. Повышение — только вручную.
    """
    mode = str(configured or "off").lower().strip()
    if mode not in ("advisory", "full_auto"):
        return mode, None
    if not bool(getattr(settings, "ML_AUTO_DEMOTE_ENABLED", True)):
        return mode, None

    m = metrics or {}
    auc_raw = m.get("val_auc")
    if auc_raw is None:
        return "shadow", ("нет валидационной метрики (val_auc) — качество модели не доказано, "
                          "полномочия отозваны до shadow")
    try:
        auc = float(auc_raw)
    except (TypeError, ValueError):
        return "shadow", f"val_auc={auc_raw!r} не число — полномочия отозваны до shadow"

    min_auc = float(getattr(settings, "ML_MIN_AUC_FOR_AUTO", 0.55))
    reasons: list[str] = []
    if auc < min_auc:
        reasons.append(f"val_auc={auc:.4f} < {min_auc}")
    try:
        epv = float(m.get("events_per_feature") or 0.0)
    except (TypeError, ValueError):
        epv = 0.0
    if epv < 3.0:
        reasons.append(f"events_per_feature={epv} < 3.0")
    if m.get("auc_is_reliable") is False:
        reasons.append(
            f"обучение пометило метрику НЕНАДЁЖНОЙ (events_per_feature={m.get('events_per_feature')}, "
            f"val_positives={m.get('val_positives')}, wf_oos_positives={m.get('wf_oos_positives')})"
        )
    ci = m.get("wf_auc_ci95")
    if isinstance(ci, (list, tuple)) and len(ci) == 2 and ci[0] is not None:
        try:
            if float(ci[0]) <= 0.5:
                reasons.append(f"нижняя граница 95% ДИ walk-forward AUC = {ci[0]} ≤ 0.5")
        except (TypeError, ValueError):
            pass
    if reasons:
        return "shadow", "; ".join(reasons) + " — полномочия отозваны до shadow"
    return mode, None


class MLController:
    def __init__(self):
        self._labeler = None

    def _metrics_block(self) -> dict | None:
        """Блок metrics последнего обучения. None — прочитать не удалось."""
        try:
            labeler = self._get_labeler()
            st = labeler.status() if labeler else {}
            return st.get("metrics") or {}
        except Exception:  # noqa: BLE001 — сбой чтения = нет доказанного качества
            return None

    def _mode(self) -> str:
        """Эффективный режим с АВТО-ДЕГРАДАЦИЕЙ по качеству модели.

        (#ml-auto-demote-2026-07-27) Модель влияет на деньги, только пока доказывает
        качество; провал качества отзывает полномочия сам.

        (#authority-2026-10-10) Решение вынесено в resolve_effective_mode() — её же
        использует хаб. Раньше копий было две, и в контроллере проверка «метрика
        ненадёжна» (#ml-unreliable-auc-2026-08-21) стояла ПОСЛЕ безусловного
        `return mode` и не выполнялась никогда, а в хабе её не было вовсе.

        Понижение до shadow безопасно для торговли (shadow ничего не блокирует), так
        что «метрику прочитать не удалось» = понижение: fail-open означает «ML не
        мешает сделкам», а не «ML сохраняет право блокировать без доказанного качества».
        """
        configured = str(getattr(settings, "ML_MODE", "off")).lower().strip()
        mode, reason = resolve_effective_mode(configured, self._metrics_block())
        self._last_demote_reason = reason
        return mode

    def _metric(self, key: str):
        """Значение из блока metrics последнего обучения. None — нет метрики."""
        try:
            labeler = self._get_labeler()
            # СИНХРОНИЗАЦИЯ: Исправляем обращение к несуществующему методу metadata() -> используем status()
            st = labeler.status() if labeler else {}
            metrics = st.get("metrics") or {}
            if key in metrics:
                return metrics.get(key)
            return st.get(key)
        except Exception:  # noqa: BLE001 — метрика не критична, fail-open
            return None

    def _val_auc(self) -> float | None:
        """Валидационный AUC последнего обучения. None — метрики нет (fail-open)."""
        try:
            labeler = self._get_labeler()
            st = labeler.status() if labeler else {}
            metrics = st.get("metrics") or {}
            if metrics.get("val_auc") is not None:
                return float(metrics["val_auc"])
        except Exception:  # noqa: BLE001 — метрика не критична
            return None
        return None

    def effective_mode(self) -> str:
        """Режим ПОСЛЕ проверки полномочий — публичный вход для тех, кто решает,
        вмешиваться ли.

        (#ml-blend-contract-2026-09-07) Нужен потому, что смешивание ML в
        уверенность жило в robot_loop мимо контроллера и мимо режима вообще.
        Спрашивать `settings.ML_MODE` напрямую нельзя: `_mode()` понижает
        advisory/full_auto до shadow при слабом или протухшем AUC, и обход этого
        понижения вернул бы ровно ту дыру, ради которой оно заведено.
        """
        return self._mode()

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
