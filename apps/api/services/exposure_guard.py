# apps/api/services/exposure_guard.py

from dataclasses import dataclass
from sqlalchemy.orm import Session

from models.signal import Signal


ACTIVE_SIGNAL_STATUSES = ["published", "opened", "tp1", "breakeven"]


@dataclass
class ExposureGuardResult:
    allowed: bool
    reason: str | None
    active_signals_count: int
    active_symbol_signals_count: int
    used_margin: float
    max_allowed_margin: float
    free_margin: float
    required_margin: float


class ExposureGuard:
    def active_signals(self, db: Session, bot_id: int):
        return (
            db.query(Signal)
            .filter(
                Signal.bot_id == bot_id,
                Signal.status.in_(ACTIVE_SIGNAL_STATUSES),
            )
            .order_by(Signal.id.desc())
            .all()
        )

    def active_signals_for_symbol(self, db: Session, bot_id: int, symbol: str):
        return (
            db.query(Signal)
            .filter(
                Signal.bot_id == bot_id,
                Signal.symbol == symbol,
                Signal.status.in_(ACTIVE_SIGNAL_STATUSES),
            )
            .order_by(Signal.id.desc())
            .all()
        )

    def estimate_signal_margin(self, signal: Signal) -> float:
        if getattr(signal, "required_margin", None):
            return float(signal.required_margin)

        plan_json = getattr(signal, "plan_json", None) or {}
        if isinstance(plan_json, dict) and plan_json.get("required_margin"):
            return float(plan_json["required_margin"])

        return 325.0

    def used_margin(self, db: Session, bot_id: int) -> float:
        total = 0.0

        for signal in self.active_signals(db, bot_id):
            total += self.estimate_signal_margin(signal)

        return round(total, 6)

    def check_before_publish(
        self,
        db: Session,
        bot_id: int,
        symbol: str,
        required_margin: float,
        equity_usdt: float,
        max_used_margin_pct: float,
        max_active_signals: int,
        max_active_per_symbol: int,
    ) -> ExposureGuardResult:
        active = self.active_signals(db, bot_id)
        active_for_symbol = self.active_signals_for_symbol(db, bot_id, symbol)

        active_count = len(active)
        symbol_active_count = len(active_for_symbol)

        used_margin = self.used_margin(db, bot_id)
        max_allowed_margin = round(equity_usdt * max_used_margin_pct, 6)
        free_margin = round(max_allowed_margin - used_margin, 6)

        base = {
            "active_signals_count": active_count,
            "active_symbol_signals_count": symbol_active_count,
            "used_margin": used_margin,
            "max_allowed_margin": max_allowed_margin,
            "free_margin": free_margin,
            "required_margin": round(float(required_margin or 0), 6),
        }

        if symbol_active_count >= max_active_per_symbol:
            return ExposureGuardResult(
                allowed=False,
                reason="active_signal_already_exists",
                **base,
            )

        if active_count >= max_active_signals:
            return ExposureGuardResult(
                allowed=False,
                reason="max_active_signals_reached",
                **base,
            )

        if required_margin > free_margin:
            return ExposureGuardResult(
                allowed=False,
                reason="required_margin_exceeds_free_margin",
                **base,
            )

        return ExposureGuardResult(
            allowed=True,
            reason="ok",
            **base,
        )