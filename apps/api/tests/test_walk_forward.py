"""Walk-forward: честная out-of-sample оценка подбора (#walk-forward-2026-07-27).

`/ml/exit-replay` подбирает и оценивает на ОДНОЙ выборке. Это in-sample, и его
лидер систематически красивее правды: перебор сотни вариантов по нескольким
сотням сделок почти гарантированно находит комбинацию, обслуживающую пару
удачных исходов.

Walk-forward отвечает на единственный вопрос, который делает подбор
осмысленным: работал бы найденный конфиг на данных, которых он не видел.
"""
from __future__ import annotations

import json
import time

import pytest

from core.config import settings
from services import exit_replay
from services.walkforward_monitor import history


# Своя тысяча идентификаторов на режим. Детерминированно и без hash(): у строк
# он не стабилен между запусками, и набор получал бы новые id каждый прогон.
_MODE_ID_BASE: dict[str, int] = {}


def _rows(n: int, mode: str = "trend"):
    """Синтетические сделки: половина едет в плюс, половина сразу в минус."""
    out = []
    for i in range(n):
        if i % 2 == 0:
            traj = [[0, 0.0], [60, 0.5], [120, 1.2], [180, 0.6], [240, 0.2]]
            result = 0.2
        else:
            traj = [[0, 0.0], [60, -0.3], [120, -0.7], [180, -1.0]]
            result = -1.0
        out.append({
            "trade_mode": mode,
            "result_pct": result,
            "symbol": "ADA/USDT",
            # (#replay-durable-source-2026-09-05) Идентификаторы уникальны по
            # всему набору: источники сливаются по `signal_id`, и одинаковые
            # id у трендового и скальп-набора схлопывали бы их друг в друга. В
            # бою так не бывает — у сигнала один id и один режим.
            "signal_id": _MODE_ID_BASE.setdefault(mode, len(_MODE_ID_BASE) * 10_000) + i,
            "lifecycle": {"mfe_pct": max(p[1] for p in traj), "traj": traj},
        })
    return out


@pytest.fixture()
def dataset(tmp_path, monkeypatch):
    path = tmp_path / "trade_outcomes.jsonl"

    class _Logger:
        def __init__(self):
            self.path = str(path)

    monkeypatch.setattr("services.ml_trade_logger.MLTradeLogger", _Logger)
    return path


def _write(path, rows):
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_tiny_sample_refuses_to_produce_a_verdict(dataset):
    """Честнее отказаться, чем выдать вердикт на пяти сделках."""
    _write(dataset, _rows(5))

    res = exit_replay.walk_forward(regime="trend", folds=4)

    assert res["status"] == "insufficient_data"
    assert res["trades"] == 5
    assert "нужно минимум" in res["message"]


def test_out_of_sample_estimate_is_produced(dataset):
    _write(dataset, _rows(60))

    res = exit_replay.walk_forward(regime="trend", folds=4)

    assert res["status"] == "ok"
    assert res["trades"] == 60
    assert res["folds_scored"] >= 1
    assert res["oos_edge_pct"] == pytest.approx(
        res["oos_total_pct"] - res["oos_current_pct"], abs=1e-6
    )


def test_parameters_are_never_chosen_on_the_data_they_are_scored_on(dataset):
    """Ключевой инвариант метода: обучающая часть строго предшествует тестовой."""
    _write(dataset, _rows(60))

    res = exit_replay.walk_forward(regime="trend", folds=4)
    scored = [s for s in res["steps"] if not s["skipped"]]

    assert scored, "должен быть хотя бы один оценённый фолд"
    seen = 0
    for step in scored:
        assert step["train_size"] >= seen, "обучающая часть только растёт"
        assert step["train_size"] + step["test_size"] <= res["trades"]
        seen = step["train_size"]


def test_regimes_are_separated(dataset):
    """Оптимум скальпа ничего не говорит о тренде — выборки не смешиваются."""
    _write(dataset, _rows(30, mode="trend") + _rows(30, mode="scalp"))

    trend = exit_replay.walk_forward(regime="trend", folds=3)
    scalp = exit_replay.walk_forward(regime="scalp", folds=3)

    assert trend["trades"] == 30
    assert scalp["trades"] == 30
    assert set(trend["current_config"]) != set(scalp["current_config"])


def test_verdict_calls_out_an_unstable_optimum(dataset):
    """Если каждый фолд просит своё — оптимума нет, и это должно быть сказано."""
    _write(dataset, _rows(60))
    res = exit_replay.walk_forward(regime="trend", folds=4)

    assert isinstance(res["verdict"], str) and res["verdict"]
    if res["oos_edge_pct"] <= 0:
        assert "подгонк" in res["verdict"] or "не бьёт" in res["verdict"]


def test_history_reports_drift_of_the_chosen_optimum(tmp_path, monkeypatch):
    """Ряд прогонов отвечает на то, на что одиночный не может: держится ли выбор."""
    path = tmp_path / "walkforward.jsonl"
    monkeypatch.setattr(settings, "WALKFORWARD_LOG_PATH", str(path), raising=False)

    now = time.time()
    with path.open("w", encoding="utf-8") as f:
        for i in range(6):
            # Каждый прогон выбирает СВОИ параметры — признак шума.
            f.write(json.dumps({
                "ts": now - i * 86400, "at": f"2026-07-{20 + i}T00:00:00Z",
                "r": {"trend": {"status": "ok", "trades": 100, "edge": 0.1,
                                "won": 2, "scored": 4, "uniq": 4,
                                "last_pick": {"be_arm_pct": 0.35 + i * 0.05}}},
            }) + "\n")

    res = history()

    assert res["status"] == "ok"
    assert res["stability"]["trend"]["distinct_picks"] == 6
    assert "плавает" in res["stability"]["trend"]["verdict"]


def test_history_confirms_a_stable_optimum(tmp_path, monkeypatch):
    path = tmp_path / "walkforward.jsonl"
    monkeypatch.setattr(settings, "WALKFORWARD_LOG_PATH", str(path), raising=False)

    now = time.time()
    with path.open("w", encoding="utf-8") as f:
        for i in range(6):
            f.write(json.dumps({
                "ts": now - i * 86400, "at": f"2026-07-{20 + i}T00:00:00Z",
                "r": {"trend": {"status": "ok", "trades": 100, "edge": 0.4,
                                "won": 4, "scored": 4, "uniq": 1,
                                "last_pick": {"be_arm_pct": 1.0}}},
            }) + "\n")

    res = history()

    assert res["stability"]["trend"]["distinct_picks"] == 1
    assert "стоит на месте" in res["stability"]["trend"]["verdict"]
    assert res["stability"]["trend"]["positive_edge_runs"] == 6
