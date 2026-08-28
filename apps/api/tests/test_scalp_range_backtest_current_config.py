"""Вкладка "Скальп / рэйндж" backtest-тула (#audit-2026-08-28).

Владелец заметил, что вкладка выглядит сломанной — карточка "Текущий конфиг"
всегда показывала "—" независимо от объёма данных. Причина: `build()`
(scalp/range профиль) никогда не возвращал `current_rank`/`current_total_pct`
(добавлены только в `build_trend()` в #replay-fidelity-2026-08-03, но не
сюда), и вдобавок хардкод-сетка перебора не включала боевые значения
render.yaml (`SCALP_BREAKEVEN_ARM_PCT=0.25`, `SCALP_TIME_STOP_MIN=60.0`) —
даже добавив поля, совпадение искать было не с чем.

Отдельно: `REGIME_MODES["range"] = ("range",)` никогда не совпадал ни с одной
реально сохранённой строкой — robot_loop.py всегда проставляет
`trade_mode="scalp"` и для regime="range", и для regime="scalp".
"""
from __future__ import annotations

import json

import pytest

from core.config import settings
from services import exit_replay as er


def _scalp_rows(n: int = 12):
    """Синтетические scalp/range-сделки с ненулевой траекторией."""
    out = []
    for i in range(n):
        traj = [[0, 0.0], [30, 0.3], [60, 0.5], [90, 0.2]]
        out.append({
            "trade_mode": "scalp" if i % 2 == 0 else "range",
            "result_pct": 0.3,
            "closed_net_pnl": 0.3,
            "net_pnl_stop": -0.4,
            "symbol": "ADA/USDT",
            "signal_id": i,
            "lifecycle": {"mfe_pct": 0.5, "entry_price": 100.0, "traj": traj},
        })
    return out


@pytest.fixture()
def dataset(tmp_path, monkeypatch):
    path = tmp_path / "trade_outcomes.jsonl"

    class _Logger:
        def __init__(self):
            self.path = str(path)

    monkeypatch.setattr("services.ml_trade_logger.MLTradeLogger", _Logger)
    with path.open("w", encoding="utf-8") as f:
        for r in _scalp_rows():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def test_build_returns_current_rank_and_total_pct_fields(dataset):
    """Регрессия: build() обязан вернуть те же ключи, что build_trend() —
    иначе фронт (backtest/page.tsx) читает undefined и рисует "—"."""
    result = er.build()
    assert result["status"] == "ok"
    assert "current_rank" in result
    assert "current_total_pct" in result
    assert "variants_count" in result
    assert result["variants_count"] == len(result["variants"])


def test_build_finds_current_config_when_it_matches_live_settings(dataset, monkeypatch):
    """Живой render.yaml даёт arm=0.25 / time_stop=60.0 — значений, которых не
    было в исходной хардкод-сетке [0.3,0.5,0.7] / [45.0,90.0,None]. Сетка
    обязана включать боевой конфиг, иначе current_row всегда None."""
    monkeypatch.setattr(settings, "SCALP_BREAKEVEN_ARM_PCT", 0.25, raising=False)
    monkeypatch.setattr(settings, "SCALP_BREAKEVEN_GIVEBACK_SHARE", 0.6, raising=False)
    monkeypatch.setattr(settings, "SCALP_TIME_STOP_MIN", 60.0, raising=False)
    monkeypatch.setattr(settings, "SCALP_TIME_STOP_ENABLED", True, raising=False)

    result = er.build()
    assert result["current_rank"] is not None, "боевой конфиг должен быть найден в сетке"
    assert result["current_total_pct"] is not None
    assert 1 <= result["current_rank"] <= result["variants_count"]


def test_regime_modes_range_matches_actually_stored_trade_mode():
    """(#audit-2026-08-28) trade_mode="range" никогда не пишется в датасет
    (robot_loop.py проставляет "scalp" для обоих regime="range"/"scalp") —
    REGIME_MODES["range"] обязан совпадать со "scalp", иначе
    walk_forward(regime="range") всегда получает 0 сделок."""
    assert "scalp" in er.REGIME_MODES["range"]
