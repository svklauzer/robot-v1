"""ML market research — ЧЕСТНОЕ измерение: предсказуемы ли движения по OHLC.

Это НЕ торговый модуль, а исследовательский: отвечает на вопрос пользователя
«насколько реально OHLC-фичи помогут предсказать движение» — ДАННЫМИ, а не верой.

Главные ловушки ML-в-трейдинге, которых здесь избегаем:
  1. Утечка будущего → фичи только из ПРОШЛОГО, метка — из будущего.
  2. Случайный сплит → НЕТ. Только хронологический (walk-forward).
  3. «Accuracy 53% = успех» → НЕТ. Считаем expectancy ПОСЛЕ КОСТОВ и сравниваем
     с наивным бейзлайном. Малый край после комиссий = бесполезно.
  4. Точный прогноз цены → НЕ делаем (огромная ошибка). Метка = triple-barrier
     (дойдёт ли +k·ATR раньше −k·ATR за N баров) — как мы реально торгуем (TP/SL).

Зависимости: pandas/numpy/scikit-learn (уже в requirements).
"""
from __future__ import annotations


# ── фичи (нормализованные, чтобы модель обобщалась между символами/режимами) ──
def build_features(df):
    import numpy as np
    import pandas as pd

    d = df.copy()
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]

    ema20 = c.ewm(span=20, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    ema200 = c.ewm(span=200, adjust=False).mean()

    # RSI14
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)

    # MACD
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - macd_sig

    # ATR14
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    atr_pct = atr / c

    # Bollinger(20,2)
    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    bb_up, bb_dn = sma20 + 2 * std20, sma20 - 2 * std20
    bb_pctb = (c - bb_dn) / (bb_up - bb_dn).replace(0, np.nan)
    bb_bw = (bb_up - bb_dn) / sma20.replace(0, np.nan)

    feats = pd.DataFrame(index=d.index)
    feats["ret_1"] = c.pct_change(1)
    feats["ret_5"] = c.pct_change(5)
    feats["hl_range_atr"] = (h - l) / atr.replace(0, np.nan)
    feats["ema20_dist"] = (c - ema20) / c
    feats["ema50_dist"] = (c - ema50) / c
    feats["ema200_dist"] = (c - ema200) / c
    feats["ema20_slope"] = (ema20 - ema20.shift(3)) / ema20
    feats["rsi14"] = rsi
    feats["macd_hist_atr"] = macd_hist / atr.replace(0, np.nan)
    feats["atr_pct"] = atr_pct
    feats["bb_pctb"] = bb_pctb
    feats["bb_bandwidth"] = bb_bw

    # ── Объём во времени, привязан к OHLC (запрос пользователя — и он прав) ────
    vma20 = v.rolling(20).mean()
    feats["vol_ratio"] = v / vma20.replace(0, np.nan)              # объём vs средний
    feats["vol_trend"] = v.rolling(5).mean() / vma20.replace(0, np.nan)  # растёт/падает
    feats["vol_zscore"] = (v - vma20) / v.rolling(20).std().replace(0, np.nan)  # всплеск/затухание
    # OBV-наклон: подтверждает ли объём направление цены (накопление/распределение)
    obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
    feats["obv_slope"] = (obv - obv.shift(5)) / vma20.replace(0, np.nan)
    # VWAP-дистанция: цена выше/ниже объёмно-взвешенной «справедливой»
    typical = (h + l + c) / 3
    vwap = (typical * v).rolling(20).sum() / v.rolling(20).sum().replace(0, np.nan)
    feats["vwap_dist"] = (c - vwap) / c
    return feats


