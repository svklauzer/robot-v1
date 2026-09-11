"""Фиксация половины на TP1 выключена, отбор входов — нет (#tp1-partial-off-2026-09-12).

При стопе остатка на самом TP1 половина на TP1 ничего не защищает: возврат к TP1
закрывает всю позицию там же, бегун без фиксации едет целиком. Гейт смешанной
награды при входе был завязан на тот же флаг и выключился бы вместе с ним —
здесь проверяется, что он остался.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.config import settings

ROOT = Path(__file__).resolve().parents[3]


def test_the_partial_is_off_and_the_lock_is_on_everywhere():
    assert settings.TP1_PARTIAL_ENABLED is False
    assert float(settings.POST_TP1_LOCK_FRAC) == 1.0
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "key: TP1_PARTIAL_ENABLED\n        value: \"false\"" in blueprint


def test_the_entry_gate_does_not_follow_the_exit_switch(monkeypatch):
    """Смесь TP1 и TP2 по-прежнему судит вход при выключенной фиксации."""
    import inspect
    from services import trade_plan

    src = inspect.getsource(trade_plan)
    gate = src.split("# (#tp1-partial-off-2026-09-12) Гейт больше не зависит", 1)[1][:600]
    assert "TP1_PARTIAL_ENABLED" not in gate
    assert "MIN_NET_RR_BLENDED_TP1_SHARE" in gate


def test_the_snapshot_records_the_switch():
    from services.decision_config import snapshot

    assert snapshot(market_type="swap", fee_rate=0.0005)["exit"]["tp1_partial_enabled"] is False
