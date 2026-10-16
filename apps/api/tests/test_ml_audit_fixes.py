"""Регрессионные тесты правок аудита ML-контура (2026-10-10).

Каждый тест воспроизводит дефект, найденный в старом коде.
"""
import json
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.config import settings
from services import ml_features
from services.ml_controller import MLController, resolve_effective_mode
from services.ml_features import row_to_label, total_net_pnl
from services.ml_meta_labeler import MetaLabeler
from services.ml_outcome_stats import MLOutcomeStatsService, _rank_auc


def _rows(n=700, seed=7, head=0.25, tail=0.07):
    rnd = random.Random(seed)
    start = datetime.now(timezone.utc) - timedelta(days=150)
    out = []
    for i in range(n):
        ts = start + timedelta(hours=i * 5)
        p = tail if i >= int(n * 0.8) else head
        entry = 100 + rnd.random()
        risk = rnd.uniform(0.8, 1.2)
        beats = rnd.random() < p
        r = rnd.uniform(0.35, 1.5) if beats else rnd.uniform(-1.0, 0.25)
        out.append({
            "logged_at": ts.isoformat(), "signal_id": i + 1, "symbol": "ETH/USDT", "side": "long",
            "grade": rnd.choice(["A+", "A", "B", "C"]), "confidence": rnd.uniform(55, 85),
            "rationale": rnd.choice(["scalp x", "crt y", "range z"]), "status": "closed",
            "closed_reason": "scalp_time_stop", "regime": "range",
            "trade_mode": rnd.choice(["scalp", "crt", "range"]),
            "entry_depth": {"spread_pct": rnd.uniform(0.01, 0.1), "cvd_ratio": rnd.uniform(-1, 1), "cvd_trades": 50},
            "entry_zone": {"from": entry * 0.999, "to": entry * 1.001},
            "stop_price": entry * (1 - rnd.uniform(0.3, 1.5) / 100),
            "net_rr_tp1": rnd.uniform(0.8, 2.0), "net_rr_tp2": rnd.uniform(1.5, 3.5),
            "net_pnl_stop": -risk, "result_pct": r * 0.5, "closed_net_pnl": r * risk,
            "opened_at": ts.isoformat(), "created_at": ts.isoformat(),
            "closed_at": (ts + timedelta(hours=1)).isoformat(),
            "lifecycle": {"entry_price": entry, "mfe_pct": abs(r) * 0.6 + 0.1},
            "labels": {}, "ml_score": rnd.uniform(0.1, 0.7),
        })
    return out


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


@pytest.fixture()
def cfg(monkeypatch):
    for k, v in {"ML_LABEL_KIND": "beats_costs", "ML_MIN_TRAIN_SAMPLES": 200, "TRADEABLE_REGIMES": "",
                 "ML_TRAIN_WINDOW_DAYS": 200, "ML_LABEL_MIN_R": 0.3, "ML_AUTO_DEMOTE_ENABLED": True,
                 "ML_MIN_AUC_FOR_AUTO": 0.55}.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    return settings


# ── метка: две ноги ───────────────────────────────────────────────────────────
def test_label_counts_tp1_partial_leg():
    row = {"closed_net_pnl": 0.2, "net_pnl_stop": -1.0, "plan": {"tp1_partial": {"net_pnl": 0.5}}}
    assert total_net_pnl(row) == pytest.approx(0.7)
    assert row_to_label(row, "beats_costs", 0.3) == 1          # было 0: 0.2R < 0.3R
    flat = {"closed_net_pnl": 0.2, "net_pnl_stop": -1.0, "tp1_partial_net_pnl": 0.5}
    assert row_to_label(flat, "beats_costs", 0.3) == 1
    assert row_to_label({"closed_net_pnl": 0.2, "net_pnl_stop": -1.0}, "beats_costs", 0.3) == 0


def test_label_is_win_uses_total():
    row = {"closed_net_pnl": -0.1, "tp1_partial_net_pnl": 0.5}
    assert row_to_label(row, "is_win") == 1


# ── метрики обучения ──────────────────────────────────────────────────────────
def test_baseline_acc_is_computed_on_validation_slice(cfg, tmp_path):
    p = tmp_path / "trade_outcomes.jsonl"
    _write(p, _rows())
    r = MetaLabeler(dataset_path=p).train()
    assert r["status"] == "trained"
    m = r["metrics"]
    val_rate = m["val_positives"] / m["val_n"]
    assert m["baseline_acc"] == pytest.approx(max(val_rate, 1 - val_rate), abs=1e-4)
    assert m["acc_beats_baseline"] == pytest.approx(m["val_acc"] - m["baseline_acc"], abs=1e-4)
    # val_acc == «всегда негатив» → разница не может быть заметно положительной
    assert m["acc_beats_baseline"] <= 0.05
    assert "train_positive_rate" in m and "val_positive_rate" in m


