"""(#audit-2026-08-27) train() воронка (dropped/label_drop) раньше была
видна ТОЛЬКО в разовом ответе train() — status() читал только meta_path,
который пишется исключительно при status="trained". Любая другая попытка
(insufficient_data и т.п.) была невидима на постоянной карточке дашборда,
даже если суточный auto-retrain честно пытался обучаться каждый день.
"""
from __future__ import annotations

import json

from core.config import settings
from services.ml_meta_labeler import MetaLabeler


def test_train_persists_last_attempt_on_insufficient_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ML_MIN_TRAIN_SAMPLES", 150, raising=False)
    path = tmp_path / "trade_outcomes.jsonl"
    path.write_text(
        json.dumps({"regime": "trend_up_candidate", "closed_net_pnl": 1.0, "net_pnl_stop": -2.0}) + "\n",
        encoding="utf-8")

    labeler = MetaLabeler(dataset_path=path)
    result = labeler.train()

    assert result["status"] == "insufficient_data"
    assert labeler.last_attempt_path.exists()

    status = labeler.status()
    assert status["model_exists"] is False
    assert status["last_attempt"] is not None
    assert status["last_attempt"]["status"] == "insufficient_data"
    assert status["last_attempt"]["samples"] == 1
    assert status["last_attempt"]["needed"] == 150
    assert "attempted_at" in status["last_attempt"]


def test_train_persists_last_attempt_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ML_MIN_TRAIN_SAMPLES", 4, raising=False)
    monkeypatch.setattr(settings, "ML_LABEL_KIND", "is_win", raising=False)
    monkeypatch.setattr(settings, "TRADEABLE_REGIMES", "", raising=False)
    path = tmp_path / "trade_outcomes.jsonl"
    rows = [
        {"regime": "trend_up_candidate", "closed_net_pnl": 5.0 if i % 2 == 0 else -3.0}
        for i in range(8)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    labeler = MetaLabeler(dataset_path=path)
    result = labeler.train()

    assert result["status"] == "trained"
    status = labeler.status()
    assert status["model_exists"] is True
    assert status["last_attempt"]["status"] == "trained"
