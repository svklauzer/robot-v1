"""Grid calculator — ЧИСТОЕ ядро умной сетки (индикаторы + регайм + уровни).

Никаких сайд-эффектов, БД и ccxt: только математика. Тестируется изолированно.
Реализует формулы Капитана:

  Шаг до n-го ордера (накопительно):  step_n = ATR · k_vol · m_step^(n-1)
                                       distance_n = Σ step_1..step_n
  Объём n-го ордера:                   V_n = V_base · m_vol^(n-1)

Регайм (по EMA200 + RSI14):
  цена > EMA200 и RSI < rsi_high  → LONG-сетка  (покупки на падении)
  цена < EMA200 и RSI > rsi_low   → SHORT-сетка (продажи на росте)
  иначе (боковик/перегрев)        → NEUTRAL (двусторонняя)

Округление цен/объёмов к спецификации тикера делает ВЫЗЫВАЮЩИЙ слой
(grid_engine через htx.price_to_precision / amount_to_precision) — здесь сырые числа.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


# ── индикаторы (pandas, локальный импорт для sandbox-тестируемости) ───────────
def compute_indicators(df, ema_period: int = 200, rsi_period: int = 14,
                       atr_period: int = 14) -> dict:
    """EMA, RSI, ATR по последней ЗАКРЫТОЙ свече. df: OHLCV DataFrame."""
    import numpy as np
    import pandas as pd

    c, h, l = df["close"].astype(float), df["high"].astype(float), df["low"].astype(float)
    price = float(c.iloc[-1])

    ema = c.ewm(span=ema_period, adjust=False).mean().iloc[-1]

    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / rsi_period, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / rsi_period, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])

    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.ewm(alpha=1 / atr_period, adjust=False).mean().iloc[-1])

    return {"price": price, "ema": float(ema), "rsi": rsi, "atr": atr}


def detect_regime(ind: dict, rsi_high: float = 70.0, rsi_low: float = 30.0,
                  ema_band_pct: float = 0.25) -> str:
    """LONG / SHORT / NEUTRAL по EMA200 + RSI14.

    (#grid-flip-thrash) Мёртвая зона вокруг EMA200: пока цена в пределах
    ±ema_band_pct% от EMA, регайм = NEUTRAL. Иначе в боковике (цена висит на
    EMA200, как SOL @70.7) регайм флипается long/short каждые пару тиков →
    бесконечные grid_regime_flip (в срезе: 20+ циклов с realized 0). Зона гасит пилу.
    """
    price, ema, rsi = ind["price"], ind["ema"], ind["rsi"]
    band = ema * max(0.0, ema_band_pct) / 100.0
    if price > ema + band and rsi < rsi_high:
        return "long"
    if price < ema - band and rsi > rsi_low:
        return "short"
    return "neutral"


@dataclass
class GridLevel:
    n: int               # номер ордера в сетке (1..)
    side: str            # "buy" / "sell"
    price: float         # цена выставления
    volume: float        # объём (база, до округления биржей)
    distance_pct: float  # дистанция от якоря, %


def _ladder(anchor: float, atr: float, side: str, *, lines: int, k_vol: float,
            m_step: float, v_base: float, m_vol: float) -> list[GridLevel]:
    """Одна лестница уровней в одну сторону. buy → ниже якоря, sell → выше."""
    levels: list[GridLevel] = []
    cum = 0.0
    for i in range(1, int(lines) + 1):
        step = atr * k_vol * (m_step ** (i - 1))     # step_n = ATR·k_vol·m_step^(n-1)
        cum += step                                   # distance_n = Σ step
        vol = v_base * (m_vol ** (i - 1))             # V_n = V_base·m_vol^(n-1)
        if side == "buy":
            price = anchor - cum
        else:
            price = anchor + cum
        if price <= 0:
            break
        levels.append(GridLevel(
            n=i, side=side, price=round(price, 10), volume=round(vol, 10),
            distance_pct=round(cum / anchor * 100, 4),
        ))
    return levels


def compute_grid(anchor: float, atr: float, regime: str, *, lines: int,
                 k_vol: float, m_step: float, v_base: float, m_vol: float) -> list[dict]:
    """Все уровни сетки от якорной цены. NEUTRAL = buy ниже + sell выше."""
    if anchor <= 0 or atr <= 0 or lines < 1:
        return []
    out: list[GridLevel] = []
    if regime == "long":
        out = _ladder(anchor, atr, "buy", lines=lines, k_vol=k_vol, m_step=m_step, v_base=v_base, m_vol=m_vol)
    elif regime == "short":
        out = _ladder(anchor, atr, "sell", lines=lines, k_vol=k_vol, m_step=m_step, v_base=v_base, m_vol=m_vol)
    else:  # neutral — половина линий вниз (buy), половина вверх (sell)
        half = max(1, int(lines) // 2)
        out = (_ladder(anchor, atr, "buy", lines=half, k_vol=k_vol, m_step=m_step, v_base=v_base, m_vol=m_vol)
               + _ladder(anchor, atr, "sell", lines=half, k_vol=k_vol, m_step=m_step, v_base=v_base, m_vol=m_vol))
    return [asdict(x) for x in out]


def respace_levels(unfilled: list[dict], base_price: float, atr: float, side: str,
                   k_vol: float, m_step: float) -> list[dict]:
    """Пере-разложить НЕисполненные уровни одной стороны под текущий ATR.
    ИСПРАВЛЕНО: Шаг геометрической прогрессии рассчитывается строго по номеру уровня lv["n"]!
    """
    out: list[dict] = []
    cum = 0.0
    for lv in unfilled:
        # ИСПРАВЛЕНО: Вместо enumerate(start=1) используем оригинальный номер ордера lv["n"]
        step = atr * k_vol * (m_step ** (lv["n"] - 1))
        cum += step
        price = base_price - cum if side == "buy" else base_price + cum
        nlv = dict(lv)
        nlv["price"] = round(price, 10)
        nlv["distance_pct"] = round(cum / base_price * 100, 4) if base_price else 0.0
        out.append(nlv)
    return out


# ── позиция/безубыток/TP/SL по ИСПОЛНЕННЫМ уровням ───────────────────────────
def position_state(filled: list[dict], fee_round_pct: float = 0.0) -> dict:
    """Средняя цена и нетто-направление по исполненным уровням.
    filled: [{side, price(fill), volume}]. Возвращает avg, net_qty, gross_qty, dominant_side."""
    buy_qty = sum(float(f["volume"]) for f in filled if f["side"] == "buy")
    sell_qty = sum(float(f["volume"]) for f in filled if f["side"] == "sell")
    buy_cost = sum(float(f["volume"]) * float(f["price"]) for f in filled if f["side"] == "buy")
    sell_cost = sum(float(f["volume"]) * float(f["price"]) for f in filled if f["side"] == "sell")
    net = buy_qty - sell_qty
    dom = "long" if net > 0 else "short" if net < 0 else "flat"
    if dom == "long":
        avg = buy_cost / buy_qty if buy_qty else 0.0
    elif dom == "short":
        avg = sell_cost / sell_qty if sell_qty else 0.0
    else:
        avg = 0.0
    return {"avg_price": avg, "net_qty": net, "buy_qty": buy_qty, "sell_qty": sell_qty,
            "dominant_side": dom, "filled_count": len(filled)}


def breakeven_price(avg_price: float, side: str, fee_round_pct: float) -> float:
    """Безубыток с учётом round-trip комиссии (% от цены)."""
    if avg_price <= 0:
        return 0.0
    f = max(0.0, fee_round_pct) / 100.0
    return avg_price * (1 + f) if side == "long" else avg_price * (1 - f)


def take_profit_price(breakeven: float, side: str, tp_pct: float) -> float:
    """TP = безубыток + фикс. % в сторону прибыли."""
    p = max(0.0, tp_pct) / 100.0
    return breakeven * (1 + p) if side == "long" else breakeven * (1 - p)


def stop_loss_price(all_levels: list[dict], atr: float, side: str, atr_mult: float, anchor: float | None = None) -> float:
    """ИСПРАВЛЕНО: Стоп-Лосс привязывается к крайнему пределу сетки.
    Сделано полностью совместимым со старыми 4-аргументными вызовами!
    """
    if not all_levels:
        return 0.0
    
    # Резервный якорь, если anchor не передан
    fallback_anchor = anchor if anchor is not None else float(all_levels[0]["price"])
    
    if side == "long":
        buy_levels = [l for l in all_levels if l["side"] == "buy"]
        lowest_grid_price = min([l["price"] for l in buy_levels]) if buy_levels else fallback_anchor
        return lowest_grid_price - atr_mult * atr
    else:
        sell_levels = [l for l in all_levels if l["side"] == "sell"]
        highest_grid_price = max([l["price"] for l in sell_levels]) if sell_levels else fallback_anchor
        return highest_grid_price + atr_mult * atr


def unrealized_pnl(filled: list[dict], price: float) -> float:
    """Нереализованный PnL всей сетки по текущей цене (агрегат, не по числу ордеров)."""
    pnl = 0.0
    for f in filled:
        v, fp = float(f["volume"]), float(f["price"])
        if f["side"] == "buy":
            pnl += (price - fp) * v
        else:
            pnl += (fp - price) * v
    return round(pnl, 8)
