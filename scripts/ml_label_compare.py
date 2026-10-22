"""Сравнение двух вариантов метки для мета-лейблера. ТОЛЬКО ЧТЕНИЕ, торговлю не трогает.

ПРОТОКОЛ (зафиксирован ДО запуска; менять после просмотра результатов нельзя):

Данные   storage/ml/trade_outcomes.jsonl; строки не-фантомные, закрытые не старше
         ML_TRAIN_WINDOW_DAYS (45) от последней строки; порядок файла.
Метки    A = beats_costs: net/риск >= 0.3 (как сейчас).
         B = reach_tp1:   mfe_pct >= дистанции до TP1 в % (дошла до первой цели).
         Сравнение на ОДНОМ и том же наборе строк (пересечение).
Модель   та же, что в трейнере: CalibratedClassifierCV(StandardScaler+RF 300/5/2,
         balanced_subsample, log2, sigmoid, cv=3). Гиперпараметры не подбираются.
Схема    walk-forward как в трейнере: 4 фолда, start_frac 0.4, embargo 5.
Метрика  ЕДИНАЯ для обеих меток и ЭКОНОМИЧЕСКАЯ: в каждом OOS-фолде сделки с
         score >= медианы фолда ("верх") против остальных ("низ"); разница
         среднего closed_net_pnl (USDT на сделку), пул по фолдам.
         CI 95% блочный bootstrap (блок 20), p — перестановка score внутри фолда.
Решение  Метка принимается для дальнейшего shadow, только если у неё нижняя
         граница CI разницы > 0 И точечная оценка больше, чем у другой метки.
         Иначе: "не доказано", остаёмся на текущей. Порог score НЕ подбирается.
         Оговорка: мощность мала (OOS ~250 сделок), эффект < ~0.7 USDT/сделку
         статистически не виден — это ограничение данных, а не вывод об отсутствии.

Запуск:  python scripts/ml_label_compare.py [путь_к_trade_outcomes.jsonl]
"""
from __future__ import annotations

import ast
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))
try:
    from services.ml_features import is_phantom_row, row_to_features, row_to_label, total_net_pnl
except Exception:  # локальная проверка вне репозитория: заглушка зависимости
    m = types.ModuleType("services.close_reasons")
    m.reached_tp2 = lambda *a, **k: False
    sys.modules["services.close_reasons"] = m
    from services.ml_features import is_phantom_row, row_to_features, row_to_label, total_net_pnl

from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

WINDOW_DAYS, N_FOLDS, START_FRAC, EMBARGO, BLOCK, N_BOOT, N_PERM = 45, 4, 0.4, 5, 20, 2000, 2000


def estimator():
    rf = RandomForestClassifier(n_estimators=300, max_depth=5, min_samples_leaf=2,
                                class_weight="balanced_subsample", max_features="log2",
                                random_state=42, n_jobs=1)
    return CalibratedClassifierCV(estimator=Pipeline([("s", StandardScaler()), ("c", rf)]),
                                  method="sigmoid", cv=3)


def _d(v):
    if isinstance(v, dict):
        return v
    try:
        return ast.literal_eval(v) if isinstance(v, str) else {}
    except Exception:
        return {}


def label_reach_tp1(r):
    lc, z, tp = _d(r.get("lifecycle")), _d(r.get("entry_zone")), _d(r.get("tp"))
    try:
        e = float(lc.get("entry_price") or (float(z["from"]) + float(z["to"])) / 2)
        dist = abs(float(tp["tp1"]) - e) / e * 100
        return 1 if float(lc["mfe_pct"]) >= dist else 0
    except Exception:
        return None


