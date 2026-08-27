"""Range / mean-reversion (свинг в боковике на 1h-коридоре).

ВАЖНО (редизайн): это НЕ скальпер — это свинг-фейд на 1h. Поэтому он обязан
работать ТОЛЬКО в подтверждённом боковике и не фейдить тренд:
  - 4h И 1h не в выраженном тренде;
  - не лонгуем падающий нож (15m trend_down у поддержки);
  - не шортим импульс вверх (15m trend_up у сопротивления) — это главный фикс
    «не отдавать деньги рынку», шорты по пробою прекращаются.

Конфиг — из get_profiles().range (Фаза 1). Сделки несут regime="range".
Чистые функции над TimeframeContext, тестируется без ccxt/pandas.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.config import settings
from core.strategy_profiles import get_profiles


@dataclass
class RangeSignal:
    action: str
    regime: str
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


_TRENDING = ("trend_up", "trend_down")


def _dynamic_tp2_buffer_mult(base_buffer: float, *, adx: float, atr_ratio: float) -> tuple[float, dict]:
    """(#range-tp2-dynamic-2026-08-27) Буфер TP2 (доля ширины диапазона, на
    которую TP2 не доходит до дальней границы) сужается — TP2 приближается к
    границе диапазона — при сильном локальном ADX/ATR-expansion. Источник
    ТОЛЬКО живые индикаторы этого бара. ТОЛЬКО приближает TP2 к границе,
    никогда не отдаляет его дальше base_buffer."""
    if not bool(getattr(settings, "RANGE_TP2_DYNAMIC_ENABLED", False)):
        return float(base_buffer), {"source": "disabled", "buffer": round(float(base_buffer), 4)}

    adx_base = float(getattr(settings, "RANGE_TP2_DYNAMIC_ADX_BASE", 15.0))
    adx_span = max(float(getattr(settings, "RANGE_TP2_DYNAMIC_ADX_SPAN", 15.0)), 1e-6)
    adx_component = min(max((float(adx) - adx_base) / adx_span, 0.0), 1.0)
    atr_component = min(max(float(atr_ratio) - 1.0, 0.0), 1.0)
    strength = min(max(0.7 * adx_component + 0.3 * atr_component, 0.0), 1.0)

    min_buffer = min(float(getattr(settings, "RANGE_TP2_DYNAMIC_MIN_BUFFER", 0.0)), float(base_buffer))
    dynamic_buffer = float(base_buffer) - strength * (float(base_buffer) - min_buffer)
    dynamic_buffer = max(min(dynamic_buffer, float(base_buffer)), min_buffer)

    return dynamic_buffer, {
        "source": f"dynamic(adx={adx_component:.2f},atr={atr_component:.2f},strength={strength:.2f})",
        "buffer": round(dynamic_buffer, 4), "base_buffer": round(float(base_buffer), 4),
    }


class RangeStrategyService:
    """Оценивает символ на range-вход. Возвращает RangeSignal или None."""

    def evaluate(self, contexts, symbol: str | None = None) -> RangeSignal | None:
        rp = get_profiles().range
        if not rp.enabled:
            return None

        work = _ctx(contexts, "1h")   # коридор берём с 1h
        h4 = _ctx(contexts, "4h")
        m15 = _ctx(contexts, "15m")
        if work is None or m15 is None:
            return None

        # 1) Подтверждённый боковик: 4h И 1h НЕ в тренде.
        if str(_v(h4, "trend", "mixed")) in _TRENDING:
            return None
        if rp.confirmed_range_only and str(_v(work, "trend", "mixed")) in _TRENDING:
            return None

        low = float(_v(work, "support", 0.0))
        high = float(_v(work, "resistance", 0.0))
        price = float(_v(work, "last_close", 0.0))
        atr = float(_v(work, "atr14", 0.0))
        if low <= 0 or high <= low or price <= 0:
            return None

        width_pct = (high - low) / low * 100.0
        min_width = rp.min_width_pct
        if width_pct < min_width:
            return None

        pos = (price - low) / (high - low)   # 0 = поддержка, 1 = сопротивление
        support_zone = rp.support_zone
        rsi15 = float(_v(m15, "rsi14", 50.0))
        rsi_min = rp.rsi_min
        rsi_max = rp.rsi_max
        atr_mult = rp.stop_atr_mult
        mid = (low + high) / 2.0
        atr_prev = float(_v(work, "atr14_prev", 0.0))
        atr_ratio = (atr / atr_prev) if atr_prev > 0 else 1.0
        adx = float(_v(work, "adx14", 0.0))
        dyn_buf_frac, tp2_dyn_meta = _dynamic_tp2_buffer_mult(
            rp.tp2_resistance_buffer, adx=adx, atr_ratio=atr_ratio)
        tp2_buf = (high - low) * dyn_buf_frac
        # Комиссия круга по ФАКТИЧЕСКОМУ маршруту исполнения. Была захардкожена
        # спотовая ставка (0.2%×2 = 0.4%), тогда как торгуем свопом (0.05%×2 +
        # проскальзывание = 0.12%) — издержки завышались вчетверо, и гейт
        # min_tp1_net резал живые боковики. Та же ошибка, что когда-то книжила
        # шорты по спотовой ставке.
        _mkt = str(getattr(settings, "execution_market_type", "spot"))
        _taker = float(settings.FUTURES_TAKER_FEE if _mkt == "swap" else settings.SPOT_TAKER_FEE)
        _slip = float(getattr(settings, "SLIPPAGE_BUFFER_PCT", 0.0) or 0.0)
        fee_round_pct = (_taker * 2 + _slip) * 100.0
        min_tp1_net = rp.min_tp1_net_pct
        vol_state = str(_v(m15, "volume_state", "weak"))
        vol_score = {"strong": 10.0, "normal": 6.0}.get(vol_state, 2.0)
        width_score = min(width_pct / min_width, 2.0) * 15.0
        min_score = rp.min_setup_score
        m15_trend = str(_v(m15, "trend", "mixed"))

        # 4) ЛОНГ от нижней границы (спот).
        if pos <= support_zone:
            # не ловим падающий нож: у поддержки 15m не должен валиться вниз
            if rp.confirmed_range_only and m15_trend == "trend_down":
                return None
            reversal_ok = rsi_min <= rsi15 <= rsi_max
            buffer = max(atr * atr_mult, low * 0.002)
            stop = low - buffer
            tp1 = mid
            tp2 = high - tp2_buf
            if (((tp1 - price) / price * 100.0) - fee_round_pct) < min_tp1_net:
                return None
            proximity = (1.0 - pos) * 40.0
            return self._mk_signal(
                "long", price, stop, tp1, tp2, pos, low, high, width_pct,
                proximity, width_score, reversal_ok, vol_score, rsi15, min_score,
                comment="range_long_at_support", reason="range_long_support_bounce",
                tp2_dynamic=tp2_dyn_meta,
            )

        # 5) ШОРТ от верхней границы (futures).
        if rp.allow_short and pos >= (1.0 - support_zone):
            # ГЛАВНЫЙ ФИКС: не шортим импульс вверх — у сопротивления 15m не должен
            # трендовать вверх (иначе фейдим пробой и кормим рынок).
            if rp.confirmed_range_only and m15_trend == "trend_up":
                return None
            reversal_ok = (100.0 - rsi_max) <= rsi15 <= (100.0 - rsi_min)
            buffer = max(atr * atr_mult, high * 0.002)
            stop = high + buffer
            tp1 = mid
            tp2 = low + tp2_buf
            if (((price - tp1) / price * 100.0) - fee_round_pct) < min_tp1_net:
                return None
            proximity = pos * 40.0
            return self._mk_signal(
                "short", price, stop, tp1, tp2, pos, low, high, width_pct,
                proximity, width_score, reversal_ok, vol_score, rsi15, min_score,
                comment="range_short_at_resistance", reason="range_short_resistance_reject",
                tp2_dynamic=tp2_dyn_meta,
            )

        return None

    def _mk_signal(self, action, price, stop, tp1, tp2, pos, low, high, width_pct,
                   proximity, width_score, reversal_ok, vol_score, rsi15, min_score,
                   *, comment, reason, tp2_dynamic: dict | None = None) -> RangeSignal:
        reversal_score = 20.0 if reversal_ok else 5.0
        final_score = round(proximity + width_score + reversal_score + vol_score, 2)
        decision = "approve" if (final_score >= min_score and reversal_ok) else "wait"
        confidence = round(min(50.0 + final_score * 0.3, 80.0), 2)
        entry_low = min(price, price * 1.001)
        entry_high = max(price, price * 1.001)
        setup_quality = {
            "strategy": "range_mean_reversion",
            "range_low": round(low, 8),
            "range_high": round(high, 8),
            "range_width_pct": round(width_pct, 3),
            "price_position": round(pos, 3),
            "proximity": round(proximity, 2),
            "width_score": round(width_score, 2),
            "reversal_score": reversal_score,
            "volume_confirmation": vol_score,
            "rsi_15m": round(rsi15, 2),
            "final_score": final_score,
            "decision": decision,
            "comment": comment,
            "tp2_dynamic": tp2_dynamic or {"source": "disabled"},
        }
        return RangeSignal(
            action=action,
            regime="range",
            entry_zone=[round(entry_low, 8), round(entry_high, 8)],
            stop_price=round(stop, 8),
            tp={"tp1": round(tp1, 8), "tp2": round(tp2, 8)},
            confidence_hint=confidence,
            reason=reason,
            setup_quality=setup_quality,
            setup_decision=decision,
        )
