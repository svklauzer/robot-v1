from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal


class ValidationGateService:
    """Profit-first paper/live-shadow validation gates for go-live readiness."""

    def __init__(
        self,
        *,
        min_closed: int | None = None,
        failed_setup_max_pct: float | None = None,
        positive_then_negative_max_pct: float | None = None,
    ):
        self.min_closed = int(min_closed or getattr(settings, "VALIDATION_MIN_CLOSED_SIGNALS", 200))
        self.failed_setup_max_pct = float(
            failed_setup_max_pct if failed_setup_max_pct is not None else getattr(settings, "VALIDATION_FAILED_SETUP_MAX_PCT", 35.0)
        )
        self.positive_then_negative_max_pct = float(
            positive_then_negative_max_pct
            if positive_then_negative_max_pct is not None
            else getattr(settings, "VALIDATION_POSITIVE_THEN_NEGATIVE_MAX_PCT", 25.0)
        )

    def evaluate(self, db: Session, limit: int | None = None) -> dict[str, Any]:
        sample_limit = max(int(limit or self.min_closed), self.min_closed)
        signals = (
            db.query(Signal)
            .filter(Signal.status == "closed")
            .order_by(Signal.id.desc())
            .limit(sample_limit)
            .all()
        )
        closed_count = len(signals)
        net_pnl = round(sum(float(signal.closed_net_pnl or 0.0) for signal in signals), 6)
        failed_setup_count = sum(1 for signal in signals if signal.closed_reason == "failed_setup_exit")
        failed_setup_share = round((failed_setup_count / closed_count * 100), 2) if closed_count else 0.0

        lifecycle_rows = []
        for signal in signals:
            lifecycle = (signal.plan_json or {}).get("lifecycle") or {}
            if lifecycle:
                lifecycle_rows.append(lifecycle)
        lifecycle_count = len(lifecycle_rows)
        positive_then_negative_count = sum(1 for row in lifecycle_rows if row.get("positive_then_negative"))
        positive_then_negative_rate = round((positive_then_negative_count / lifecycle_count * 100), 2) if lifecycle_count else 0.0

        gates = {
            "rolling_net_pnl_positive": net_pnl > 0,
            "failed_setup_below_threshold": failed_setup_share < self.failed_setup_max_pct if closed_count else False,
            "positive_then_negative_below_threshold": (
                positive_then_negative_rate < self.positive_then_negative_max_pct if lifecycle_count else False
            ),
            "min_closed_outcomes": closed_count >= self.min_closed,
        }
        blockers: list[str] = []
        if not gates["rolling_net_pnl_positive"]:
            blockers.append("validation rolling net PnL is not positive after costs")
        if not gates["failed_setup_below_threshold"]:
            blockers.append("validation failed_setup_exit share is above threshold")
        if not gates["positive_then_negative_below_threshold"]:
            blockers.append("validation positive_then_negative rate is above threshold or missing lifecycle sample")
        if not gates["min_closed_outcomes"]:
            blockers.append("validation requires at least 200 closed paper/live_shadow outcomes")

        return {
            "ready": not blockers,
            "blockers": blockers,
            "sample_limit": sample_limit,
            "closed_count": closed_count,
            "min_closed": self.min_closed,
            "net_pnl_usdt": net_pnl,
            "failed_setup_count": failed_setup_count,
            "failed_setup_share_pct": failed_setup_share,
            "failed_setup_max_pct": self.failed_setup_max_pct,
            "lifecycle_count": lifecycle_count,
            "positive_then_negative_count": positive_then_negative_count,
            "positive_then_negative_rate_pct": positive_then_negative_rate,
            "positive_then_negative_max_pct": self.positive_then_negative_max_pct,
            "gates": gates,
        }