def test_walk_forward_metrics_and_reliability(cfg, tmp_path):
    p = tmp_path / "trade_outcomes.jsonl"
    _write(p, _rows(seed=3, head=0.25, tail=0.25))
    m = MetaLabeler(dataset_path=p).train()["metrics"]
    assert m["wf_folds"] >= 3
    assert m["wf_oos_positives"] > 30
    assert m["wf_auc_ci95"] is not None and m["wf_auc_ci95"][0] <= m["wf_auc"] <= m["wf_auc_ci95"][1]
    # признаки не несут сигнала → ДИ обязан включать 0.5, а в warnings это видно
    assert m["wf_auc_ci95"][0] <= 0.5
    assert any("монетки" in w for w in m["warnings"])


def test_positive_rate_shift_is_warned(cfg, tmp_path):
    p = tmp_path / "trade_outcomes.jsonl"
    _write(p, _rows(head=0.25, tail=0.05))
    m = MetaLabeler(dataset_path=p).train()["metrics"]
    assert abs(m["positive_rate_shift_z"]) >= 2.58
    assert any("доля позитивов сменилась" in w for w in m["warnings"])


def test_duplicates_are_dropped_on_load(cfg, tmp_path):
    rows = _rows(300)
    p = tmp_path / "trade_outcomes.jsonl"
    _write(p, rows + rows[:50])
    lab = MetaLabeler(dataset_path=p)
    loaded = lab._load_rows()
    assert len(loaded) == 300
    assert lab._load_report["duplicates"] == 50


def test_stale_model_is_reloaded_after_retrain(cfg, tmp_path):
    p = tmp_path / "trade_outcomes.jsonl"
    _write(p, _rows(seed=1))
    MetaLabeler(dataset_path=p).train()
    old = MetaLabeler(dataset_path=p)
    cand = {"confidence": 70, "net_rr_tp1": 1.5, "net_rr_tp2": 2.5, "stop_price": 99,
            "entry_zone": {"from": 100, "to": 100}}
    before = old.predict(cand)
    _write(p, _rows(seed=99, head=0.5, tail=0.5))
    MetaLabeler(dataset_path=p).train()                       # ретрейн ДРУГИМ экземпляром
    fresh = MetaLabeler(dataset_path=p).predict(cand)
    assert old.predict(cand) == pytest.approx(fresh)
    assert fresh != pytest.approx(before)


# ── полномочия ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("metrics,expected", [
    ({"val_auc": 0.66, "events_per_feature": 6.0, "auc_is_reliable": False}, "shadow"),   # мёртвый код
    ({"val_auc": None}, "shadow"),
    ({"val_auc": 0.4937, "events_per_feature": 13.0, "auc_is_reliable": True}, "shadow"),
    ({"val_auc": 0.70, "events_per_feature": 13.0, "auc_is_reliable": True,
      "wf_auc_ci95": [0.48, 0.80]}, "shadow"),
    ({"val_auc": 0.70, "events_per_feature": 13.0, "auc_is_reliable": True,
      "wf_auc_ci95": [0.56, 0.80]}, "advisory"),
])
def test_effective_mode(cfg, monkeypatch, metrics, expected):
    monkeypatch.setattr(settings, "ML_MODE", "advisory", raising=False)
    assert resolve_effective_mode("advisory", metrics)[0] == expected
    c = MLController()
    c._labeler = SimpleNamespace(status=lambda: {"metrics": metrics})
    assert c.effective_mode() == expected


def test_off_and_shadow_untouched(cfg):
    assert resolve_effective_mode("off", None) == ("off", None)
    assert resolve_effective_mode("shadow", {}) == ("shadow", None)


# ── хаб ───────────────────────────────────────────────────────────────────────
def test_feature_contributions_work_for_calibrated_model(cfg, tmp_path):
    from services.ml_intelligence_hub import MLIntelligenceHub
    p = tmp_path / "trade_outcomes.jsonl"
    _write(p, _rows())
    lab = MetaLabeler(dataset_path=p)
    lab.train()
    hub = MLIntelligenceHub()
    contrib = hub._get_feature_contributions(lab, {})
    assert set(contrib) == set(ml_features.FEATURE_NAMES)
    assert sum(contrib.values()) == pytest.approx(1.0, abs=1e-6)


def test_drift_status_is_not_fake_ok():
    from services.ml_intelligence_hub import DriftMonitor
    st = DriftMonitor().status()
    assert st["status"] != "ok" and st["implemented"] is False


