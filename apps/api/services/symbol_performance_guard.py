from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from core.config import settings
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
    failed_setup_count: int
    positive_then_negative_count: int
    last_closed_reason: str | None
    losing_streak: int

    def to_payload(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "risk_multiplier": self.risk_multiplier,
            "symbol": self.symbol,
            "closed_count": self.closed_count,
            "wins": self.wins,
            "losses": self.losses,
            "winrate": self.winrate,
            "total_net_pnl": self.total_net_pnl,
            "stop_loss_count": self.stop_loss_count,
            "failed_setup_count": self.failed_setup_count,
            "positive_then_negative_count": self.positive_then_negative_count,
            "last_closed_reason": self.last_closed_reason,
            "losing_streak": self.losing_streak,
        }


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
                failed_setup_count=0,
                positive_then_negative_count=0,
                last_closed_reason=None,
                losing_streak=0,
            )

        closed_count = len(closed_signals)

        wins = 0
        losses = 0
        total_net_pnl = 0.0
        stop_loss_count = 0
        failed_setup_count = 0
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
            if signal.closed_reason == "failed_setup_exit":
                failed_setup_count += 1

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

        min_history = int(getattr(settings, "SYMBOL_PERF_MIN_HISTORY", 3))
        block_min_history = int(getattr(settings, "SYMBOL_PERF_BLOCK_MIN_HISTORY", 5))
        block_max_winrate = float(getattr(settings, "SYMBOL_PERF_BLOCK_MAX_WINRATE", 40.0))
        reduce_max_winrate = float(getattr(settings, "SYMBOL_PERF_REDUCE_MAX_WINRATE", 45.0))
        cooldown_streak = int(getattr(settings, "SYMBOL_PERF_COOLDOWN_STREAK", 3))
        cooldown_stops = int(getattr(settings, "SYMBOL_PERF_COOLDOWN_STOPS", 3))
        cooldown_failed_setups = int(getattr(settings, "SYMBOL_PERF_COOLDOWN_FAILED_SETUPS", 4))
        small_history_stop_multiplier = float(getattr(settings, "SYMBOL_PERF_SMALL_HISTORY_STOP_MULTIPLIER", 0.65))
        weak_multiplier = float(getattr(settings, "SYMBOL_PERF_WEAK_MULTIPLIER", 0.45))
        giveback_multiplier = float(getattr(settings, "SYMBOL_PERF_GIVEBACK_MULTIPLIER", 0.60))
        giveback_trigger = int(getattr(settings, "SYMBOL_PERF_GIVEBACK_TRIGGER", 3))

        # Мало истории — не блокируем, но можем слегка уменьшить риск после стопа.
        if closed_count < min_history:
            if last_closed_reason == "stop_loss":
                return SymbolPerformanceDecision(
                    allowed=True,
                    reason="small_history_last_stop_reduce_risk",
                    risk_multiplier=small_history_stop_multiplier,
                    symbol=symbol,
                    closed_count=closed_count,
                    wins=wins,
                    losses=losses,
                    winrate=winrate,
                    total_net_pnl=total_net_pnl,
                    stop_loss_count=stop_loss_count,
                    failed_setup_count=failed_setup_count,
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
                failed_setup_count=failed_setup_count,
                positive_then_negative_count=positive_then_negative_count,
                last_closed_reason=last_closed_reason,
                losing_streak=losing_streak,
            )

        # Жёсткий cooldown: серия стопов.
        if losing_streak >= cooldown_streak and stop_loss_count >= cooldown_stops:
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
                failed_setup_count=failed_setup_count,
                positive_then_negative_count=positive_then_negative_count,
                last_closed_reason=last_closed_reason,
                losing_streak=losing_streak,
            )


        # Отдельный cooldown, если символ часто закрывается как failed_setup_exit.
        # Это типичный ранний индикатор, что входы/таймфрейм/структура для монеты сейчас плохие.
        if losing_streak >= cooldown_streak and failed_setup_count >= cooldown_failed_setups:
            return SymbolPerformanceDecision(
                allowed=False,
                reason="symbol_cooldown_failed_setup_streak",
                risk_multiplier=0.0,
                symbol=symbol,
                closed_count=closed_count,
                wins=wins,
                losses=losses,
                winrate=winrate,
                total_net_pnl=total_net_pnl,
                stop_loss_count=stop_loss_count,
                failed_setup_count=failed_setup_count,
                positive_then_negative_count=positive_then_negative_count,
                last_closed_reason=last_closed_reason,
                losing_streak=losing_streak,
            )

        # Символ статистически убыточный.
        if closed_count >= block_min_history and total_net_pnl < 0 and winrate < block_max_winrate:
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
                failed_setup_count=failed_setup_count,
                positive_then_negative_count=positive_then_negative_count,
                last_closed_reason=last_closed_reason,
                losing_streak=losing_streak,
            )

        # Символ слабый, но не катастрофа — разрешаем с пониженным риском.
        if total_net_pnl < 0 or winrate < reduce_max_winrate:
            return SymbolPerformanceDecision(
                allowed=True,
                reason="symbol_weak_reduce_risk",
                risk_multiplier=weak_multiplier,
                symbol=symbol,
                closed_count=closed_count,
                wins=wins,
                losses=losses,
                winrate=winrate,
                total_net_pnl=total_net_pnl,
                stop_loss_count=stop_loss_count,
                failed_setup_count=failed_setup_count,
                positive_then_negative_count=positive_then_negative_count,
                last_closed_reason=last_closed_reason,
                losing_streak=losing_streak,
            )

        # Много positive_then_negative — значит надо защищать прибыль раньше.
        if positive_then_negative_count >= giveback_trigger:
            return SymbolPerformanceDecision(
                allowed=True,
                reason="symbol_gives_back_profit_reduce_risk",
                risk_multiplier=giveback_multiplier,
                symbol=symbol,
                closed_count=closed_count,
                wins=wins,
                losses=losses,
                winrate=winrate,
                total_net_pnl=total_net_pnl,
                stop_loss_count=stop_loss_count,
                failed_setup_count=failed_setup_count,
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
            failed_setup_count=failed_setup_count,
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
            "failed_setup_count": decision.failed_setup_count,
            "positive_then_negative_count": decision.positive_then_negative_count,
            "last_closed_reason": decision.last_closed_reason,
            "losing_streak": decision.losing_streak,
        }
