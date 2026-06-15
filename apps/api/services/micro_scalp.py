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

from core.config import settings
from core.strategy_profiles import get_profiles


@dataclass
class ScalpSignal:
    action: str
    regime: str          # "scalp"
    entry_zone: list[float]
    stop_price: float
    tp: dict
    confidence_hint: float
    reason: str
    setup_quality: dict
    setup_decision: str


def _ctx(contexts, tf):
    if not isinstance(contexts, dict):
        return None
    return contexts.get(tf)


def _v(ctx, key, default=0.0):
    if ctx is None:
        return default
    if isinstance(ctx, dict):
        return ctx.get(key, default)
    return getattr(ctx, key, default)


class MicroScalpService:
    """Оценивает символ на micro-scalp вход. depth — dict из OrderBookAnalyzer.analyze().as_dict()."""

    def evaluate(self, contexts, depth: dict | None = None, symbol: str | None = None) -> ScalpSignal | None:
        sp = get_profiles().scalp_engine
        if not sp.enabled:
            return None

        m5 = _ctx(contexts, "5m")
        if m5 is None:
            return None

        # Скальп без живого стакана не торгует — поток это его инструмент.
        depth = depth or {}
        fresh = bool(depth.get("fresh", False))
        if sp.require_depth and not fresh:
            return None
        obi = float(depth.get("obi", 0.0))
        spread = depth.get("spread_pct", None)
        cvd_ratio = float(depth.get("cvd_ratio", 0.0))

        if spread is not None and float(spread) > sp.max_spread_pct:
            return None  # дорого для скальпа

        low = float(_v(m5, "support", 0.0))
        high = float(_v(m5, "resistance", 0.0))
        price = float(_v(m5, "last_close", 0.0))
        atr = float(_v(m5, "atr14", 0.0))
        if low <= 0 or high <= low or price <= 0:
            return None

        width_pct = (high - low) / low * 100.0
        if width_pct < sp.min_micro_width_pct:
            return None
        if atr <= 0:
            atr = price * 0.001

        pos = (price - low) / (high - low)   # 0 = микро-поддержка, 1 = микро-сопротивление
        m5_trend = str(_v(m5, "trend", "mixed"))
        fee_round_pct = float(settings.SPOT_TAKER_FEE) * 2 * 100.0
        buf = atr * sp.stop_buffer_atr

        direction = None
        if pos <= sp.edge_zone and m5_trend != "trend_down" and obi >= sp.min_obi:
            direction = "long"
        elif (sp.allow_short and pos >= (1.0 - sp.edge_zone)
              and m5_trend != "trend_up" and obi <= -sp.min_obi):
            direction = "short"
        if direction is None:
            return None

        if direction == "long":
            entry = price
            stop = low - buf
            tp1 = price * (1.0 + sp.target_pct / 100.0)
            tp2 = price * (1.0 + sp.target_pct * sp.tp2_mult / 100.0)
            net_tp1 = (tp1 - entry) / entry * 100.0 - fee_round_pct
            flow_align = max(0.0, cvd_ratio)        # покупки помогают лонгу
            obi_str = obi
        else:
            entry = price
            stop = high + buf
            tp1 = price * (1.0 - sp.target_pct / 100.0)
            tp2 = price * (1.0 - sp.target_pct * sp.tp2_mult / 100.0)
            net_tp1 = (entry - tp1) / entry * 100.0 - fee_round_pct
            flow_align = max(0.0, -cvd_ratio)       # продажи помогают шорту
            obi_str = -obi

        if net_tp1 < sp.min_tp1_net_pct:
            return None
        if stop <= 0:
            return None

        # Скоринг: близость к краю + сила OBI + согласие CVD + ширина.
        proximity = (1.0 - (pos if direction == "long" else (1.0 - pos))) * 35.0
        obi_score = min(obi_str / max(sp.min_obi, 1e-6), 2.0) * 20.0
        cvd_score = min(flow_align * 20.0, 20.0)
        width_score = min(width_pct / sp.min_micro_width_pct, 2.0) * 7.5
        final_score = round(proximity + obi_score + cvd_score + width_score, 2)
        decision = "approve" if final_score >= sp.min_setup_score else "wait"
        confidence = round(min(50.0 + final_score * 0.3, 80.0), 2)

        entry_low = min(entry, entry * 1.0005)
        entry_high = max(entry, entry * 1.0005)
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
            reason=f"micro_scalp_{'long_support' if direction=='long' else 'short_resistance'}_flow",
            setup_quality=setup_quality,
            setup_decision=decision,
        )
