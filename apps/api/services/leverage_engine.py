"""Smart leverage & portfolio risk budget — Фаза 4 (каркас).

«Видим бурю и силу — включаем плечо в работу, в разумных пределах.»

Плечо НЕ фиксированное, а зависит от conviction:
  conviction = grade_mult × trend_strength × volatility_factor   (0..1)
  leverage   = 1 + conviction × (MAX_LEVERAGE − 1)

Сверху — два жёстких предохранителя (догма):
  1) MAX_LEVERAGE — потолок плеча;
  2) PORTFOLIO_RISK_BUDGET_PCT — суммарный риск по ВСЕМ открытым сделкам.
     Новая сделка масштабируется/блокируется, чтобы не превысить бюджет.

Выключено по умолчанию (ENABLE_SMART_LEVERAGE=False → leverage=1.0, без эффекта).
Чистые функции — тестируются без ccxt/pandas. Реальное применение (множитель к
qty/margin) подключается в TradePlanBuilder только при ENABLE_FUTURES_EXECUTION.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.config import settings


@dataclass
class LeverageDecision:
    leverage: float        # итоговое плечо (≥1.0)
    risk_pct: float        # риск этой сделки в % эквити (с учётом плеча)
    conviction: float      # 0..1 — насколько «уверены»
    allowed: bool          # False = портфельный бюджет исчерпан
    reason: str


class LeverageEngine:
    def _grade_mult(self, grade: str | None) -> float:
        return {
            "A+": float(getattr(settings, "LEVERAGE_GRADE_A_PLUS", 1.0)),
            "A": float(getattr(settings, "LEVERAGE_GRADE_A", 0.7)),
            "B": float(getattr(settings, "LEVERAGE_GRADE_B", 0.4)),
        }.get(str(grade or ""), 0.0)

    def _volatility_factor(self, volatility_state: str | None) -> float:
        # «Буря» = расширение волатильности → сила движения.
        return {
            "expanding": 1.0,
            "high": 1.0,
            "normal": 0.6,
            "low": 0.3,
        }.get(str(volatility_state or "normal"), 0.5)

    def compute(
        self,
        *,
        grade: str | None,
        trend_strength: float,          # 0..1 (выравнивание ТФ / сила тренда)
        volatility_state: str | None,
        base_risk_pct: float,           # риск на сделку без плеча, % эквити
        open_portfolio_risk_pct: float = 0.0,
    ) -> LeverageDecision:
        base_risk = max(float(base_risk_pct), 0.0)

        if not bool(getattr(settings, "ENABLE_SMART_LEVERAGE", False)):
            return LeverageDecision(1.0, base_risk, 0.0, True, "smart_leverage_disabled")

        max_lev = max(float(getattr(settings, "MAX_LEVERAGE", 3.0)), 1.0)
        ts = min(max(float(trend_strength or 0.0), 0.0), 1.0)
        conviction = self._grade_mult(grade) * ts * self._volatility_factor(volatility_state)
        conviction = min(max(conviction, 0.0), 1.0)

        leverage = 1.0 + conviction * (max_lev - 1.0)
        leverage = min(max(leverage, 1.0), max_lev)

        # Портфельный риск-бюджет: риск ≈ base_risk × leverage.
        budget = max(float(getattr(settings, "PORTFOLIO_RISK_BUDGET_PCT", 6.0)), 0.0)
        open_risk = max(float(open_portfolio_risk_pct or 0.0), 0.0)
        room = budget - open_risk

        if base_risk <= 0:
            return LeverageDecision(round(leverage, 2), 0.0, round(conviction, 3), True, "conviction_sized")

        if room <= 0:
            return LeverageDecision(1.0, base_risk, round(conviction, 3), False, "portfolio_risk_budget_full")

        trade_risk = base_risk * leverage
        if trade_risk > room:
            # Ужимаем плечо, чтобы вписаться в остаток бюджета.
            leverage = max(1.0, min(leverage, room / base_risk))
            trade_risk = base_risk * leverage
            reason = "conviction_sized_capped_by_budget"
        else:
            reason = "conviction_sized"

        return LeverageDecision(
            leverage=round(leverage, 2),
            risk_pct=round(trade_risk, 3),
            conviction=round(conviction, 3),
            allowed=True,
            reason=reason,
        )
