"""Range / mean-reversion strategy (scalp) для бокового рынка.

Когда старший таймфрейм (4h) НЕ в тренде, трендовое продолжение простаивает —
а боковик может идти неделями. Этот модуль ловит отскоки внутри коридора.

На споте исполняется только ЛОНГ от нижней границы (шорт от сопротивления
требует futures — Phase 4). Логика — чистые функции над уже посчитанными
TimeframeContext (support/resistance/atr/rsi/volume_state), поэтому модуль
тестируется без ccxt/pandas.

Сделки помечаются режимом "range" → robot_loop ставит trade_mode="scalp",
и exit-политика ведёт их быстрым тейком (без trend-ride).
"""
from __future__ import annotations

from dataclasses import dataclass

from core.config import settings


@dataclass
class RangeSignal:
    action: str                 # всегда "long" на споте
    regime: str                 # "range"
    entry_zone: list[float]
    stop_price: float
    tp: dict                    # {"tp1": ..., "tp2": ...}
    confidence_hint: float
    reason: str
    setup_quality: dict
    setup_decision: str         # "approve" / "wait" / "hold"


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


class RangeStrategyService:
    """Оценивает символ на range-вход. Возвращает RangeSignal или None."""

    def evaluate(self, contexts, symbol: str | None = None) -> RangeSignal | None:
        if not bool(getattr(settings, "ENABLE_RANGE_STRATEGY", False)):
            return None

        work = _ctx(contexts, "1h")   # коридор берём с 1h (≈ 2 дня tail-50)
        h4 = _ctx(contexts, "4h")
        m15 = _ctx(contexts, "15m")
        m5 = _ctx(contexts, "5m")
        if work is None or m15 is None:
            return None

        # 1) Режим RANGE: 4h НЕ в выраженном тренде.
        if str(_v(h4, "trend", "mixed")) in ("trend_up", "trend_down"):
            return None

        low = float(_v(work, "support", 0.0))
        high = float(_v(work, "resistance", 0.0))
        price = float(_v(work, "last_close", 0.0))
        atr = float(_v(work, "atr14", 0.0))
        if low <= 0 or high <= low or price <= 0:
            return None

        # 2) Ширина коридора достаточна, чтобы заработать после комиссий.
        width_pct = (high - low) / low * 100.0
        min_width = float(getattr(settings, "RANGE_MIN_WIDTH_PCT", 2.5))
        if width_pct < min_width:
            return None

        # 3) Цена у нижней границы (зона лонга).
        pos = (price - low) / (high - low)   # 0 = поддержка, 1 = сопротивление
        support_zone = float(getattr(settings, "RANGE_SUPPORT_ZONE", 0.30))
        if pos > support_zone:
            return None

        # 4) Подтверждение разворота: 15m RSI в зоне «перепродан→нейтрально».
        rsi15 = float(_v(m15, "rsi14", 50.0))
        rsi_min = float(getattr(settings, "RANGE_ENTRY_RSI_MIN", 25.0))
        rsi_max = float(getattr(settings, "RANGE_ENTRY_RSI_MAX", 52.0))
        reversal_ok = rsi_min <= rsi15 <= rsi_max

        # 5) Уровни.
        buffer = max(atr * float(getattr(settings, "RANGE_STOP_ATR_MULT", 0.5)), low * 0.002)
        stop = low - buffer
        mid = (low + high) / 2.0
        tp1 = mid
        tp2 = high - (high - low) * float(getattr(settings, "RANGE_TP2_RESISTANCE_BUFFER", 0.10))
        entry_low = price
        entry_high = price * 1.001

        # 6) Проверка: до TP1 хватает хода после round-trip комиссий.
        fee_round_pct = float(settings.SPOT_TAKER_FEE) * 2 * 100.0
        tp1_move_pct = (tp1 - price) / price * 100.0
        min_tp1_net = float(getattr(settings, "RANGE_MIN_TP1_NET_PCT", 0.8))
        if (tp1_move_pct - fee_round_pct) < min_tp1_net:
            return None

        # 7) Скоринг range-сетапа (без trend_alignment — он тут не нужен).
        proximity = (1.0 - pos) * 40.0                              # ближе к поддержке → до 40
        width_score = min(width_pct / min_width, 2.0) * 15.0        # до 30
        reversal_score = 20.0 if reversal_ok else 5.0
        vol_state = str(_v(m15, "volume_state", "weak"))
        vol_score = {"strong": 10.0, "normal": 6.0}.get(vol_state, 2.0)
        final_score = round(proximity + width_score + reversal_score + vol_score, 2)

        min_score = float(getattr(settings, "RANGE_MIN_SETUP_SCORE", 60.0))
        decision = "approve" if (final_score >= min_score and reversal_ok) else "wait"

        confidence = round(min(50.0 + final_score * 0.3, 80.0), 2)

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
            "comment": "range_long_at_support",
        }

        return RangeSignal(
            action="long",
            regime="range",
            entry_zone=[round(entry_low, 8), round(entry_high, 8)],
            stop_price=round(stop, 8),
            tp={"tp1": round(tp1, 8), "tp2": round(tp2, 8)},
            confidence_hint=confidence,
            reason="range_long_support_bounce",
            setup_quality=setup_quality,
            setup_decision=decision,
        )
