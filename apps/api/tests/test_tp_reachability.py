"""Достижимость цели сделки (#tp-reachability-2026-08-03).

Замер по 342 закрытым (`/analytics/mfe-mae`):

    scalp: avg_mfe 0.391%   при SCALP_TARGET_PCT = 0.8

TP1 стоит вдвое дальше, чем цена в этом режиме ходит. До цели не доходят ~82%
скальпов (тайм-стоп 24, безубыток-замок 30, flow-выход 11 из 79).

Само недостижение подстраховано выходами. Ломает другое: `net_rr_tp1` и
`net_rr_tp2` считаются ОТ этой цели, и гейт `min_rr_tp2 = 1.3` пропускает
сделку по геометрии, которой не существует.
"""
from __future__ import annotations

import json

import pytest

from core.config import settings
from services import tp_reachability as tr


@pytest.fixture(autouse=True)
def _clear_cache():
    tr._CACHE["ts"] = 0.0
    tr._CACHE["by_key"] = {}
    tr._CACHE["by_regime"] = {}
    yield
    tr._CACHE["ts"] = 0.0


def _seed(monkeypatch, tmp_path, rows):
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    class _Logger:
        def __init__(self):
            self.path = str(path)

    monkeypatch.setattr("services.ml_trade_logger.MLTradeLogger", _Logger)


def _scalp_rows(n=40, mfe=0.391, symbol="SOL/USDT"):
    return [
        {"symbol": symbol, "regime": "scalp", "lifecycle": {"mfe_pct": mfe}}
        for _ in range(n)
    ]


# ── воспроизведение боевого случая ──────────────────────────────────────────
def test_scalp_target_is_twice_the_typical_move(monkeypatch, tmp_path):
    """Ровно те цифры из замера: цель 0.8 при медианном ходе 0.391."""
    _seed(monkeypatch, tmp_path, _scalp_rows())
    monkeypatch.setattr(settings, "TP_REACH_MODE", "enforce", raising=False)
    monkeypatch.setattr(settings, "TP_REACH_MAX_RATIO", 1.5, raising=False)

    out = tr.evaluate(symbol="SOL/USDT", regime="scalp", tp1_dist_pct=0.8)

    assert out.median_mfe_pct == pytest.approx(0.391)
    assert out.reach_ratio == pytest.approx(2.046, abs=0.01)
    assert out.allowed is False
    assert out.reason.startswith("tp1_beyond_typical_move")


def test_target_inside_the_typical_move_passes(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, _scalp_rows())
    monkeypatch.setattr(settings, "TP_REACH_MODE", "enforce", raising=False)

    out = tr.evaluate(symbol="SOL/USDT", regime="scalp", tp1_dist_pct=0.4)
    assert out.allowed is True
    assert out.reason == "within_typical_move"


# ── предохранители ──────────────────────────────────────────────────────────
def test_small_sample_does_not_block(monkeypatch, tmp_path):
    """Медиана по трём сделкам — не медиана."""
    _seed(monkeypatch, tmp_path, _scalp_rows(n=3))
    monkeypatch.setattr(settings, "TP_REACH_MODE", "enforce", raising=False)

    out = tr.evaluate(symbol="SOL/USDT", regime="scalp", tp1_dist_pct=0.8)
    assert out.allowed is True
    assert out.reason.startswith("sample_too_small")


def test_shadow_mode_blocks_nothing(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, _scalp_rows())
    monkeypatch.setattr(settings, "TP_REACH_MODE", "shadow", raising=False)

    out = tr.evaluate(symbol="SOL/USDT", regime="scalp", tp1_dist_pct=0.8)
    assert out.allowed is True
    assert out.reason == "mode_shadow"
    # Но измерение всё равно записано — иначе наблюдать нечего.
    assert out.reach_ratio == pytest.approx(2.046, abs=0.01)


def test_no_data_passes(monkeypatch, tmp_path):
    """Отсутствие статистики не должно выглядеть как «цель недостижима»."""
    _seed(monkeypatch, tmp_path, [])
    monkeypatch.setattr(settings, "TP_REACH_MODE", "enforce", raising=False)

    out = tr.evaluate(symbol="NEW/USDT", regime="scalp", tp1_dist_pct=0.8)
    assert out.allowed is True
    assert out.evaluated is False


def test_missing_tp_distance_passes(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, _scalp_rows())
    monkeypatch.setattr(settings, "TP_REACH_MODE", "enforce", raising=False)

    assert tr.evaluate(symbol="SOL/USDT", regime="scalp", tp1_dist_pct=None).allowed
    assert tr.evaluate(symbol="SOL/USDT", regime="scalp", tp1_dist_pct=0.0).allowed


# ── откуда берётся медиана ──────────────────────────────────────────────────
def test_falls_back_to_regime_when_symbol_is_thin(monkeypatch, tmp_path):
    """У нового символа своей статистики нет — берём режим целиком."""
    rows = _scalp_rows(n=40, symbol="SOL/USDT") + _scalp_rows(n=2, symbol="NEW/USDT")
    _seed(monkeypatch, tmp_path, rows)
    monkeypatch.setattr(settings, "TP_REACH_MODE", "enforce", raising=False)

    out = tr.evaluate(symbol="NEW/USDT", regime="scalp", tp1_dist_pct=0.8)
    assert out.source == "regime"
    assert out.allowed is False


def test_median_not_mean(monkeypatch, tmp_path):
    """MFE имеет длинный правый хвост (max 9.16 при среднем 0.816) —
    среднее тянется вверх выбросами и завышало бы «типичный ход»."""
    rows = _scalp_rows(n=39, mfe=0.4) + [
        {"symbol": "SOL/USDT", "regime": "scalp", "lifecycle": {"mfe_pct": 9.16}}
    ]
    _seed(monkeypatch, tmp_path, rows)
    monkeypatch.setattr(settings, "TP_REACH_MODE", "enforce", raising=False)

    out = tr.evaluate(symbol="SOL/USDT", regime="scalp", tp1_dist_pct=0.8)
    assert out.median_mfe_pct == pytest.approx(0.4)
    assert out.allowed is False


def test_negative_and_zero_mfe_ignored(monkeypatch, tmp_path):
    """Сделка без хода в свою сторону не говорит ничего о достижимости цели."""
    rows = _scalp_rows(n=40, mfe=0.4) + [
        {"symbol": "SOL/USDT", "regime": "scalp", "lifecycle": {"mfe_pct": 0.0}}
        for _ in range(20)
    ]
    _seed(monkeypatch, tmp_path, rows)
    monkeypatch.setattr(settings, "TP_REACH_MODE", "enforce", raising=False)

    out = tr.evaluate(symbol="SOL/USDT", regime="scalp", tp1_dist_pct=0.8)
    assert out.sample == 40
    assert out.median_mfe_pct == pytest.approx(0.4)
