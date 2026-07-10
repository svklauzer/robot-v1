"""Micro-flow скальпер (Фаза 4) — настоящий скальпер на 5m + стакан.

Принципы (по требованию Капитана):
  - живёт на 5m микроструктуре, 1h/4h НЕ читает;
  - направление — от микро-края + ПОТОКА ОРДЕРОВ (OBI/CVD), родного инструмента
    скальпера; не фейдит микро-импульс (вето по 5m-тренду);
  - много сделок, мелкая прибыль (target ~0.4%), тугой стоп; ведение — scalp-профиль;
  - без живого стакана не торгует (require_depth).

Чистые функции над TimeframeContext + depth-dict (fresh/obi/spread_pct/cvd_ratio/
cvd_trades). Тестируется без ccxt/pandas/websocket.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.config import settings
from core.strategy_profiles import get_profiles


@dataclass
class ScalpSignal:
    action: str          # "long" | "short"
    regime: str          # "scalp"
    entry_zone: list[float]
    stop_price: float
    tp: dict[str, float]
    confidence_hint: float
    reason: str
    setup_quality: dict[str, Any]
    setup_decision: str  # "approve"


def _ctx(contexts: Any, tf: str) -> dict[str, Any] | None:
    if not isinstance(contexts, dict):
        return None
    return contexts.get(tf)


def _v(ctx: Any, key: str, default: float | str = 0.0) -> Any:
    if ctx is None:
        return default
    if isinstance(ctx, dict):
        return ctx.get(key, default)
    return getattr(ctx, key, default)


class MicroScalpService:
    """Оценивает символ на micro-scalp вход. 
    
    depth — dict из OrderBookAnalyzer.analyze().as_dict().
    """

    def evaluate(self, contexts: Any, depth: dict[str, Any] | None = None, symbol: str | None = None) -> ScalpSignal | None:
        sp = get_profiles().scalp_engine
        if not sp.enabled:
            return None

        m5 = _ctx(contexts, "5m")
        if m5 is None:
            return None

        # 1. Валидация стакана (Orderbook) — поток это главный инструмент скальпера
        active_depth = depth if depth is not None else {}
        fresh = bool(active_depth.get("fresh", False))
        if sp.require_depth and not fresh:
            return None

        obi = float(active_depth.get("obi", 0.0))
        spread = active_depth.get("spread_pct", None)
        cvd_ratio = float(active_depth.get("cvd_ratio", 0.0))

        if spread is not None and float(spread) > sp.max_spread_pct:
            return None  # Слишком дорогой спред для скальпинга

        # 2. Получение параметров микроструктуры 5m
        low = float(_v(m5, "support", 0.0))
        high = float(_v(m5, "resistance", 0.0))
        price = float(_v(m5, "last_close", 0.0))
        atr = float(_v(m5, "atr14", 0.0))

        if low <= 0 or high <= low or price <= 0:
            return None

        width_pct = (high - low) / low * 100.0
        
        # Безопасная проверка минимальной ширины с защитой от ZeroDivisionError
        min_width = max(float(sp.min_micro_width_pct), 1e-6)
        if width_pct < min_width:
            return None

        if atr <= 0:
            atr = price * 0.001

        pos = (price - low) / (high - low)   # 0 = микро-поддержка, 1 = микро-сопротивление
        m5_trend = str(_v(m5, "trend", "mixed"))
        fee_round_pct = float(settings.SPOT_TAKER_FEE) * 2 * 100.0
        buf = atr * sp.stop_buffer_atr

        # 3. Определение направления по микро-краям и OBI
        direction = None
        if pos <= sp.edge_zone and m5_trend != "trend_down" and obi >= sp.min_obi:
            direction = "long"
        elif (sp.allow_short and pos >= (1.0 - sp.edge_zone)
              and m5_trend != "trend_up" and obi <= -sp.min_obi):
            direction = "short"

        if direction is None:
            return None

        # 4. (#scalp-htf-veto-2026-07-10) Вето старшего таймфрейма на экстремумы моментума
        if bool(getattr(settings, "SCALP_HTF_EXTREME_VETO", True)):
            _h1 = _ctx(contexts, "1h")
            if _h1 is not None:
                _h1_rsi = float(_v(_h1, "rsi14", 50.0) or 50.0)
                if direction == "short" and _h1_rsi >= float(getattr(settings, "SCALP_HTF_RSI_OVERHEAT", 70.0)):
                    return None
                if direction == "long" and _h1_rsi <= float(getattr(settings, "SCALP_HTF_RSI_OVERSOLD", 30.0)):
                    return None

        # 5. Расчет торговых уровней и зон входа (раздельная логика для Long и Short)
        if direction == "long":
            entry = price
            stop = low - buf
            tp1 = price * (1.0 + sp.target_pct / 100.0)
            tp2 = price * (1.0 + sp.target_pct * sp.tp2_mult / 100.0)
            net_tp1 = (tp1 - entry) / entry * 100.0 - fee_round_pct
            
            flow_align = max(0.0, cvd_ratio)
            obi_str = obi
            proximity = (1.0 - pos) * 35.0  # Чем ближе к 0 (поддержка), тем выше балл
            
            # Зона входа: от текущей цены до чуть более глубокого отката
            entry_low = entry * (1.0 - 0.0005)
            entry_high = entry
        else:
            entry = price
            stop = high + buf
            tp1 = price * (1.0 - sp.target_pct / 100.0)
            tp2 = price * (1.0 - sp.target_pct * sp.tp2_mult / 100.0)
            net_tp1 = (entry - tp1) / entry * 100.0 - fee_round_pct
            
            flow_align = max(0.0, -cvd_ratio)
            obi_str = -obi
            proximity = pos * 35.0  # Чем ближе к 1 (сопротивление), тем выше балл
            
            # Зона входа: от текущей цены до чуть более высокого отскока вверх
            entry_low = entry
            entry_high = entry * (1.0 + 0.0005)

        if net_tp1 < sp.min_tp1_net_pct or stop <= 0:
            return None

        # 6. Математический скоринг сетапа (Защищенный)
        min_obi_safe = max(float(sp.min_obi), 1e-6)
        obi_score = min(obi_str / min_obi_safe, 2.0) * 20.0
        cvd_score = min(flow_align * 20.0, 20.0)
        width_score = min(width_pct / min_width, 2.0) * 7.5
        
        final_score = round(proximity + obi_score + cvd_score + width_score, 2)
        decision = "approve" if final_score >= sp.min_setup_score else "wait"

        # Если сетап не набрал проходной балл, уничтожаем сигнал (требование Капитана)
        if decision == "wait":
            return None

        confidence = round(min(50.0 + final_score * 0.3, 80.0), 2)

        setup_quality = {
            "strategy": "micro_flow_scalp",
            "micro_low": round(low, 8),
            "micro_high": round(high, 8),
            "micro_width_pct": round(width_pct, 3),
            "price_position": round(pos, 3),
            "obi": round(obi, 3),
            "cvd_ratio": round(cvd_ratio, 3),
            "m5_trend": m5_trend,
            "proximity": round(proximity, 2),
            "obi_score": round(obi_score, 2),
            "cvd_score": round(cvd_score, 2),
            "width_score": round(width_score, 2),
            "final_score": final_score,
            "decision": decision,
        }

        return ScalpSignal(
            action=direction,
            regime="scalp",
            entry_zone=[round(entry_low, 8), round(entry_high, 8)],
            stop_price=round(stop, 8),
            tp={"tp1": round(tp1, 8), "tp2": round(tp2, 8)},
            confidence_hint=confidence,
            reason=f"micro_scalp_{'long_support' if direction == 'long' else 'short_resistance'}_flow",
            setup_quality=setup_quality,
            setup_decision=decision,
        )
