from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from models.signal import Signal


@dataclass
class SymbolPerformanceDecision:
    allowed: bool
    reason: str
    risk_multiplier: float

    symbol: str
    closed_count: int
    wins: int
    losses: int
    winrate: float
    total_net_pnl: float
    stop_loss_count: int
    positive_then_negative_count: int
    last_closed_reason: str | None
    losing_streak: int


class SymbolPerformanceGuard:
    """
    Адаптивный штрафной слой по символу.

    Цель:
    - не запрещать обучение полностью;
    - но не давать монете с плохой статистикой продолжать жечь депозит;
    - снижать размер сделки или ставить cooldown после серии stop_loss.
    """

    def analyze(
        self,
        db: Session,
        bot_id: int,
        symbol: str,
        lookback: int = 12,
    ) -> SymbolPerformanceDecision:
        closed_signals = (
            db.query(Signal)
            .filter(
                Signal.bot_id == bot_id,
                Signal.symbol == symbol,
                Signal.status == "closed",
            )
            .order_by(Signal.id.desc())
            .limit(lookback)
            .all()
        )

        if not closed_signals:
            return SymbolPerformanceDecision(
                allowed=True,
                reason="no_history",
                risk_multiplier=1.0,
                symbol=symbol,
                closed_count=0,
                wins=0,
                losses=0,
                winrate=0.0,
                total_net_pnl=0.0,
                stop_loss_count=0,
                positive_then_negative_count=0,
                last_closed_reason=None,
                losing_streak=0,
            )

        closed_count = len(closed_signals)

        wins = 0
        losses = 0
        total_net_pnl = 0.0
        stop_loss_count = 0
        positive_then_negative_count = 0
        losing_streak = 0

        streak_active = True

        for signal in closed_signals:
            net_pnl = float(signal.closed_net_pnl or 0)
            total_net_pnl += net_pnl

            if net_pnl > 0:
                wins += 1
                streak_active = False
            else:
                losses += 1
                if streak_active:
                    losing_streak += 1

            if signal.closed_reason == "stop_loss":
                stop_loss_count += 1

            lifecycle = {}
            try:
                lifecycle = (signal.plan_json or {}).get("lifecycle") or {}
            except Exception:
                lifecycle = {}

            if lifecycle.get("positive_then_negative"):
                positive_then_negative_count += 1

        winrate = round((wins / closed_count * 100), 2) if closed_count else 0.0
        total_net_pnl = round(total_net_pnl, 6)

        last_closed_reason = closed_signals[0].closed_reason

        # Мало истории — не блокируем, но можем слегка уменьшить риск после стопа.
        if closed_count < 3:
            if last_closed_reason == "stop_loss":
                return SymbolPerformanceDecision(
                    allowed=True,
                    reason="small_history_last_stop_reduce_risk",
                    risk_multiplier=0.65,
                    symbol=symbol,
                    closed_count=closed_count,
                    wins=wins,
                    losses=losses,
                    winrate=winrate,
                    total_net_pnl=total_net_pnl,
                    stop_loss_count=stop_loss_count,
                    positive_then_negative_count=positive_then_negative_count,
                    last_closed_reason=last_closed_reason,
                    losing_streak=losing_streak,
                )

            return SymbolPerformanceDecision(
                allowed=True,
                reason="small_history_ok",
                risk_multiplier=1.0,
                symbol=symbol,
                closed_count=closed_count,
                wins=wins,
                losses=losses,
                winrate=winrate,
                total_net_pnl=total_net_pnl,
                stop_loss_count=stop_loss_count,
                positive_then_negative_count=positive_then_negative_count,
                last_closed_reason=last_closed_reason,
                losing_streak=losing_streak,
            )

        # Жёсткий cooldown: серия стопов.
        if losing_streak >= 3 and stop_loss_count >= 3:
            return SymbolPerformanceDecision(
                allowed=False,
                reason="symbol_cooldown_losing_streak",
                risk_multiplier=0.0,
                symbol=symbol,
                closed_count=closed_count,
                wins=wins,
                losses=losses,
                winrate=winrate,
                total_net_pnl=total_net_pnl,
                stop_loss_count=stop_loss_count,
                positive_then_negative_count=positive_then_negative_count,
                last_closed_reason=last_closed_reason,
                losing_streak=losing_streak,
            )

        # Символ статистически убыточный.
        if closed_count >= 5 and total_net_pnl < 0 and winrate < 40:
            return SymbolPerformanceDecision(
                allowed=False,
                reason="symbol_negative_expectancy_blocked",
                risk_multiplier=0.0,
                symbol=symbol,
                closed_count=closed_count,
                wins=wins,
                losses=losses,
                winrate=winrate,
                total_net_pnl=total_net_pnl,
                stop_loss_count=stop_loss_count,
                positive_then_negative_count=positive_then_negative_count,
                last_closed_reason=last_closed_reason,
                losing_streak=losing_streak,
            )

        # Символ слабый, но не катастрофа — разрешаем с пониженным риском.
        if total_net_pnl < 0 or winrate < 45:
            return SymbolPerformanceDecision(
                allowed=True,
                reason="symbol_weak_reduce_risk",
                risk_multiplier=0.45,
                symbol=symbol,
                closed_count=closed_count,
                wins=wins,
                losses=losses,
                winrate=winrate,
                total_net_pnl=total_net_pnl,
                stop_loss_count=stop_loss_count,
                positive_then_negative_count=positive_then_negative_count,
                last_closed_reason=last_closed_reason,
                losing_streak=losing_streak,
            )

        # Много positive_then_negative — значит надо защищать прибыль раньше.
        if positive_then_negative_count >= 3:
            return SymbolPerformanceDecision(
                allowed=True,
                reason="symbol_gives_back_profit_reduce_risk",
                risk_multiplier=0.60,
                symbol=symbol,
                closed_count=closed_count,
                wins=wins,
                losses=losses,
                winrate=winrate,
                total_net_pnl=total_net_pnl,
                stop_loss_count=stop_loss_count,
                positive_then_negative_count=positive_then_negative_count,
                last_closed_reason=last_closed_reason,
                losing_streak=losing_streak,
            )

        return SymbolPerformanceDecision(
            allowed=True,
            reason="symbol_performance_ok",
            risk_multiplier=1.0,
            symbol=symbol,
            closed_count=closed_count,
            wins=wins,
            losses=losses,
            winrate=winrate,
            total_net_pnl=total_net_pnl,
            stop_loss_count=stop_loss_count,
            positive_then_negative_count=positive_then_negative_count,
            last_closed_reason=last_closed_reason,
            losing_streak=losing_streak,
        )

    def to_dict(self, decision: SymbolPerformanceDecision) -> dict:
        return {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "risk_multiplier": decision.risk_multiplier,
            "symbol": decision.symbol,
            "closed_count": decision.closed_count,
            "wins": decision.wins,
            "losses": decision.losses,
            "winrate": decision.winrate,
            "total_net_pnl": decision.total_net_pnl,
            "stop_loss_count": decision.stop_loss_count,
            "positive_then_negative_count": decision.positive_then_negative_count,
            "last_closed_reason": decision.last_closed_reason,
            "losing_streak": decision.losing_streak,
        }