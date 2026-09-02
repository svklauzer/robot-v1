"""(#audit-2026-08-27) MLIntelligenceHub.health()/_mode() читали
`getattr(labeler, "metadata", None)` — метода/атрибута `metadata` на
MetaLabeler нет вообще (есть `.status()`), поэтому это всегда было `None`:
`/ml/status` (реальный эндпоинт дашборда, routers/ml.py) показывал
`model_exists=False` и пустые метрики ДАЖЕ КОГДА модель реально обучена, а
auto-demote по AUC в `_mode()` не срабатывал никогда, что бы ни намеряла
модель. Тесты ниже обучают крошечную модель через MetaLabeler и проверяют,
что health()/_mode() реально её видят.
"""
from __future__ import annotations

import json

import pytest

from core.config import settings
from services.ml_intelligence_hub import MLIntelligenceHub
from services.ml_meta_labeler import MetaLabeler


def _train_tiny_model(tmp_path, monkeypatch, *, val_auc_floor=None):
    monkeypatch.setattr(settings, "ML_MIN_TRAIN_SAMPLES", 4, raising=False)
    monkeypatch.setattr(settings, "ML_LABEL_KIND", "is_win", raising=False)
    monkeypatch.setattr(settings, "TRADEABLE_REGIMES", "", raising=False)
    monkeypatch.setattr(settings, "ML_TRAIN_WINDOW_DAYS", 3650, raising=False)

    path = tmp_path / "trade_outcomes.jsonl"
    rows = []
    for i in range(8):
        rows.append({
            "regime": "trend_up_candidate",
            "confidence": 60.0,
            "grade": "B",
            "side": "long",
            "net_rr_tp1": 0.5,
            "net_rr_tp2": 1.5,
            "closed_net_pnl": 5.0 if i % 2 == 0 else -3.0,
        })
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    labeler = MetaLabeler(dataset_path=path)
    result = labeler.train()
    assert result["status"] == "trained", result
    return labeler


def test_hub_health_reports_model_exists_when_actually_trained(tmp_path, monkeypatch):
    labeler = _train_tiny_model(tmp_path, monkeypatch)

    hub = MLIntelligenceHub()
    hub._meta_labeler = labeler  # inject the already-trained instance directly
    status = hub.health()

    assert status["model"]["model_exists"] is True
    assert status["model"]["trained_at"] is not None
    assert status["model"]["samples"] == 8


def test_hub_health_surfaces_last_attempt_when_untrained(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ML_MIN_TRAIN_SAMPLES", 150, raising=False)
    path = tmp_path / "trade_outcomes.jsonl"
    path.write_text("", encoding="utf-8")
    labeler = MetaLabeler(dataset_path=path)
    labeler.train()  # writes last_attempt with status=insufficient_data

    hub = MLIntelligenceHub()
    hub._meta_labeler = labeler
    status = hub.health()

    assert status["model"]["model_exists"] is False
    assert status["model"]["last_attempt"] is not None
    assert status["model"]["last_attempt"]["status"] == "insufficient_data"


def test_hub_health_nests_metrics_inside_model(tmp_path, monkeypatch):
    """(#ml-status-metrics-nesting-2026-09-02) app/ml/page.tsx reads
    `model?.metrics` (never the top-level `status.metrics` sibling) for
    "Валидация AUC"/"Валидация Acc"/"Событий на признак". Before this fix,
    health() only ever set metrics at the top level — those three rows showed
    "—" unconditionally, even right after a successful `train()`, while
    trained_at/samples/win_rate (which DO live inside model_info) displayed
    fine, making the card look half-broken on every real training run."""
    labeler = _train_tiny_model(tmp_path, monkeypatch)

    hub = MLIntelligenceHub()
    hub._meta_labeler = labeler
    status = hub.health()

    assert "metrics" in status["model"]
    assert status["model"]["metrics"] == status["metrics"]


def test_hub_auto_demote_reads_real_val_auc(tmp_path, monkeypatch):
    """_mode() должен реально видеть val_auc из status(), не всегда получать
    None из-за несуществующего .metadata."""
    labeler = _train_tiny_model(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "ML_MODE", "full_auto", raising=False)
    monkeypatch.setattr(settings, "ML_AUTO_DEMOTE_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "ML_MIN_AUC_FOR_AUTO", 2.0, raising=False)  # impossible bar -> forces demotion if AUC is read at all

    hub = MLIntelligenceHub()
    hub._meta_labeler = labeler
    st = labeler.status()
    val_auc = (st.get("metrics") or {}).get("val_auc")

    if val_auc is None:
        pytest.skip("tiny fixture produced no validation split (val_n too small) — nothing to demote on")

    assert hub._mode() == "shadow", "AUC below ML_MIN_AUC_FOR_AUTO must demote full_auto -> shadow"