# ── метка: triple-barrier (как мы торгуем: TP/SL за N баров) ──────────────────
def triple_barrier_labels(df, horizon: int, k_atr: float):
    import numpy as np
    import pandas as pd

    c, h, l = df["close"].values, df["high"].values, df["low"].values
    # ATR% для масштаба барьеров
    tr = pd.concat([
        (df["high"] - df["low"]),
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean().values

    n = len(c)
    y = np.full(n, np.nan)
    for i in range(n - 1):
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        up = c[i] + k_atr * atr[i]
        dn = c[i] - k_atr * atr[i]
        end = min(i + horizon, n - 1)
        label = np.nan
        for j in range(i + 1, end + 1):
            if h[j] >= up:
                label = 1.0  # верхний барьер первым
                break
            if l[j] <= dn:
                label = 0.0  # нижний барьер первым
                break
        y[i] = label  # NaN если ни один барьер не задет за горизонт
    return pd.Series(y, index=df.index)


# ── исследование: честная walk-forward оценка ────────────────────────────────
def evaluate(symbol: str, timeframe: str = "1h", limit: int = 1500,
             horizon: int = 24, k_atr: float = 1.5) -> dict:
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline
        from sklearn.metrics import roc_auc_score
        from services.market_data import MarketDataService
        from core.config import settings
    except Exception as exc:
        return {"status": "deps_unavailable", "error": f"{type(exc).__name__}: {exc}"}

    try:
        df = MarketDataService().ohlcv(symbol, timeframe=timeframe, limit=int(limit))
    except Exception as exc:
        return {"status": "ohlcv_error", "error": f"{type(exc).__name__}: {exc}"}
    if df is None or len(df) < 300:
        return {"status": "not_enough_bars", "bars": 0 if df is None else len(df)}

    X = build_features(df)
    y = triple_barrier_labels(df, horizon=horizon, k_atr=k_atr)

    data = X.copy()
    data["y"] = y
    data = data.replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < 200 or data["y"].nunique() < 2:
        return {"status": "not_enough_labeled", "labeled": int(len(data))}

    Xa = data[list(X.columns)].values
    ya = data["y"].astype(int).values
    base_rate = float(ya.mean())
    n = len(ya)

    folds = int(getattr(settings, "RESEARCH_WF_FOLDS", 5))
    cost_atr = float(getattr(settings, "RESEARCH_COST_ATR", 0.25))
    thr = 0.55

    def _make(name):
        if name == "logreg":
            return Pipeline([("s", StandardScaler()),
                             ("c", LogisticRegression(max_iter=1000, class_weight="balanced"))])
        # n_estimators 120→60: вдвое меньше CPU при scan, чтобы тяжёлый прогон не
        # «душил» event loop и не ронял WS-фид (pong-таймаут 1003). logreg —
        # основной по вердикту, gbm лишь для сравнения, поэтому потеря точности ок.
        return GradientBoostingClassifier(max_depth=3, n_estimators=60, learning_rate=0.08)

    def _walk_forward(name):
        # Purged walk-forward: расширяющееся окно train, последовательные тест-окна,
        # между train/test выкидываем horizon баров (против утечки triple-barrier).
        test_size = max(n // (folds + 1), 30)
        aucs, exps, pos = [], [], 0
        for i in range(1, folds + 1):
            te_start = i * test_size
            te_end = (te_start + test_size) if i < folds else n
            tr_end = max(0, te_start - horizon)  # purge
            if tr_end < 80 or (te_end - te_start) < 30:
                continue
            Xtr, ytr = Xa[:tr_end], ya[:tr_end]
            Xte, yte = Xa[te_start:te_end], ya[te_start:te_end]
            if len(set(ytr.tolist())) < 2 or len(set(yte.tolist())) < 2:
                continue
            try:
                m = _make(name).fit(Xtr, ytr)
                proba = m.predict_proba(Xte)[:, 1]
                auc = float(roc_auc_score(yte, proba))
                take = proba >= thr
                exp = (float(np.mean(np.where(yte[take] == 1, k_atr, -k_atr)) - cost_atr * 2)
                       if take.sum() > 0 else 0.0)
                aucs.append(auc); exps.append(exp)
                if auc > 0.5 and exp > 0:
                    pos += 1
            except Exception:
                continue
        if not aucs:
            return None
        return {
            "mean_auc": round(float(np.mean(aucs)), 4),
            "std_auc": round(float(np.std(aucs)), 4),
            "mean_expectancy_atr": round(float(np.mean(exps)), 4),
            "folds_positive": pos,
            "folds": len(aucs),
        }

    models = {}
    for nm in ("logreg", "gbm"):
        r = _walk_forward(nm)
        if r:
            models[nm] = r

    # Вердикт по logreg (устойчив к переобучению) + КОНСИСТЕНТНОСТЬ по фолдам.
    primary = models.get("logreg") or (next(iter(models.values())) if models else None)
    if (primary and primary["mean_auc"] >= 0.55 and primary["mean_expectancy_atr"] > 0
            and primary["folds_positive"] >= max(1, int(primary["folds"] * 0.6))):
        verdict = "edge_found"
    elif primary and primary["mean_auc"] >= 0.53:
        verdict = "weak_signal_marginal"
    else:
        verdict = "no_edge_after_costs"

    return {
        "status": "ok",
        "symbol": symbol, "timeframe": timeframe, "bars": int(len(df)),
        "labeled_samples": int(n),
        "horizon": horizon, "k_atr": k_atr, "wf_folds": folds,
        "baseline_up_rate": round(base_rate, 4),
        "models": models,
        "verdict": verdict,
        "note": ("Purged walk-forward k-fold. edge_found = средний AUC≥0.55, "
                 "положительный средний expectancy И ≥60% фолдов в плюс. "
                 "logreg — основной (устойчив к переобучению). Меньше cherry-pick, чем одиночный сплит."),
    }
