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
from services.ml_features import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    is_phantom_row,
    row_to_features,
    row_to_label,
)


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
        """Строки датасета. Битые строки СЧИТАЮТСЯ, а не глотаются молча.

        Раньше `except: continue` делал повреждённую строку невидимой: датасет
        мог быть наполовину нечитаем, а train() показывал бы только «мало
        данных». Счётчик кладём в self._load_report, чтобы train() отдал его
        наружу — иначе о порче узнать неоткуда.
        """
        self._load_report = {"lines": 0, "parsed": 0, "bad_json": 0, "not_dict": 0,
                             "bad_samples": []}
        if not self.dataset_path.exists():
            return []
        rows: list[dict] = []
        with self.dataset_path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                self._load_report["lines"] += 1
                try:
                    obj = json.loads(line)
                except Exception as exc:  # noqa: BLE001
                    self._load_report["bad_json"] += 1
                    if len(self._load_report["bad_samples"]) < 5:
                        self._load_report["bad_samples"].append(
                            {"line": lineno, "error": str(exc)[:120],
                             "preview": line.strip()[:120]}
                        )
                    continue
                if not isinstance(obj, dict):
                    self._load_report["not_dict"] += 1
                    continue
                self._load_report["parsed"] += 1
                rows.append(obj)
        # хронологический порядок для time-aware сплита
        rows.sort(key=lambda r: str(r.get("closed_at") or r.get("created_at") or ""))
        return rows

    def _usable_rows(self, rows: list[dict]) -> tuple[list[dict], dict]:
        """(#ml-rework-2026-07-28) Отсев данных из системы, которой больше нет.

        Три причины выбросить строку — все обнаружены замером, а не гипотезой:

        1. **Фантомный филл.** 13 строк из 287 записаны с результатом выше
           достигнутого пика. Метка у них положительная там, где рынок дал
           минус: модель училась бы, что сетап приносит +8.18 USDT.

        2. **Отключённый режим.** trend_up/trend_down — 154 записи из 287.
           Мы их больше не торгуем; предсказывать исход в мире, которого нет,
           бессмысленно, а весов на себя они оттягивают половину.

        3. **Возраст.** Exit-политика за месяц менялась несколько раз
           (MIN_PROTECTIVE 1.80→0.40, замок безубытка выключен). Метка — это
           исход ПОД ТОГДАШНЮЮ логику ведения; старая строка отвечает на другой
           вопрос. Окно по времени — самый дешёвый способ не смешивать эпохи.
        """
        from datetime import datetime, timedelta, timezone

        allowed = {
            r.strip() for r in str(getattr(settings, "TRADEABLE_REGIMES", "")).split(",")
            if r.strip()
        }
        window_days = float(getattr(settings, "ML_TRAIN_WINDOW_DAYS", 45))
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

        kept: list[dict] = []
        dropped = {"phantom": 0, "regime_off": 0, "too_old": 0}
        for r in rows:
            if is_phantom_row(r):
                dropped["phantom"] += 1
                continue
            if allowed and str(r.get("regime") or "") not in allowed:
                dropped["regime_off"] += 1
                continue
            raw = str(r.get("closed_at") or r.get("created_at") or "")
            if raw:
                try:
                    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        dropped["too_old"] += 1
                        continue
                except ValueError:
                    pass
            kept.append(r)
        return kept, dropped

    def _xy(self, rows: list[dict], label_kind: str):
        """Фичи и метки. Строки без метки СЧИТАЮТСЯ по причине.

        `beats_costs` требует и закрытый P&L, и плановый риск (`net_pnl_stop`).
        Раньше строка без любого из них исчезала молча, и «411 сделок» могли
        превратиться в 40 без единого следа — отсюда «данных мало» при полном
        датасете. Причину кладём в self._label_report.
        """
        min_r = float(getattr(settings, "ML_LABEL_MIN_R", 0.3))
        report = {"no_label": 0, "no_pnl": 0, "no_risk": 0}
        X, y = [], []
        for r in rows:
            lbl = row_to_label(r, label_kind, min_r=min_r)
            if lbl is None:
                report["no_label"] += 1
                if r.get("closed_net_pnl") is None:
                    report["no_pnl"] += 1
                else:
                    # P&L есть, значит метку убил отсутствующий плановый риск
                    report["no_risk"] += 1
                continue
            X.append(row_to_features(r))
            y.append(int(lbl))
        self._label_report = report
        return X, y

    # ── обучение ──────────────────────────────────────────────────────────────
    def train(self) -> dict:
        label_kind = str(getattr(settings, "ML_LABEL_KIND", "is_win"))
        min_samples = int(getattr(settings, "ML_MIN_TRAIN_SAMPLES", 150))

        raw_rows = self._load_rows()
        rows, dropped = self._usable_rows(raw_rows)
        X, y = self._xy(rows, label_kind)
        n = len(y)

        if n < min_samples:
            _load = getattr(self, "_load_report", {})
            _lbl = getattr(self, "_label_report", {})
            return {
                "status": "insufficient_data",
                "samples": n,
                "needed": min_samples,
                "rows_total": len(raw_rows),
                "dropped": dropped,
                # Полная воронка: файл → распарсено → пережило фильтры → размечено.
                # Без неё «мало данных» не отличить от «датасет побит» и от
                # «метка не считается».
                "dataset": _load,
                "label_drop": _lbl,
                "label_kind": label_kind,
                "message": (
                    f"Нужно ≥{min_samples} размеченных сделок (есть {n} из "
                    f"{len(raw_rows)} прочитанных). Файл: строк "
                    f"{_load.get('lines', '?')}, битых JSON {_load.get('bad_json', 0)}. "
                    f"Фильтры: фантомных {dropped['phantom']}, режим off "
                    f"{dropped['regime_off']}, старше окна {dropped['too_old']}. "
                    f"Без метки ({label_kind}): {_lbl.get('no_label', 0)} "
                    f"(нет P&L {_lbl.get('no_pnl', 0)}, нет планового риска "
                    f"{_lbl.get('no_risk', 0)})."
                ),
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

        # (#ml-honest-metrics-2026-08-03) Три числа, без которых метрики выше
        # вводят в заблуждение. Замер 03.08: val_auc 0.7588 / val_acc 0.80 при
        # live AUC 0.5702 — расхождение объяснялось целиком тем, что ниже.
        positives = int(ya.sum())
        base_rate = float(1.0 - ya.mean())
        epv = round(positives / max(len(FEATURE_NAMES), 1), 2)
        val_positives = int(yte.sum())
        metrics.update({
            # Точность бессмысленна, если она равна доле большинства: модель
            # «всегда нет» даёт столько же. При 19.84% положительных это 0.80 —
            # ровно то, что показывалось как достижение.
            "baseline_acc": round(base_rate, 4),
            "acc_beats_baseline": (
                None if metrics["val_acc"] is None
                else round(float(metrics["val_acc"]) - base_rate, 4)
            ),
            # Событий на признак. Меньше 10 — модель запоминает выборку.
            # 51 положительный на 16 признаков = 3.19.
            "events_per_feature": epv,
            "features_used": len(FEATURE_NAMES),
            "positives": positives,
            # Положительных В ВАЛИДАЦИИ. AUC на десятке событий имеет
            # доверительный интервал порядка ±0.15 — то есть 0.76 и 0.57
            # неразличимы, и «валидация лучше боя» может быть просто шумом.
            "val_positives": val_positives,
            "auc_is_reliable": bool(val_positives >= 30),
        })
        warnings = []
        if epv < 10:
            warnings.append(
                f"events_per_feature={epv} (<10): признаков больше, чем выдерживает "
                f"{positives} положительных примеров — переобучение по построению"
            )
        if val_positives < 30:
            warnings.append(
                f"val_positives={val_positives} (<30): доверительный интервал AUC "
                f"порядка ±0.15, число не отличимо от случайного"
            )
        if metrics.get("acc_beats_baseline") is not None and metrics["acc_beats_baseline"] <= 0.02:
            warnings.append(
                f"val_acc не превышает долю большинства ({base_rate:.2%}) — "
                f"метрика ничего не измеряет, смотрите AUC"
            )
        metrics["warnings"] = warnings

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
            "label_min_r": float(getattr(settings, "ML_LABEL_MIN_R", 0.3)),
            "features": FEATURE_NAMES,
            # (#ml-rework-2026-07-28) Версия контракта фич. Модель, обученная на
            # другом наборе, несовместима и по длине вектора, и по смыслу —
            # предсказывать по ней молча нельзя.
            "feature_version": FEATURE_VERSION,
            # Что и почему выброшено из датасета. Без этого «samples: 120»
            # выглядит как потеря данных, хотя это отсев исходов от логики,
            # которой больше нет.
            "rows_total": len(raw_rows),
            "dropped": dropped,
            "train_window_days": float(getattr(settings, "ML_TRAIN_WINDOW_DAYS", 45)),
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

        # (#ml-rework-2026-07-28) Модель, обученная на прежнем наборе фич,
        # молча предсказывать не имеет права: вектор изменил и длину, и смысл
        # позиций. Расхождение train/serve — самый тихий класс ML-багов, он не
        # падает, а просто выдаёт правдоподобный мусор.
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if int(meta.get("feature_version") or 1) != int(FEATURE_VERSION):
                return None
            if list(meta.get("features") or []) != FEATURE_NAMES:
                return None
        except Exception:  # noqa: BLE001 — нет метаданных = модель доверия не заслуживает
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
