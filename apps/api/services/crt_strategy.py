"""CRT (Candle Range Theory) — 3-свечной вход: Accumulation → Manipulation → Distribution.

Логика (ICT / Smart Money):
  C1 (Accumulation): старшая свеча (4h) задаёт диапазон CRH/CRL.
  C2 (Manipulation): свип CRH или CRL, НО закрытие обратно ВНУТРИ C1 (turtle soup).
  C3 (Distribution): вход на LTF (15m/5m) по подтверждению — MSS и/или FVG,
                     в premium-зоне (для шорта) или discount-зоне (для лонга).
  SL: за хвост C2. TP1: противоположная ликвидность (CRL для шорта, CRH для лонга).
  TP2: дальше, по R:R (1:2/1:3).

Чистые функции над списками свечей [{"open","high","low","close"}...] (закрытые,
старые→новые), поэтому тестируется без ccxt/pandas. lookahead исключён: используем
только ЗАКРЫТЫЕ свечи (формирующуюся отбрасываем на стороне вызова).
"""
from __future__ import annotations

from dataclasses import dataclass

from core.config import settings


@dataclass
class CRTSignal:
    action: str                 # "long" / "short"
    regime: str                 # "crt"
    entry_zone: list[float]
    stop_price: float
    tp: dict                    # {"tp1":..., "tp2":...}
    confidence_hint: float
    reason: str
    setup_quality: dict
    setup_decision: str


def _f(c, k):
    try:
        return float(c[k] if isinstance(c, dict) else getattr(c, k))
    except (KeyError, TypeError, ValueError, AttributeError):
        return 0.0


def detect_fvg(candles, direction: str, lookback: int = 12) -> bool:
    """3-свечной разрыв (imbalance) в сторону входа.
    bullish FVG: low[i] > high[i-2] (гэп вверх). bearish: high[i] < low[i-2]."""
    cs = candles[-lookback:]
    for i in range(2, len(cs)):
        a, c = cs[i - 2], cs[i]
        if direction == "long" and _f(c, "low") > _f(a, "high"):
            return True
        if direction == "short" and _f(c, "high") < _f(a, "low"):
            return True
    return False


def detect_mss(candles, direction: str, swing: int = 3, lookback: int = 12) -> bool:
    """Market Structure Shift: последняя закрытая свеча пробивает локальный
    экстремум предыдущего свинга в сторону входа."""
    cs = candles[-lookback:]
    if len(cs) < swing + 2:
        return False
    last = cs[-1]
    prior = cs[-(swing + 1):-1]
    if direction == "short":
        return _f(last, "close") < min(_f(c, "low") for c in prior)
    return _f(last, "close") > max(_f(c, "high") for c in prior)