def load(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    rows = [r for r in rows if str(r.get("status")) == "closed" and not is_phantom_row(r)]
    def ts(r):
        raw = str(r.get("closed_at") or r.get("created_at") or "").replace("Z", "+00:00")
        try:
            t = datetime.fromisoformat(raw)
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    last = max(t for t in map(ts, rows) if t)
    cut = last - timedelta(days=WINDOW_DAYS)
    rows = [r for r in rows if (ts(r) or last) >= cut]
    out = []
    for r in rows:
        a, b, n = row_to_label(r, "beats_costs", 0.3), label_reach_tp1(r), total_net_pnl(r)
        if a is None or b is None or n is None:
            continue
        out.append((row_to_features(r), a, b, n))
    return out


def walk_forward(X, y):
    n = len(y)
    start = int(n * START_FRAC)
    edges = [start + (n - start) * k // N_FOLDS for k in range(N_FOLDS + 1)]
    folds = []
    for k in range(N_FOLDS):
        te0, te1 = edges[k], edges[k + 1]
        tr = max(te0 - EMBARGO, 0)
        if len(set(y[:tr].tolist())) < 2:
            continue
        p = estimator().fit(X[:tr], y[:tr]).predict_proba(X[te0:te1])[:, 1]
        folds.append((np.arange(te0, te1), p))
    return folds


def top_bottom(folds, net):
    hi, lo = [], []
    for idx, p in folds:
        top = p >= np.median(p)
        hi.extend(net[idx][top]); lo.extend(net[idx][~top])
    return float(np.mean(hi) - np.mean(lo)), len(hi), len(lo)


def evaluate(name, folds, net, y):
    est, nh, nl = top_bottom(folds, net)
    rng = np.random.default_rng(42)
    idx_all = np.concatenate([i for i, _ in folds])
    p_all = np.concatenate([p for _, p in folds])
    n_all, y_all = net[idx_all], y[idx_all]
    top_all = np.concatenate([p >= np.median(p) for _, p in folds])
    m = len(idx_all)
    nb = int(np.ceil(m / BLOCK))
    boots = []
    for _ in range(N_BOOT):
        s = rng.integers(0, max(m - BLOCK, 0) + 1, size=nb)
        ix = np.concatenate([np.arange(a, min(a + BLOCK, m)) for a in s])[:m]
        t, v = top_all[ix], n_all[ix]
        if t.all() or (~t).all():
            continue
        boots.append(v[t].mean() - v[~t].mean())
    lo, hi = np.percentile(boots, [2.5, 97.5])
    cnt = 0
    for _ in range(N_PERM):
        d = []
        for idx, p in folds:
            q = rng.permutation(p)
            t = q >= np.median(q)
            d.append((net[idx][t], net[idx][~t]))
        diff = np.mean(np.concatenate([a for a, _ in d])) - np.mean(np.concatenate([b for _, b in d]))
        cnt += diff >= est
    auc_own = roc_auc_score(y_all, p_all) if len(set(y_all.tolist())) == 2 else None
    auc_econ = roc_auc_score((n_all > 0).astype(int), p_all)
    print(f"\n[{name}] OOS={m}  метка+ доля={y_all.mean():.3f}")
    print(f"  верх−низ, USDT/сделку: {est:+.3f}  CI95 [{lo:+.3f}; {hi:+.3f}]  p(перест.)={cnt / N_PERM:.3f}  (верх n={nh}, низ n={nl})")
    print(f"  AUC по своей метке: {auc_own:.3f}   AUC по net>0: {auc_econ:.3f}")
    return est, lo


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "storage" / "ml" / "trade_outcomes.jsonl"
    data = load(path)
    X = np.array([d[0] for d in data]); net = np.array([d[3] for d in data])
    ya, yb = np.array([d[1] for d in data]), np.array([d[2] for d in data])
    print(f"строк в сравнении: {len(data)}; A beats_costs +{ya.mean():.3f}; B reach_tp1 +{yb.mean():.3f}")
    print(f"средний net: метка B=1 {net[yb == 1].mean():+.2f} (n={int((yb == 1).sum())}), B=0 {net[yb == 0].mean():+.2f}")
    ra = evaluate("A beats_costs", walk_forward(X, ya), net, ya)
    rb = evaluate("B reach_tp1", walk_forward(X, yb), net, yb)
    print("\nРЕШЕНИЕ по протоколу:")
    if rb[1] > 0 and rb[0] > ra[0]:
        print("  B принимается для дальнейшего shadow (CI>0 и лучше A).")
    elif ra[1] > 0 and ra[0] >= rb[0]:
        print("  Остаёмся на A (CI>0), B не лучше.")
    else:
        print("  НЕ ДОКАЗАНО ни для одной метки: остаёмся на текущей, копим данные.")


if __name__ == "__main__":
    main()
