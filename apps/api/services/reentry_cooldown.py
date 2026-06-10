from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from models.signal import Signal


@dataclass
class ReEntryCooldownDecision:
    allowed: bool
    reason: str
    payload: dict


class ReEntryCooldownGuard:
    """
    Защита от повторного входа в тот же symbol + side сразу после закрытия.

    Проблема:
    - робот закрывает слабый LINK long;
    - через несколько минут снова видит LINK long;
    - снова открывает почти тот же сетап;
    - комиссии + шум съедают депозит.

    Решение:
    - после плохого закрытия ставим cooldown по symbol + side;
    - разрешаем повтор только если новый сетап намного сильнее.
    """

    COOLDOWN_MINUTES = {
        "failed_setup_exit": 120,          # 2h — position never confirmed direction
        "low_grade_capital_release": 120,
        "stop_loss": 180,                  # 3h — full stop hit, market moved against us
        "protective_breakeven_profit_guard": 90,  # raised 60→90: re-entering after micro-exit burns fees
        "adaptive_mfe_capture": 60,        # new: captured early profit, wait for fresh setup
        "protective_trailing_stop": 45,
        "adaptive_trailing_stop": 45,
        "adaptive_post_tp1_stop": 30,
        "trend_trailing_stop": 30,
        "tp1_reached": 30,
        "tp2_reached": 30,
    }

    def _now(self):
        return datetime.now(timezone.utc)

    def _as_aware(self, dt):
        if dt is None:
            return None

        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)

        return dt

    def _safe_float(self, value, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    def _last_priority_score(self, signal: Signal) -> float:
        plan = signal.plan_json or {}
        return self._safe_float(plan.get("priority_score"), 0.0)

    def _is_strong_override(
        self,
        *,
        current_priority_score: float,
        current_setup_score: float,
        current_rr_tp2: float,
        last_signal: Signal,
    ) -> bool:
        """
        Разрешаем повторный вход во время cooldown только если новый сетап
        явно сильнее старого.
        """

        last_priority_score = self._last_priority_score(last_signal)
        priority_delta = current_priority_score - last_priority_score

        return (
            current_priority_score >= 115
            and current_setup_score >= 90
            and current_rr_tp2 >= 1.9
            and priority_delta >= 25
        )

    def check(
        self,
        *,
        db,
        bot_id: int,
        symbol: str,
        side: str,
        current_priority_score: float,
        current_setup_score: float,
        current_rr_tp2: float,
    ) -> ReEntryCooldownDecision:
        last_signal = (
            db.query(Signal)
            .filter(
                Signal.bot_id == bot_id,
                Signal.symbol == symbol,
                Signal.side == side,
                Signal.status == "closed",
            )
            .order_by(Signal.closed_at.desc().nullslast(), Signal.id.desc())
            .first()
        )

        if not last_signal:
            return ReEntryCooldownDecision(
                allowed=True,
                reason="no_previous_closed_signal",
                payload={
                    "symbol": symbol,
                    "side": side,
                },
            )

        closed_reason = str(last_signal.closed_reason or "unknown")
        cooldown_minutes = int(self.COOLDOWN_MINUTES.get(closed_reason, 60))

        closed_at = self._as_aware(last_signal.closed_at or last_signal.created_at)

        if closed_at is None:
            return ReEntryCooldownDecision(
                allowed=True,
                reason="previous_signal_has_no_close_time",
                payload={
                    "last_signal_id": last_signal.id,
                    "closed_reason": closed_reason,
                },
            )

        now = self._now()
        cooldown_until = closed_at + timedelta(minutes=cooldown_minutes)

        if now >= cooldown_until:
            return ReEntryCooldownDecision(
                allowed=True,
                reason="cooldown_expired",
                payload={
                    "last_signal_id": last_signal.id,
                    "closed_reason": closed_reason,
                    "closed_at": closed_at.isoformat(),
                    "cooldown_until": cooldown_until.isoformat(),
                    "cooldown_minutes": cooldown_minutes,
                },
            )

        if self._is_strong_override(
            current_priority_score=float(current_priority_score or 0),
            current_setup_score=float(current_setup_score or 0),
            current_rr_tp2=float(current_rr_tp2 or 0),
            last_signal=last_signal,
        ):
            return ReEntryCooldownDecision(
                allowed=True,
                reason="cooldown_overridden_by_stronger_setup",
                payload={
                    "last_signal_id": last_signal.id,
                    "closed_reason": closed_reason,
                    "last_priority_score": self._last_priority_score(last_signal),
                    "current_priority_score": current_priority_score,
                    "current_setup_score": current_setup_score,
                    "current_rr_tp2": current_rr_tp2,
                    "cooldown_until": cooldown_until.isoformat(),
                },
            )

        return ReEntryCooldownDecision(
            allowed=False,
            reason="reentry_cooldown_active",
            payload={
                "symbol": symbol,
                "side": side,
                "last_signal_id": last_signal.id,
                "last_closed_reason": closed_reason,
                "last_closed_at": closed_at.isoformat(),
                "cooldown_minutes": cooldown_minutes,
                "cooldown_until": cooldown_until.isoformat(),
                "current_priority_score": current_priority_score,
                "current_setup_score": current_setup_score,
                "current_rr_tp2": current_rr_tp2,
            },
        )