"""Volume Profile из OHLCV — узлы объёма по цене (для ВЫБОРА УРОВНЕЙ, не прогноза).

Это «объёмный анализ» в выполнимой форме: настоящий footprint (объём по цене
внутри бара) требует tick-данных, которых у нас нет исторически. Volume Profile
же считается из обычных OHLCV: объём каждого бара распределяем по его диапазону
[low, high] и копим по ценовым корзинам. Получаем:

  VPOC  — Point of Control: цена с максимальным объёмом (справедливая цена).
  Value Area (VAH/VAL) — узкий диапазон, где прошло ~70% объёма.
  HVN   — High Volume Nodes: узлы реакции (цена тормозит, отскакивает).
  LVN   — Low Volume Nodes: «пустоты», цена проходит их быстро.

Польза — ИСПОЛНЕНИЕ (ставить TP/стоп у HVN, не в LVN), где у нас и есть край,
а НЕ предсказание направления. pandas/numpy.
"""
from __future__ import annotations


def compute_volume_profile(symbol: str, timeframe: str = "1h",
                           limit: int = 1000, bins: int = 50) -> dict:
    try:
        import numpy as np
        from services.market_data import MarketDataService
    except Exception as exc:
        return {"status": "deps_unavailable", "error": f"{type(exc).__name__}: {exc}"}

    try:
        df = MarketDataService().ohlcv(symbol, timeframe=timeframe, limit=int(limit))
    except Exception as exc:
        return {"status": "ohlcv_error", "error": f"{type(exc).__name__}: {exc}"}
    if df is None or len(df) < 50:
        return {"status": "not_enough_bars", "bars": 0 if df is None else len(df)}

    low = float(df["low"].min())
    high = float(df["high"].max())
    if high <= low:
        return {"status": "degenerate_range"}

    nb = max(int(bins), 10)
    edges = np.linspace(low, high, nb + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    bin_w = (high - low) / nb

    vol = np.zeros(nb)
    bl, bh, bv = df["low"].values, df["high"].values, df["volume"].values
    for i in range(len(bv)):
        lo_i = max(0, int((bl[i] - low) / bin_w))
        hi_i = min(nb - 1, int((bh[i] - low) / bin_w))
        span = hi_i - lo_i + 1
        if span <= 0 or bv[i] <= 0:
            continue
        vol[lo_i:hi_i + 1] += bv[i] / span  # объём бара равномерно по его диапазону

    total = float(vol.sum())
    if total <= 0:
        return {"status": "no_volume"}

    vpoc_idx = int(np.argmax(vol))
    vpoc = float(centers[vpoc_idx])

    # Value Area: расширяемся от VPOC, добирая больший из соседей, пока < 70%.
    target = 0.7 * total
    lo_idx = hi_idx = vpoc_idx
    acc = float(vol[vpoc_idx])
    while acc < target and (lo_idx > 0 or hi_idx < nb - 1):
        left = vol[lo_idx - 1] if lo_idx > 0 else -1.0
        right = vol[hi_idx + 1] if hi_idx < nb - 1 else -1.0
        if right >= left:
            hi_idx += 1
            acc += float(vol[hi_idx])
        else:
            lo_idx -= 1
            acc += float(vol[lo_idx])
    vah, val = float(centers[hi_idx]), float(centers[lo_idx])

    # HVN = локальные максимумы выше 75-перцентиля; LVN = локальные минимумы ниже 25-го.
    hi_thr = float(np.percentile(vol, 75))
    lo_thr = float(np.percentile(vol, 25))
    hvn, lvn = [], []
    for j in range(nb):
        left = vol[j - 1] if j > 0 else -1.0
        right = vol[j + 1] if j < nb - 1 else -1.0
        if vol[j] >= hi_thr and vol[j] >= left and vol[j] >= right:
            hvn.append(round(float(centers[j]), 8))
        if vol[j] <= lo_thr and vol[j] <= left and vol[j] <= right:
            lvn.append(round(float(centers[j]), 8))

    price = float(df["close"].iloc[-1])
    vpct = (vol / total * 100.0)
    return {
        "status": "ok",
        "symbol": symbol, "timeframe": timeframe, "bars": int(len(df)),
        "price": round(price, 8),
        "vpoc": round(vpoc, 8),
        "vah": round(vah, 8), "val": round(val, 8),
        "in_value_area": bool(val <= price <= vah),
        "hvn": hvn, "lvn": lvn,
        "nearest_hvn_above": min([h for h in hvn if h > price], default=None),
        "nearest_hvn_below": max([h for h in hvn if h < price], default=None),
        "nearest_lvn_above": min([l for l in lvn if l > price], default=None),
        "nearest_lvn_below": max([l for l in lvn if l < price], default=None),
        "profile": [{"price": round(float(centers[j]), 8), "vol_pct": round(float(vpct[j]), 3)}
                    for j in range(nb)],
        "note": ("Volume Profile из OHLCV (объём бара распределён по [low,high]). "
                 "HVN=узлы реакции (TP/стоп тут логичны), LVN=быстрые зоны, VPOC=справедливая цена. "
                 "Это для ВЫБОРА УРОВНЕЙ, не для прогноза направления."),
    }
