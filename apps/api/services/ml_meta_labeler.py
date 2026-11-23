"""Meta-labeler (Слой 1 ML). fail-open; time-aware валидация; персист модели/метрик.

#ml-audit-2026-11-23:
 - обучение на logged_features (вектор, виденный live-скором) -> нет train/serve skew;
 - экономическая метка net_r (поддерживается ml_features.row_to_label);
 - live-валидация (AUC скора против метки + экономический сплит) пишется в metrics
   и служит гейтом полномочий (resolve_effective_mode);
 - drift_baseline (квантили train-фич) для PSI-монитора;
 - порог сдвига позитивов 2.0 (был 2.58 — 2.28 молчал).
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from services.ml_features import (
    FEATURE_NAMES, FEATURE_VERSION, is_phantom_row, row_to_features,
    row_to_label, logged_features, total_net_pnl,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _build_estimator():
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    rf = RandomForestClassifier(
        n_estimators=400,
        max_depth=3,            # было 5
        min_samples_leaf=8,     # было 2 — лист не должен запоминать 2 сделки
        class_weight="balanced_subsample",
        max_features="sqrt",
        random_state=42, n_jobs=1,
    )
    pipeline = Pipeline([("scaler", StandardScaler()), ("clf", rf)])
    return CalibratedClassifierCV(estimator=pipeline, method="sigmoid", cv=3)


def _walk_forward_oos(Xa, ya, n_folds: int = 4, start_frac: float = 0.4,
                      embargo: int = 5, block: int = 20, n_boot: int = 500,
                      seed: int = 42) -> dict:
    import numpy as np
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
    n = len(ya)
    start = int(n * start_frac)
    if n - start < n_folds * 10:
        return {"wf_status": "too_few_rows", "wf_folds": 0}
    edges = [start + (n - start) * k // n_folds for k in range(n_folds + 1)]
    oy, op, ob, fold_aucs = [], [], [], []
    used = 0
    for k in range(n_folds):
        te0, te1 = edges[k], edges[k + 1]
        tr_end = max(te0 - embargo, 0)
        ytr, yte = ya[:tr_end], ya[te0:te1]
        if len(yte) == 0 or len(set(ytr.tolist())) < 2:
            continue
        model = _build_estimator().fit(Xa[:tr_end], ytr)
        p = model.predict_proba(Xa[te0:te1])[:, 1]
        oy.extend(yte.tolist()); op.extend(p.tolist())
        ob.extend([float(ytr.mean())] * len(yte)); used += 1
        if len(set(yte.tolist())) == 2:
            fold_aucs.append(round(float(roc_auc_score(yte, p)), 4))
    out: dict = {"wf_folds": used, "wf_fold_aucs": fold_aucs, "wf_embargo": embargo}
    if not oy:
        out["wf_status"] = "no_usable_folds"; return out
    y_arr, p_arr, b_arr = np.array(oy), np.array(op), np.array(ob)
    m = len(y_arr)
    out["wf_oos_n"] = int(m)
    out["wf_oos_positives"] = int(y_arr.sum())
    out["wf_oos_positive_rate"] = round(float(y_arr.mean()), 4)
    out["wf_brier"] = round(float(brier_score_loss(y_arr, p_arr)), 5)
    out["wf_brier_baseline"] = round(float(np.mean((b_arr - y_arr) ** 2)), 5)
    if len(set(oy)) < 2:
        out["wf_status"] = "single_class_oos"; return out
    out["wf_status"] = "ok"
    out["wf_auc"] = round(float(roc_auc_score(y_arr, p_arr)), 4)
    out["wf_pr_auc"] = round(float(average_precision_score(y_arr, p_arr)), 4)
    rng = np.random.default_rng(seed)
    n_blocks = max(1, int(math.ceil(m / block)))
    boots = []
    for _ in range(n_boot):
        starts = rng.integers(0, max(m - block, 0) + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, min(s + block, m)) for s in starts])[:m]
        yb = y_arr[idx]
        if yb.min() == yb.max():
            continue
        boots.append(roc_auc_score(yb, p_arr[idx]))
    out["wf_auc_ci95"] = ([round(float(x), 4) for x in np.percentile(boots, [2.5, 97.5])]
                          if len(boots) >= 50 else None)
    return out


class MetaLabeler:
    def __init__(self, dataset_path: str | Path | None = None):
        from services.ml_trade_logger import MLTradeLogger
        self.dataset_path = Path(dataset_path) if dataset_path else MLTradeLogger().path
        base = self.dataset_path.parent
        self.model_path = base / "meta_labeler.pkl"
        self.meta_path = base / "meta_labeler.json"
        self.last_attempt_path = base / "meta_labeler_last_attempt.json"
        self._model = None
        self._model_mtime: int | None = None

    # ── данные ───────────────────────────────────────────────────────────────
    def _load_rows(self) -> list[dict]:
        self._load_report = {"lines": 0, "parsed": 0, "bad_json": 0, "not_dict": 0,
                             "duplicates": 0, "bad_samples": []}
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
                            {"line": lineno, "error": str(exc)[:120], "preview": line.strip()[:120]})
                    continue
                if not isinstance(obj, dict):
                    self._load_report["not_dict"] += 1
                    continue
                self._load_report["parsed"] += 1
                rows.append(obj)
        by_id: dict = {}
        no_id: list[dict] = []
        for r in rows:
            sid = r.get("signal_id")
            if sid is None:
                no_id.append(r); continue
            if sid in by_id:
                self._load_report["duplicates"] += 1
            by_id[sid] = r
        rows = list(by_id.values()) + no_id
        rows.sort(key=lambda r: _parse_ts(r.get("closed_at") or r.get("created_at")) or _EPOCH)
        return rows

    def _usable_rows(self, rows: list[dict]) -> tuple[list[dict], dict]:
        from datetime import timedelta
        allowed = {r.strip() for r in str(getattr(settings, "TRADEABLE_REGIMES", "")).split(",") if r.strip()}
        window_days = float(getattr(settings, "ML_TRAIN_WINDOW_DAYS", 45))
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        kept: list[dict] = []
        dropped = {"phantom": 0, "regime_off": 0, "too_old": 0}
        for r in rows:
            if is_phantom_row(r):
                dropped["phantom"] += 1; continue
            if allowed and str(r.get("regime") or "") not in allowed:
                dropped["regime_off"] += 1; continue
            ts = _parse_ts(r.get("closed_at") or r.get("created_at"))
            if ts and ts < cutoff:
                dropped["too_old"] += 1; continue
            kept.append(r)
        return kept, dropped

    def _xy(self, rows: list[dict], label_kind: str):
        min_r = float(getattr(settings, "ML_LABEL_MIN_R", 0.3))
        report = {"no_label": 0, "no_pnl": 0, "no_risk": 0, "logged": 0, "recomputed": 0}
        X, y = [], []
        for r in rows:
            lbl = row_to_label(r, label_kind, min_r=min_r)
            if lbl is None:
                report["no_label"] += 1
                if r.get("closed_net_pnl") is None:
                    report["no_pnl"] += 1
                else:
                    report["no_risk"] += 1
                continue
            # (#ml-audit) приоритет — вектор, виденный live-скором на входе.
            vec = logged_features(r)
            if vec is not None:
                report["logged"] += 1
            else:
                vec = row_to_features(r)
                report["recomputed"] += 1
            X.append(vec)
            y.append(int(lbl))
        self._label_report = report
        return X, y

    def _live_validation(self, rows: list[dict], label_kind: str) -> dict:
        """Честная живая метрика: AUC сохранённого скора против метки + экономический
        сплит. Только строки текущей версии фич (иначе смешиваем версии модели)."""
        scored = []
        for r in rows:
            fv = r.get("ml_feature_version")
            ml = (r.get("plan") or {}).get("ml") or {}
            fv = fv if fv is not None else ml.get("feature_version")
            fv = r.get("ml_feature_version")
            ml = (r.get("plan") or {}).get("ml") or {}
            fv = fv if fv is not None else ml.get("feature_version")
            # (#ml-live-version) Строка БЕЗ версии — не наша модель. Смешивать
            # версии нельзя: live_auc обязан мерить ТЕКУЩУЮ модель, иначе он
            # усредняет десятки исторических и всегда даёт ~0.5.
            try:
                if fv is None or int(fv) != FEATURE_VERSION:
                    continue
            except (TypeError, ValueError):
                continue
            sc = r.get("ml_score")
            if sc is None:
                sc = ml.get("ml_score")
            if sc is None:
                continue
            lbl = row_to_label(r, label_kind, min_r=float(getattr(settings, "ML_LABEL_MIN_R", 0.3)))
            pnl = total_net_pnl(r)
            if lbl is None or pnl is None:
                continue
            try:
                scored.append((float(sc), int(lbl), float(pnl)))
            except (TypeError, ValueError):
                continue
        out = {"live_n": len(scored), "live_auc": None, "live_econ_diff": None}
        if len(scored) < 50:
            return out
        try:
            from sklearn.metrics import roc_auc_score
            ys = [s[1] for s in scored]; ps = [s[0] for s in scored]
            if len(set(ys)) == 2:
                out["live_auc"] = round(float(roc_auc_score(ys, ps)), 4)
            scored.sort(key=lambda t: t[0])
            half = len(scored) // 2
            bot = [t[2] for t in scored[:half]]; top = [t[2] for t in scored[half:]]
            if bot and top:
                out["live_econ_diff"] = round(sum(top) / len(top) - sum(bot) / len(bot), 4)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _save_last_attempt(self, result: dict) -> None:
        try:
            payload = {**result, "attempted_at": datetime.now(timezone.utc).isoformat()}
            self.last_attempt_path.parent.mkdir(parents=True, exist_ok=True)
            self.last_attempt_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ── обучение ─────────────────────────────────────────────────────────────
    def train(self) -> dict:
        result = self._train_inner()
        self._save_last_attempt(result)
        return result

    def _train_inner(self) -> dict:
        label_kind = str(getattr(settings, "ML_LABEL_KIND", "net_r"))
        min_samples = int(getattr(settings, "ML_MIN_TRAIN_SAMPLES", 150))
        raw_rows = self._load_rows()
        rows, dropped = self._usable_rows(raw_rows)
        X, y = self._xy(rows, label_kind)
        n = len(y)
        if n < min_samples:
            _load = getattr(self, "_load_report", {})
            _lbl = getattr(self, "_label_report", {})
            return {"status": "insufficient_data", "samples": n, "needed": min_samples,
                    "rows_total": len(raw_rows), "dropped": dropped, "dataset": _load,
                    "label_drop": _lbl, "label_kind": label_kind,
                    "message": f"Нужно ≥{min_samples} размеченных сделок (есть {n})."}
        if len(set(y)) < 2:
            return {"status": "single_class", "samples": n, "message": "Один класс — модель не обучается."}
        try:
            import numpy as np
            from sklearn.metrics import accuracy_score, average_precision_score, brier_score_loss, roc_auc_score
        except Exception as exc:
            return {"status": "sklearn_unavailable", "error": f"{type(exc).__name__}: {exc}"}
        Xa, ya = np.array(X, dtype=float), np.array(y, dtype=int)
        cut = max(10, min(int(n * 0.80), n - 30))
        Xtr, Xte, ytr, yte = Xa[:cut], Xa[cut:], ya[:cut], ya[cut:]
        metrics = {"val_auc": None, "val_acc": None, "val_n": int(len(yte))}
        proba = None
        try:
            if len(set(ytr.tolist())) >= 2 and len(yte) >= 5 and len(set(yte.tolist())) >= 2:
                m = _build_estimator().fit(Xtr, ytr)
                proba = m.predict_proba(Xte)[:, 1]
                metrics["val_auc"] = round(float(roc_auc_score(yte, proba)), 4)
                metrics["val_acc"] = round(float(accuracy_score(yte, (proba >= 0.5).astype(int))), 4)
            else:
                metrics["val_error"] = "В валидации один класс"
        except Exception as exc:
            metrics["val_error"] = f"{type(exc).__name__}: {exc}"
        positives = int(ya.sum()); train_positives = int(ytr.sum()); val_positives = int(yte.sum())
        train_rate = train_positives / max(len(ytr), 1); val_rate = val_positives / max(len(yte), 1)
        val_majority = max(val_rate, 1.0 - val_rate) if len(yte) else None
        n_feat = max(len(FEATURE_NAMES), 1)
        epv = round(positives / n_feat, 2); train_epv = round(train_positives / n_feat, 2)
        metrics.update({
            "baseline_acc": None if val_majority is None else round(val_majority, 4),
            "acc_beats_baseline": (None if metrics["val_acc"] is None or val_majority is None
                                   else round(float(metrics["val_acc"]) - val_majority, 4)),
            "train_positive_rate": round(train_rate, 4), "val_positive_rate": round(val_rate, 4),
            "events_per_feature": epv, "train_events_per_feature": train_epv,
            "features_used": len(FEATURE_NAMES), "positives": positives,
            "train_positives": train_positives, "val_positives": val_positives,
        })
        if proba is not None:
            try:
                metrics["val_pr_auc"] = round(float(average_precision_score(yte, proba)), 4)
                metrics["val_brier"] = round(float(brier_score_loss(yte, proba)), 5)
                metrics["val_brier_baseline"] = round(float(np.mean((train_rate - yte) ** 2)), 5)
            except Exception:
                pass
        try:
            wf = _walk_forward_oos(Xa, ya)
        except Exception as exc:
            wf = {"wf_status": "error", "wf_error": f"{type(exc).__name__}: {exc}"}
        metrics.update(wf)
        metrics["auc_is_reliable"] = bool(int(wf.get("wf_oos_positives", 0) or 0) >= 30 and train_epv >= 10)
        # (#ml-audit) живая валидация — гейт полномочий, не wf.
        metrics.update(self._live_validation(rows, label_kind))
        warnings = []
        if epv < 10:
            warnings.append(f"events_per_feature={epv} (<10): переобучение по построению")
        if metrics.get("acc_beats_baseline") is not None and metrics["acc_beats_baseline"] <= 0.02:
            warnings.append("val_acc не превышает долю большинства — смотрите AUC")
        if len(ytr) and len(yte):
            pooled = (train_positives + val_positives) / (len(ytr) + len(yte))
            se = math.sqrt(pooled * (1 - pooled) * (1 / len(ytr) + 1 / len(yte))) if 0 < pooled < 1 else 0.0
            z = (val_rate - train_rate) / se if se > 0 else 0.0
            metrics["positive_rate_shift_z"] = round(z, 2)
            if abs(z) >= float(getattr(settings, "ML_POSITIVE_SHIFT_Z_WARN", 2.0)):
                warnings.append(f"доля позитивов сменилась: {train_rate:.1%} → {val_rate:.1%} (z={z:.1f})")
        if int(wf.get("wf_oos_positives", 0) or 0) < 30:
            warnings.append("walk-forward: <30 позитивов в OOS — AUC нечитаем")
        ci = wf.get("wf_auc_ci95")
        if ci and ci[0] <= 0.5:
            warnings.append(f"wf AUC ДИ [{ci[0]}, {ci[1]}] включает 0.5")
        if metrics.get("live_auc") is not None and metrics["live_auc"] < float(getattr(settings, "ML_PROMOTION_MIN_LIVE_AUC", 0.55)):
            warnings.append(f"live_auc={metrics['live_auc']} < порога полномочий — ML остаётся в shadow")
        metrics["warnings"] = warnings
        model = _build_estimator().fit(Xa, ya)
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import joblib
            tmp_model = self.model_path.with_name(self.model_path.name + ".tmp")
            joblib.dump(model, tmp_model)
            os.replace(tmp_model, self.model_path)
        except Exception as exc:
            return {"status": "save_failed", "error": f"{type(exc).__name__}: {exc}"}
        meta = {
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "samples": n, "positives": int(ya.sum()), "win_rate": round(float(ya.mean()) * 100, 2),
            "label_kind": label_kind, "label_min_r": float(getattr(settings, "ML_LABEL_MIN_R", 0.3)),
            "features": FEATURE_NAMES, "feature_version": FEATURE_VERSION,
            "rows_total": len(raw_rows), "dropped": dropped,
            "train_window_days": float(getattr(settings, "ML_TRAIN_WINDOW_DAYS", 45)),
            "metrics": metrics,
            "model": "CalibratedClassifierCV(StandardScaler+RandomForest, sigmoid, cv=3)",
        }
        # (#ml-audit) baseline для PSI-дрейфа.
        if bool(getattr(settings, "ML_DRIFT_ENABLED", True)):
            try:
                meta["drift_baseline"] = {
                    "feature_version": FEATURE_VERSION,
                    "q10": np.quantile(Xa, 0.10, axis=0).round(5).tolist(),
                    "q90": np.quantile(Xa, 0.90, axis=0).round(5).tolist(),
                }
            except Exception:
                pass
        try:
            tmp_meta = self.meta_path.with_name(self.meta_path.name + ".tmp")
            tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_meta, self.meta_path)
        except Exception:
            pass
        self._model = model
        try:
            self._model_mtime = self.model_path.stat().st_mtime_ns
        except OSError:
            self._model_mtime = None
        return {"status": "trained", **meta}

    # ── предсказание ─────────────────────────────────────────────────────────
    def _ensure_model(self):
        try:
            current = self.model_path.stat().st_mtime_ns if self.model_path.exists() else None
        except OSError:
            current = None
        if self._model is not None:
            if self._model_mtime is None or current is None or current == self._model_mtime:
                return self._model
            self._model = None; self._model_mtime = None
        if current is None:
            return None
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if int(meta.get("feature_version") or 1) != int(FEATURE_VERSION):
                return None
            if list(meta.get("features") or []) != FEATURE_NAMES:
                return None
        except Exception:
            return None
        try:
            import joblib
            self._model = joblib.load(self.model_path)
            self._model_mtime = current
        except Exception:
            self._model = None; self._model_mtime = None
        return self._model

    def predict(self, candidate: dict) -> float | None:
        model = self._ensure_model()
        if model is None:
            return None
        try:
            import numpy as np
            x = np.array([row_to_features(candidate)], dtype=float)
            return float(model.predict_proba(x)[0, 1])
        except Exception:
            return None

    def drift_baseline(self) -> dict | None:
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            b = meta.get("drift_baseline")
            if isinstance(b, dict) and int(b.get("feature_version") or 0) == FEATURE_VERSION:
                return b
        except Exception:
            pass
        return None

    def feature_analysis(self) -> dict:
        label_kind = str(getattr(settings, "ML_LABEL_KIND", "net_r"))
        rows, _ = self._usable_rows(self._load_rows())
        X, y = self._xy(rows, label_kind)
        n = len(y)
        if n < 20 or len(set(y)) < 2:
            return {"status": "insufficient_data", "samples": n}
        try:
            import numpy as np
            from sklearn.metrics import roc_auc_score
        except Exception as exc:
            return {"status": "sklearn_unavailable", "error": f"{type(exc).__name__}: {exc}"}
        Xa = np.array(X, dtype=float); ya = np.array(y, dtype=int)
        feats = []
        for j, name in enumerate(FEATURE_NAMES):
            col = Xa[:, j]
            if np.all(col == col[0]):
                continue
            try:
                auc = float(roc_auc_score(ya, col))
            except Exception:
                continue
            feats.append({"feature": name, "single_auc": round(auc, 3),
                          "separation": round(abs(auc - 0.5), 3),
                          "mean_win": round(float(col[ya == 1].mean()), 4),
                          "mean_loss": round(float(col[ya == 0].mean()), 4)})
        feats.sort(key=lambda f: f["separation"], reverse=True)
        return {"status": "ok", "samples": n, "win_rate": round(float(ya.mean()) * 100, 2),
                "label_kind": label_kind, "features": feats}

    def status(self) -> dict:
        meta = {}
        if self.meta_path.exists():
            try:
                meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        last_attempt = {}
        if self.last_attempt_path.exists():
            try:
                last_attempt = json.loads(self.last_attempt_path.read_text(encoding="utf-8"))
            except Exception:
                last_attempt = {}
        return {"model_exists": self.model_path.exists(), "dataset_path": str(self.dataset_path),
                "min_train_samples": int(getattr(settings, "ML_MIN_TRAIN_SAMPLES", 150)),
                "trained_at": meta.get("trained_at"), "samples": meta.get("samples"),
                "win_rate": meta.get("win_rate"), "metrics": meta.get("metrics"),
                "label_kind": meta.get("label_kind"), "feature_version": meta.get("feature_version"),
                "last_attempt": last_attempt or None}
