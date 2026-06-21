"""Meta-labeler (Слой 1 ML) — НАСТОЯЩАЯ обученная модель, не эвристика.

Предсказывает P(сделка прибыльна) по признакам кандидата, обучаясь на
trade_outcomes.jsonl. Подход López de Prado (meta-labeling): не предсказываем
цену — предсказываем исход сетапа.

Дизайн-инварианты:
- fail-open: нет модели / мало данных / ошибка → predict() возвращает None,
  и вызывающий код работает как раньше (rule-based). ML НИКОГДА не на крит-пути.
- мало данных (< ML_MIN_TRAIN_SAMPLES) → модель не обучается, статус честный.
- валидация time-aware (хронологический сплит), без утечки будущего.
- модель и метрики персистятся (joblib + json) на тот же диск, что и датасет.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from services.ml_features import FEATURE_NAMES, row_to_features, row_to_label


class MetaLabeler:
    def __init__(self, dataset_path: str | Path | None = None):
        from services.ml_trade_logger import MLTradeLogger
        self.dataset_path = Path(dataset_path) if dataset_path else MLTradeLogger().path
        base = self.dataset_path.parent
        self.model_path = base / "meta_labeler.pkl"
        self.meta_path = base / "meta_labeler.json"
        self._model = None  # лениво загружается

    # ── данные ────────────────────────────────────────────────────────────────
    def _load_rows(self) -> list[dict]:
        if not self.dataset_path.exists():
            return []
        rows: list[dict] = []
        with self.dataset_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        # хронологический порядок для time-aware сплита
        rows.sort(key=lambda r: str(r.get("closed_at") or r.get("created_at") or ""))
        return rows

    def _xy(self, rows: list[dict], label_kind: str):
        X, y = [], []
        for r in rows:
            lbl = row_to_label(r, label_kind)
            if lbl is None:
                continue
            X.append(row_to_features(r))
            y.append(int(lbl))
        return X, y

    # ── обучение ──────────────────────────────────────────────────────────────
    def train(self) -> dict:
        label_kind = str(getattr(settings, "ML_LABEL_KIND", "is_win"))
        min_samples = int(getattr(settings, "ML_MIN_TRAIN_SAMPLES", 150))

        rows = self._load_rows()
        X, y = self._xy(rows, label_kind)
        n = len(y)

        if n < min_samples:
            return {
                "status": "insufficient_data",
                "samples": n,
                "needed": min_samples,
                "message": f"Нужно ≥{min_samples} размеченных сделок для обучения (есть {n}).",
            }
        if len(set(y)) < 2:
            return {"status": "single_class", "samples": n,
                    "message": "В данных только один класс (все win или все loss) — модель не обучается."}

        try:
            import numpy as np
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline
            from sklearn.metrics import roc_auc_score, accuracy_score
        except Exception as exc:
            return {"status": "sklearn_unavailable", "error": f"{type(exc).__name__}: {exc}"}

        Xa, ya = np.array(X, dtype=float), np.array(y, dtype=int)

        # time-aware сплит: последние 30% — тест (имитация будущего)
        cut = max(int(n * 0.7), n - 60)
        cut = min(cut, n - 1)
        Xtr, Xte, ytr, yte = Xa[:cut], Xa[cut:], ya[:cut], ya[cut:]

        def _make():
            return Pipeline([
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
            ])

        metrics = {"val_auc": None, "val_acc": None, "val_n": int(len(yte))}
        try:
            if len(set(ytr.tolist())) >= 2 and len(yte) >= 5 and len(set(yte.tolist())) >= 2:
                m = _make().fit(Xtr, ytr)
                proba = m.predict_proba(Xte)[:, 1]
                metrics["val_auc"] = round(float(roc_auc_score(yte, proba)), 4)
                metrics["val_acc"] = round(float(accuracy_score(yte, (proba >= 0.5).astype(int))), 4)
        except Exception as exc:
            metrics["val_error"] = f"{type(exc).__name__}: {exc}"

        # финальная модель — на ВСЕХ данных (после валидации)
        model = _make().fit(Xa, ya)

        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import joblib
            joblib.dump(model, self.model_path)
        except Exception as exc:
            return {"status": "save_failed", "error": f"{type(exc).__name__}: {exc}"}

        meta = {
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "samples": n,
            "positives": int(ya.sum()),
            "win_rate": round(float(ya.mean()) * 100, 2),
            "label_kind": label_kind,
            "features": FEATURE_NAMES,
            "metrics": metrics,
            "model": "LogisticRegression+StandardScaler",
        }
        try:
            self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        self._model = model
        return {"status": "trained", **meta}

    # ── предсказание (fail-open) ──────────────────────────────────────────────
    def _ensure_model(self):
        if self._model is not None:
            return self._model
        if not self.model_path.exists():
            return None
        try:
            import joblib
            self._model = joblib.load(self.model_path)
        except Exception:
            self._model = None
        return self._model

    def predict(self, candidate: dict) -> float | None:
        """P(win) ∈ [0,1] для кандидата. None — если модели нет / ошибка."""
        model = self._ensure_model()
        if model is None:
            return None
        try:
            import numpy as np
            x = np.array([row_to_features(candidate)], dtype=float)
            return float(model.predict_proba(x)[0, 1])
        except Exception:
            return None

    # ── дескриптивный анализ фич (cheap test, до полного обучения) ─────────────
    def feature_analysis(self) -> dict:
        """НЕ обученная модель, а descriptive-анализ: какие фичи разделяют
        win/loss на накопленных сделках. Сила фичи = single-feature AUC (как ОДНА
        фича ранжирует win vs loss; 0.5 = не разделяет, >0.6 или <0.4 = несёт
        сигнал). Работает уже на малой выборке (честно, с оговоркой). Включает
        стакан (OBI/CVD/спред/стенки), RR, режим — отвечает «несёт ли стакан
        сигнал для НАШИХ сетапов» там, где он реально есть."""
        label_kind = str(getattr(settings, "ML_LABEL_KIND", "is_win"))
        rows = self._load_rows()
        X, y = self._xy(rows, label_kind)
        n = len(y)
        if n < 20 or len(set(y)) < 2:
            return {"status": "insufficient_data", "samples": n,
                    "message": "Нужно ≥20 размеченных сделок обоих классов."}
        try:
            import numpy as np
            from sklearn.metrics import roc_auc_score
        except Exception as exc:
            return {"status": "sklearn_unavailable", "error": f"{type(exc).__name__}: {exc}"}

        Xa = np.array(X, dtype=float)
        ya = np.array(y, dtype=int)
        feats = []
        for j, name in enumerate(FEATURE_NAMES):
            col = Xa[:, j]
            if np.all(col == col[0]):  # константа — не разделяет
                continue
            try:
                auc = float(roc_auc_score(ya, col))
            except Exception:
                continue
            feats.append({
                "feature": name,
                "single_auc": round(auc, 3),
                "separation": round(abs(auc - 0.5), 3),
                "mean_win": round(float(col[ya == 1].mean()), 4),
                "mean_loss": round(float(col[ya == 0].mean()), 4),
            })
        feats.sort(key=lambda f: f["separation"], reverse=True)
        return {
            "status": "ok",
            "samples": n,
            "win_rate": round(float(ya.mean()) * 100, 2),
            "label_kind": label_kind,
            "note": ("single_auc≈0.5 = фича не разделяет win/loss; >0.6 или <0.4 = "
                     "несёт сигнал. Выборка мала (≈85) → ДЕСКРИПТИВНО, не доказательство — "
                     "но показывает, на какие фичи опираться в мета-лейблере."),
            "features": feats,
        }

    # ── статус ────────────────────────────────────────────────────────────────
    def status(self) -> dict:
        meta = {}
        if self.meta_path.exists():
            try:
                meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        return {
            "model_exists": self.model_path.exists(),
            "dataset_path": str(self.dataset_path),
            "min_train_samples": int(getattr(settings, "ML_MIN_TRAIN_SAMPLES", 150)),
            "trained_at": meta.get("trained_at"),
            "samples": meta.get("samples"),
            "win_rate": meta.get("win_rate"),
            "metrics": meta.get("metrics"),
            "label_kind": meta.get("label_kind"),
        }