class CRTStrategyService:
    def evaluate(self, htf_candles, ltf_candles, *, symbol: str | None = None,
                 current_price: float | None = None) -> CRTSignal | None:
        if not bool(getattr(settings, "ENABLE_CRT_STRATEGY", False)):
            return None
        if not htf_candles or len(htf_candles) < 2 or not ltf_candles or len(ltf_candles) < 5:
            return None

        c1, c2 = htf_candles[-2], htf_candles[-1]   # C1 диапазон, C2 манипуляция
        crh, crl = _f(c1, "high"), _f(c1, "low")
        rng = crh - crl
        if rng <= 0 or crl <= 0:
            return None

        width_pct = rng / crl * 100.0
        if width_pct < float(getattr(settings, "CRT_MIN_RANGE_PCT", 1.5)):
            return None

        price = float(current_price if current_price is not None else _f(ltf_candles[-1], "close"))
        if price <= 0:
            return None

        c2_close = _f(c2, "close")
        inside = crl <= c2_close <= crh
        swept_high = _f(c2, "high") > crh and inside    # медвежий CRT (шорт)
        swept_low = _f(c2, "low") < crl and inside      # бычий CRT (лонг)

        direction = None
        if swept_high and bool(getattr(settings, "CRT_ALLOW_SHORT", True)):
            direction = "short"
        elif swept_low and bool(getattr(settings, "CRT_ALLOW_LONG", True)):
            direction = "long"
        if direction is None:
            return None

        pos = (price - crl) / rng   # 0 = CRL, 1 = CRH
        if bool(getattr(settings, "CRT_REQUIRE_PREMIUM_DISCOUNT", True)):
            if direction == "short" and pos < 0.5:   # шорт только из premium
                return None
            if direction == "long" and pos > 0.5:    # лонг только из discount
                return None

        mode = str(getattr(settings, "CRT_LTF_CONFIRM", "either")).lower()
        mss = detect_mss(ltf_candles, direction)
        fvg = detect_fvg(ltf_candles, direction)
        if mode == "both" and not (mss and fvg):
            return None
        if mode == "either" and not (mss or fvg):
            return None

        fee_round_pct = float(settings.SPOT_TAKER_FEE) * 2 * 100.0
        min_tp1 = float(getattr(settings, "CRT_MIN_TP1_NET_PCT", 0.5))
        buf = rng * float(getattr(settings, "CRT_STOP_BUFFER_PCT", 0.05))
        rr = float(getattr(settings, "CRT_TP2_RR", 2.0))

        if direction == "short":
            entry = price
            stop = _f(c2, "high") + buf
            risk = stop - entry
            if risk <= 0:
                return None
            tp1 = crl
            tp2 = min(crl - rng * 0.1, entry - risk * rr)   # дальше CRL
            net_tp1 = (entry - tp1) / entry * 100.0 - fee_round_pct
        else:
            entry = price
            stop = _f(c2, "low") - buf
            risk = entry - stop
            if risk <= 0:
                return None
            tp1 = crh
            tp2 = max(crh + rng * 0.1, entry + risk * rr)   # дальше CRH
            net_tp1 = (tp1 - entry) / entry * 100.0 - fee_round_pct

        if net_tp1 < min_tp1:
            return None

        # Скоринг сетапа.
        sweep_depth = (abs(_f(c2, "high") - crh) if direction == "short" else abs(crl - _f(c2, "low"))) / rng
        sweep_score = min(sweep_depth * 100.0, 25.0)
        extremity = (pos if direction == "short" else (1.0 - pos)) * 30.0
        confirm_score = (20.0 if mss else 0.0) + (15.0 if fvg else 0.0)
        width_score = min(width_pct / float(getattr(settings, "CRT_MIN_RANGE_PCT", 1.5)), 2.0) * 7.5
        final_score = round(sweep_score + extremity + confirm_score + width_score, 2)
        min_score = float(getattr(settings, "CRT_MIN_SETUP_SCORE", 55.0))
        decision = "approve" if final_score >= min_score else "wait"
        confidence = round(min(50.0 + final_score * 0.35, 85.0), 2)

        entry_low = min(entry, entry * 1.001)
        entry_high = max(entry, entry * 1.001)
        setup_quality = {
            "strategy": "crt_3candle",
            "crh": round(crh, 8), "crl": round(crl, 8),
            "range_width_pct": round(width_pct, 3),
            "price_position": round(pos, 3),
            "sweep": "CRH" if direction == "short" else "CRL",
            "sweep_depth_pct": round(sweep_depth * 100.0, 3),
            "mss": mss, "fvg": fvg,
            "c2_high": round(_f(c2, "high"), 8), "c2_low": round(_f(c2, "low"), 8),
            "sweep_score": round(sweep_score, 2), "extremity": round(extremity, 2),
            "confirm_score": confirm_score, "width_score": round(width_score, 2),
            "final_score": final_score, "decision": decision,
            "rr_tp2": rr,
        }
        return CRTSignal(
            action=direction, regime="crt",
            entry_zone=[round(entry_low, 8), round(entry_high, 8)],
            stop_price=round(stop, 8),
            tp={"tp1": round(tp1, 8), "tp2": round(tp2, 8)},
            confidence_hint=confidence,
            reason=f"crt_{'bear' if direction=='short' else 'bull'}_sweep_{'CRH' if direction=='short' else 'CRL'}",
            setup_quality=setup_quality, setup_decision=decision,
        )
