"""Признаки живого кандидата должны совпадать с признаками строки обучения."""
from services.ml_features import row_to_features


def _train_row():
    return {"confidence": 66.0, "net_rr_tp1": 1.0, "net_rr_tp2": 2.3, "stop_price": 99.0,
            "lifecycle": {"entry_price": 100.0}, "entry_depth": {"spread_pct": 0.02},
            "opened_at": "2026-10-16T04:28:29",
            "rationale": "intelligence_crt_sweep_confirmed", "trade_mode": "trend"}


def test_candidate_with_rationale_matches_training_row():
    train = _train_row()
    cand = {k: v for k, v in train.items() if k not in ("rationale", "trade_mode")}
    cand["rationale"] = "intelligence_crt_sweep_confirmed"
    cand["trade_mode"] = "trend"
    assert row_to_features(cand) == row_to_features(train)
    assert row_to_features(train)[-1] == 1.0                # is_crt


def test_candidate_without_rationale_loses_scalp_and_crt():
    cand = {k: v for k, v in _train_row().items() if k not in ("rationale", "trade_mode")}
    assert row_to_features(cand)[-2:] == [0.0, 0.0]          # то, что было до правки
