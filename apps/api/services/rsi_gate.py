"""Разделение RSI-фильтра: жёсткий блок против штрафа за поздний вход
(#rsi-dynamic-2026-07-27).

Проблема плоского порога. Правило «4h RSI ≥ 72 → не лонгуем» исходит из того,
что перекупленность означает разворот. В сильном тренде это неверно: RSI может
держаться выше 70 неделями, и именно там живёт основная часть движения. Плоский
порог одинаково режет и разгон к новым максимумам, и вход в вершину боковика —
хотя это противоположные ситуации.

При этом сам порог был введён по делу: BTC #264 (−4.54), ETH #276 (−4.92),
SOL #278 (−2.48) — все три входили в перегретый рынок. Отменять фильтр нельзя,
но и оставлять плоским — значит платить за него пропущенными трендами.

Разделение на три зоны вместо двух:

    RSI < dynamic_threshold        → вход без ограничений
    dynamic ≤ RSI < hard_block     → ПОЗДНИЙ ВХОД: разрешён, риск урезан
    RSI ≥ hard_block               → жёсткий блок, как раньше

`dynamic_threshold` поднимается тем выше, чем убедительнее тренд:

  * веер EMA (ema20−ema50), нормированный на ATR — структурная сила тренда,
    заменяет ADX, которого в контекстах нет;
  * `volume_ratio` — импульс объёма: разгон на объёме отличается от вялого
    сползания вверх;
  * согласие старших ТФ — 4h и 1h смотрят в одну сторону.

Каждый признак двигает порог на свою добавку, сумма ограничена потолком.
Смысл: сильному тренду разрешаем быть перекупленным дольше, слабому — нет.

Штраф за поздний вход — множитель риска, а не отказ. Вход в верхней части
движения статистически хуже раннего, но не безнадёжен; правильная реакция —
меньший размер, а не бинарный запрет. Это ровно та логика, которой не хватало:
раньше выбор был «полный размер» или «ничего».

Чистые функции без обращения к БД и сети — проверяются напрямую.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.config import settings


@dataclass(frozen=True)
class RsiGateDecision:
    zone: str                     # "clear" | "late_entry" | "hard_block"
    allowed: bool
    risk_multiplier: float
    rsi: float
    base_threshold: float
    dynamic_threshold: float
    hard_block: float
    trend_strength: float         # 0..1, насколько тренд убедителен
    reasons: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "zone": self.zone,
            "allowed": self.allowed,
            "risk_multiplier": round(self.risk_multiplier, 4),
            "rsi": round(self.rsi, 2),
            "base_threshold": round(self.base_threshold, 2),
            "dynamic_threshold": round(self.dynamic_threshold, 2),
            "hard_block": round(self.hard_block, 2),
            "trend_strength": round(self.trend_strength, 3),
            "reasons": list(self.reasons),
        }


def trend_strength(
    *,
    ema20: float | None,
    ema50: float | None,
    atr: float | None,
    volume_ratio: float | None,
    htf_aligned: bool,
    side: str,
) -> tuple[float, list[str]]:
    """Насколько убедителен тренд: 0 (никак) … 1 (разгон на объёме с согласием ТФ).

    Веер EMA нормируется на ATR намеренно. Абсолютный разрыв между EMA
    несравним между BTC и TRX; в единицах ATR — сравним, и порог получается
    один на все символы.
    """
    score = 0.0
    reasons: list[str] = []

    fan_w = float(getattr(settings, "RSI_DYN_FAN_WEIGHT", 0.45))
    vol_w = float(getattr(settings, "RSI_DYN_VOLUME_WEIGHT", 0.30))
    htf_w = float(getattr(settings, "RSI_DYN_HTF_WEIGHT", 0.25))

    try:
        if ema20 and ema50 and atr and float(atr) > 0:
            gap = (float(ema20) - float(ema50)) / float(atr)
            if str(side).lower() == "short":
                gap = -gap
            full = float(getattr(settings, "RSI_DYN_FAN_FULL_ATR", 1.0))
            part = max(0.0, min(gap / full, 1.0)) if full > 0 else 0.0
            score += fan_w * part
            if part > 0:
                reasons.append(f"веер EMA {gap:.2f} ATR")
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    try:
        if volume_ratio is not None:
            strong = float(getattr(settings, "RSI_DYN_VOLUME_STRONG_RATIO", 1.5))
            part = max(0.0, min((float(volume_ratio) - 1.0) / max(strong - 1.0, 1e-9), 1.0))
            score += vol_w * part
            if part > 0:
                reasons.append(f"объём ×{float(volume_ratio):.2f}")
    except (TypeError, ValueError):
        pass

    if htf_aligned:
        score += htf_w
        reasons.append("старшие ТФ согласны")

    return max(0.0, min(score, 1.0)), reasons


def evaluate(
    *,
    side: str,
    rsi: float | None,
    ema20: float | None = None,
    ema50: float | None = None,
    atr: float | None = None,
    volume_ratio: float | None = None,
    htf_aligned: bool = False,
) -> RsiGateDecision:
    """Решение по RSI: чисто, ждать или входить уменьшенным размером."""
    side_l = str(side).lower()
    is_long = side_l == "long"

    base = float(getattr(
        settings,
        "TREND_HTF_RSI_HARD_OVERHEAT" if is_long else "TREND_HTF_RSI_HARD_OVERSOLD",
        72.0 if is_long else 28.0,
    ))
    lift_max = float(getattr(settings, "RSI_DYN_MAX_LIFT", 8.0))
    hard_gap = float(getattr(settings, "RSI_DYN_HARD_BLOCK_GAP", 10.0))
    late_mult = float(getattr(settings, "RSI_LATE_ENTRY_RISK_MULTIPLIER", 0.55))

    if rsi is None:
        return RsiGateDecision(
            zone="clear", allowed=True, risk_multiplier=1.0, rsi=float("nan"),
            base_threshold=base, dynamic_threshold=base,
            hard_block=base + hard_gap if is_long else base - hard_gap,
            trend_strength=0.0, reasons=["RSI недоступен — фильтр не применяется"],
        )

    rsi_v = float(rsi)
    strength, why = trend_strength(
        ema20=ema20, ema50=ema50, atr=atr, volume_ratio=volume_ratio,
        htf_aligned=htf_aligned, side=side_l,
    )

    if not bool(getattr(settings, "RSI_DYNAMIC_ENABLED", True)):
        # Откат к прежнему поведению одним флагом: плоский порог, бинарный ответ.
        blocked = rsi_v >= base if is_long else rsi_v <= base
        return RsiGateDecision(
            zone="hard_block" if blocked else "clear",
            allowed=not blocked, risk_multiplier=0.0 if blocked else 1.0,
            rsi=rsi_v, base_threshold=base, dynamic_threshold=base, hard_block=base,
            trend_strength=strength, reasons=["динамический порог выключен"],
        )

    lift = lift_max * strength
    if is_long:
        dynamic = base + lift
        hard = dynamic + hard_gap
        if rsi_v >= hard:
            zone, allowed, mult = "hard_block", False, 0.0
        elif rsi_v >= dynamic:
            zone, allowed, mult = "late_entry", True, late_mult
        else:
            zone, allowed, mult = "clear", True, 1.0
    else:
        dynamic = base - lift
        hard = dynamic - hard_gap
        if rsi_v <= hard:
            zone, allowed, mult = "hard_block", False, 0.0
        elif rsi_v <= dynamic:
            zone, allowed, mult = "late_entry", True, late_mult
        else:
            zone, allowed, mult = "clear", True, 1.0

    reasons = list(why)
    if zone == "late_entry":
        reasons.append(
            f"поздний вход: RSI {rsi_v:.1f} за динамическим порогом {dynamic:.1f}, "
            f"размер ×{late_mult}"
        )
    elif zone == "hard_block":
        reasons.append(f"жёсткий блок: RSI {rsi_v:.1f} за пределом {hard:.1f}")

    return RsiGateDecision(
        zone=zone, allowed=allowed, risk_multiplier=mult, rsi=rsi_v,
        base_threshold=base, dynamic_threshold=dynamic, hard_block=hard,
        trend_strength=strength, reasons=reasons,
    )