# ── статистика и AUC ──────────────────────────────────────────────────────────
def test_outcome_stats_counts_both_legs_and_caches(cfg, tmp_path):
    rows = [
        {"symbol": "A", "side": "long", "status": "closed", "grade": "A", "closed_net_pnl": -0.1,
         "tp1_partial_net_pnl": 0.5, "closed_total_cost": 0.01, "tp1_partial_total_cost": 0.02},
        {"symbol": "A", "side": "long", "status": "closed", "grade": "A", "closed_net_pnl": -0.3},
    ]
    p = tmp_path / "o.jsonl"
    _write(p, rows)
    svc = MLOutcomeStatsService(path=p)
    g = svc.summary()["groups"][0]
    assert g["wins"] == 1 and g["net_pnl"] == pytest.approx(0.1) and g["costs"] == pytest.approx(0.03)
    assert svc.grade_stats(min_count=1)["A"]["wins"] == 1
    first = svc._load_rows()
    # кэш: после дозаписи файл перечитывается, без изменений — нет
    assert len(svc._load_rows()) == len(first) == 2
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rows[1]) + "\n")
    assert len(svc._load_rows()) == 3


def test_rank_auc_handles_ties():
    assert _rank_auc([1, 0, 1, 0], [0.5, 0.5, 0.5, 0.5]) == pytest.approx(0.5)
    assert _rank_auc([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1]) == pytest.approx(1.0)
    assert _rank_auc([1, 1], [0.1, 0.2]) is None


# ── логгер ────────────────────────────────────────────────────────────────────
def _signal(i, closed_net, plan):
    return SimpleNamespace(
        id=i, bot_id=1, symbol="X", side="long", grade="A", confidence=70, rationale="scalp",
        status="closed", closed_reason="scalp_time_stop", entry_zone_json={"from": 1, "to": 1},
        stop_price=0.99, tp_json=None, qty=1, required_margin=1, net_rr_tp1=1.0, net_rr_tp2=2.0,
        net_pnl_tp1=1, net_pnl_tp2=2, net_pnl_stop=-1.0, result_pct=0.1, closed_exit_price=1,
        closed_net_pnl=closed_net, closed_total_cost=0.01, opened_at=None, closed_at=None,
        created_at=None, plan_json=plan)


def test_logger_writes_second_leg_and_total(cfg, tmp_path):
    from services.ml_trade_logger import MLTradeLogger
    lg = MLTradeLogger(path=tmp_path / "t.jsonl")
    lg.log_closed_signal(_signal(1, -0.1, {"tp1_partial": {"net_pnl": 0.5, "total_cost": 0.02}}))
    lg.log_closed_signal(_signal(2, 0.2, json.dumps({"lifecycle": {}})))     # plan_json строкой
    r1, r2 = [json.loads(x) for x in open(tmp_path / "t.jsonl", encoding="utf-8")]
    assert r1["tp1_partial_net_pnl"] == 0.5 and r1["total_net_pnl"] == pytest.approx(0.4)
    assert r1["labels"]["is_win"] is True              # остаток −0.1, но сделка в плюсе
    assert r2["tp1_partial_net_pnl"] == 0.0


def test_logger_backfill_adds_missing_leg_once(cfg, tmp_path, monkeypatch):
    import models.signal as ms
    from services.ml_trade_logger import MLTradeLogger

    class _Col:
        def in_(self, ids):
            return ids

    class _Sig:
        id = _Col()
    monkeypatch.setattr(ms, "Signal", _Sig, raising=False)

    path = tmp_path / "t.jsonl"
    old_row = {"signal_id": 1, "closed_net_pnl": -0.1, "labels": {"is_win": False}}
    _write(path, [old_row])
    sig = _signal(1, -0.1, {"tp1_partial": {"net_pnl": 0.5, "total_cost": 0.02}})

    class _Q:
        def filter(self, *_a):
            return self
        def all(self):
            return [sig]
    db = SimpleNamespace(query=lambda *_a: _Q())

    lg = MLTradeLogger(path=path)
    res = lg.backfill_tp1_partial(db)
    assert res["status"] == "ok" and res["updated"] == 1 and res["with_partial_leg"] == 1
    row = json.loads(open(path, encoding="utf-8").read())
    assert row["tp1_partial_net_pnl"] == 0.5 and row["labels"]["is_win"] is True
    assert (tmp_path / "t.jsonl.bak-before-tp1-backfill").exists()
    assert lg.backfill_tp1_partial(db)["updated"] == 0          # повторно не трогает


# ── исследовательский модуль ──────────────────────────────────────────────────
def test_triple_barrier_same_bar_is_conservative():
    pd = pytest.importorskip("pandas")
    from services.ml_market_research import triple_barrier_labels
    n = 40
    df = pd.DataFrame({"open": [100.0] * n, "high": [100.5] * n, "low": [99.5] * n, "close": [100.0] * n})
    df.loc[20, ["high", "low"]] = [130.0, 70.0]                # оба барьера в одной свече
    y = triple_barrier_labels(df, horizon=5, k_atr=1.5)
    assert y.iloc[19] == 0.0
