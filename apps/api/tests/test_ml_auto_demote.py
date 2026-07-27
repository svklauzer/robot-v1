"""Авто-деградация ML по качеству модели (#ml-auto-demote-2026-07-27).

Запрос Капитана: «ML должна влиять только в full_auto, и автоматически
переключаться в наблюдение, если AUC падает».

Повод: ретрейн 16.07 дал val AUC 0.5067 — монетку, — и переключение в shadow
делалось руками. Провал качества обязан отзывать полномочия сам.
"""
import pytest

from core.config import settings
from services.ml_controller import MLController


def _controller(auc):
    c = MLController()
    c._val_auc = lambda: auc          # noqa: SLF001 — подменяем источник метрики
    return c


def test_full_auto_is_demoted_when_model_is_a_coin_flip(monkeypatch):
    monkeypatch.setattr(settings, "ML_MODE", "full_auto")
    assert _controller(0.5067)._mode() == "shadow"


def test_advisory_is_demoted_too(monkeypatch):
    monkeypatch.setattr(settings, "ML_MODE", "advisory")
    assert _controller(0.51)._mode() == "shadow"


def test_full_auto_kept_when_model_proves_itself(monkeypatch):
    monkeypatch.setattr(settings, "ML_MODE", "full_auto")
    assert _controller(0.62)._mode() == "full_auto"


def test_threshold_is_the_boundary(monkeypatch):
    monkeypatch.setattr(settings, "ML_MODE", "full_auto")
    thr = float(settings.ML_MIN_AUC_FOR_AUTO)
    assert _controller(thr)._mode() == "full_auto"
    assert _controller(thr - 0.001)._mode() == "shadow"


def test_missing_metric_does_not_change_mode(monkeypatch):
    """Fail-open: проблема с чтением метрики не должна ломать торговлю."""
    monkeypatch.setattr(settings, "ML_MODE", "full_auto")
    assert _controller(None)._mode() == "full_auto"


def test_shadow_and_off_are_untouched(monkeypatch):
    for mode in ("off", "shadow"):
        monkeypatch.setattr(settings, "ML_MODE", mode)
        assert _controller(0.30)._mode() == mode


def test_demotion_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "ML_MODE", "full_auto")
    monkeypatch.setattr(settings, "ML_AUTO_DEMOTE_ENABLED", False)
    assert _controller(0.40)._mode() == "full_auto"


def test_health_explains_the_demotion(monkeypatch):
    monkeypatch.setattr(settings, "ML_MODE", "full_auto")
    h = _controller(0.5067).health()

    assert h["configured_mode"] == "full_auto"
    assert h["effective_mode"] == "shadow"
    assert h["demoted"] is True
    assert "val_auc" in (h["demote_reason"] or "")
    assert h["val_auc"] == pytest.approx(0.5067)


def test_evaluate_respects_the_demoted_mode(monkeypatch):
    """Ключевое: деградация должна влиять на РЕШЕНИЕ, а не только на отчёт."""
    monkeypatch.setattr(settings, "ML_MODE", "full_auto")
    c = _controller(0.5067)
    c._get_labeler = lambda: type("L", (), {"predict": lambda self, x: 0.10})()  # noqa: SLF001

    out = c.evaluate_candidate({"confidence": 70})

    assert out["mode"] == "shadow"
    assert out["allow"] is True, "в shadow модель не имеет права блокировать сделку"
    assert out["size_multiplier"] == 1.0
