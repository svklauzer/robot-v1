from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal
from services.phantom_fill import phantom_adjustment, summarize as summarize_phantom


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

    # (#phantom-fill-2026-07-25) Детектор вынесен в services/phantom_fill.py —
    # один источник правды для validation-gates, /analytics/summary и отчётов.
    _phantom_adjustment = staticmethod(phantom_adjustment)

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

        # (#phantom-fill-2026-07-25) Честный PnL: снимаем завышение фантомных филлов.
        phantom = summarize_phantom(signals)
        phantom_count = phantom["phantom_fill_count"]
        phantom_delta = phantom["phantom_fill_delta_usdt"]
        phantom_signal_ids = phantom["phantom_fill_signal_ids"]
        net_pnl_honest = round(net_pnl + phantom_delta, 6)

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
            # Гейт судит по ЧЕСТНОМУ PnL: фантомные филлы не должны открывать live.
            "rolling_net_pnl_positive": net_pnl_honest > 0,
            "no_phantom_fills_in_sample": phantom_count == 0,
            "failed_setup_below_threshold": failed_setup_share < self.failed_setup_max_pct if closed_count else False,
            "positive_then_negative_below_threshold": (
                positive_then_negative_rate < self.positive_then_negative_max_pct if lifecycle_count else False
            ),
            "min_closed_outcomes": closed_count >= self.min_closed,
        }
        blockers: list[str] = []
        if not gates["rolling_net_pnl_positive"]:
            blockers.append("validation rolling net PnL is not positive after costs (honest fills)")
        if not gates["no_phantom_fills_in_sample"]:
            blockers.append(
                f"sample contains {phantom_count} phantom-fill outcomes (result_pct > mfe_pct): "
                "PnL is fiction until they roll out of the window"
            )
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
            "net_pnl_honest_usdt": net_pnl_honest,
            "phantom_fill_count": phantom_count,
            "phantom_fill_overstatement_usdt": round(abs(phantom_delta), 6),
            "phantom_fill_signal_ids": phantom_signal_ids,
            "failed_setup_count": failed_setup_count,
            "failed_setup_share_pct": failed_setup_share,
            "failed_setup_max_pct": self.failed_setup_max_pct,
            "lifecycle_count": lifecycle_count,
            "positive_then_negative_count": positive_then_negative_count,
            "positive_then_negative_rate_pct": positive_then_negative_rate,
            "positive_then_negative_max_pct": self.positive_then_negative_max_pct,
            "gates": gates,
        }

    def live_blockers(self, db: Session, limit: int | None = None) -> dict[str, Any]:
        """Return validation readiness only when live trading is enabled.

        Paper mode can keep collecting data, but live/live_limited must not start
        or run while the profit gates fail.
        """
        state = self.evaluate(db, limit=limit)
        state["live_enabled"] = bool(settings.is_live_enabled)
        state["enforced"] = bool(settings.is_live_enabled)
        state["live_blockers"] = state.get("blockers", []) if settings.is_live_enabled else []
        return state
