"""Сделка, дошедшая до TP2 через этап, остаётся дошедшей (#tp2-stage-2026-09-12).

С 12.09 правило «TP2 достигнут» на 92% пути не перехватывает этап TP2, и хвост
закрывается как `tp2_trail_stop` / `tp2_trail_giveback`. Метки ML, счётчик TP2
в аналитике и пауза перед повторным входом узнавали TP2 только по
`tp2_reached` — для обучения такие сделки стали бы «не дошедшими».
"""
from __future__ import annotations

import inspect

from services.close_reasons import TP2_STAGE_REASONS, reached_tp2
from services.reentry_cooldown import ReEntryCooldownGuard


def test_the_old_close_and_the_stage_closes_all_count():
    assert reached_tp2("tp2_reached")
    assert reached_tp2("tp2_trail_stop")
    assert reached_tp2("tp2_trail_giveback")


def test_a_trade_with_a_tp2_partial_reached_tp2_whatever_closed_it():
    assert reached_tp2("manual_close", {"tp2_partial": {"closed_qty": 1.0}})


def test_other_closes_did_not_reach_tp2():
    for reason in ("stop_loss", "breakeven_stop", "post_tp1_lock_stop", None):
        assert not reached_tp2(reason, {})


def test_the_stage_reasons_are_real_close_reasons():
    """Если этап переименует причину, правило должно это заметить."""
    src = inspect.getsource(__import__("services.signal_lifecycle", fromlist=["x"]))
    src += inspect.getsource(__import__("services.exit_policy", fromlist=["x"]))
    for reason in TP2_STAGE_REASONS:
        assert f'"{reason}"' in src, reason


def test_ml_labels_and_the_tp2_counter_use_the_rule():
    for module in ("services.ml_trade_logger", "services.trade_outcome_logger",
                   "services.ml_features", "routers.analytics"):
        src = inspect.getsource(__import__(module, fromlist=["x"]))
        assert "reached_tp2(" in src, module


def test_profitable_closes_after_tp1_and_tp2_get_the_short_pause():
    minutes = ReEntryCooldownGuard.COOLDOWN_MINUTES
    for reason in ("tp2_trail_stop", "tp2_trail_giveback", "post_tp1_lock_stop"):
        assert minutes[reason] == minutes["tp2_reached"], reason
