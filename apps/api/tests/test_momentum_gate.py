"""Вход, когда импульс уже смотрит по тренду (#momentum-late-2026-09-12).

79 сделок с импульсом 15m по тренду: −43.4 USDT, стопов 46%; 60 нейтральных:
+5.8, стопов 30%. Разрыв в обеих половинах 45 дней и в обе стороны. Гейт — в
тени по умолчанию, вердикт пишется в план каждой сделки.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from core.config import settings
from services import momentum_gate
from services.decision_config import snapshot
from services.stop_loss_forensics import _CATEGORICAL

ROOT = Path(__file__).resolve().parents[3]


def _set(monkeypatch, value):
    monkeypatch.setattr(settings, "MOMENTUM_GATE_MODE", value, raising=False)


def test_momentum_along_the_trend_is_a_late_entry(monkeypatch):
    _set(monkeypatch, "shadow")
    up = momentum_gate.evaluate("mtf_trend_up_bullish_normal_structure_confirmed")
    down = momentum_gate.evaluate("mtf_trend_down_bearish_strong_structure_confirmed")
    assert up["aligned"] and up["would_block"]
    assert down["aligned"] and down["would_block"]


def test_neutral_and_counter_momentum_pass(monkeypatch):
    _set(monkeypatch, "enforce")
    for reason in ("mtf_trend_up_neutral_weak_structure_confirmed",
                   "mtf_trend_down_neutral_normal_structure_confirmed",
                   "mtf_trend_down_bullish_weak_structure_confirmed"):
        assert momentum_gate.evaluate(reason)["blocks"] is False, reason


def test_shadow_records_the_verdict_but_never_blocks(monkeypatch):
    _set(monkeypatch, "shadow")
    verdict = momentum_gate.evaluate("mtf_trend_up_bullish_strong_structure_confirmed")
    assert verdict["would_block"] is True and verdict["blocks"] is False


def test_enforce_blocks_only_aligned(monkeypatch):
    _set(monkeypatch, "enforce")
    assert momentum_gate.evaluate("mtf_trend_up_bullish_normal_structure_confirmed")["blocks"]


def test_other_engines_and_off_mode_are_left_alone(monkeypatch):
    _set(monkeypatch, "enforce")
    assert momentum_gate.evaluate("crt_bull_sweep_CRL") is None
    assert momentum_gate.evaluate("micro_scalp_long_support_flow") is None
    _set(monkeypatch, "off")
    assert momentum_gate.evaluate("mtf_trend_up_bullish_normal_structure_confirmed") is None


def test_it_starts_in_shadow_everywhere():
    assert settings.MOMENTUM_GATE_MODE == "shadow"
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "key: MOMENTUM_GATE_MODE\n        value: shadow" in blueprint
    assert snapshot(market_type="swap", fee_rate=0.0005)["guards"]["momentum_gate_mode"] == "shadow"


def test_the_loop_checks_it_and_records_it_in_the_plan():
    from workers.robot_loop import RobotLoop

    src = inspect.getsource(RobotLoop)
    assert "momentum_gate.evaluate(" in src
    assert '"momentum_gate": _momentum' in src
    assert 'decision="momentum_aligned_late_entry"' in src


def test_forensics_and_the_feed_know_the_verdict():
    assert any(path == "momentum_gate.would_block" for path, _ in _CATEGORICAL)
    page = (ROOT / "apps/web/app/intelligence/page.tsx").read_text(encoding="utf-8")
    assert '"momentum_aligned_late_entry"' in page
    assert "momentum_aligned_late_entry:" in page
