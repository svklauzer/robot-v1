from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal
from services.market_data import MarketDataService


class LiveShadowDriftService:
    """Compare paper signal levels with executable live-shadow bid/ask snapshots.

    The service does not place orders. It estimates a worst-case executable entry
    from current bid/ask plus configured slippage and reports whether paper
    signals would still be executable inside the allowed live-shadow drift.
    """

    ACTIVE_STATUSES = {"published", "opened", "queued"}

    def __init__(self, market_data: MarketDataService | None = None):
        self.market_data = market_data or MarketDataService()

    def report(self, db: Session, limit: int = 20, max_drift_pct: float | None = None) -> dict[str, Any]:
        checked_at = datetime.now(timezone.utc).isoformat()
        limit = min(max(int(limit or 20), 1), 100)
        threshold = float(
            max_drift_pct
            if max_drift_pct is not None
            else getattr(settings, "LIVE_SHADOW_MAX_ENTRY_DRIFT_PCT", 0.35)
        )

        signals = (
            db.query(Signal)
            .filter(Signal.status.in_(self.ACTIVE_STATUSES))
            .order_by(Signal.id.desc())
            .limit(limit)
            .all()
        )

        items = [self.evaluate_signal(signal, threshold) for signal in signals]
        degraded = [item for item in items if not item.get("ok")]

        return {
            "status": "ok" if not degraded else "degraded",
            "checked_at": checked_at,
            "limit": limit,
            "max_entry_drift_pct": threshold,
            "signals_checked": len(items),
            "drift_count": len(degraded),
            "blockers": ["live-shadow entry drift exceeds threshold"] if degraded else [],
            "items": items,
        }

    def evaluate_signal(self, signal: Signal, max_drift_pct: float | None = None) -> dict[str, Any]:
        threshold = float(max_drift_pct if max_drift_pct is not None else getattr(settings, "LIVE_SHADOW_MAX_ENTRY_DRIFT_PCT", 0.35))
        side = str(signal.side or "").lower()
        entry_from = float((signal.entry_zone_json or {}).get("from") or 0.0)
        entry_to = float((signal.entry_zone_json or {}).get("to") or entry_from)
        planned_entry = round((entry_from + entry_to) / 2, 8) if entry_from or entry_to else 0.0

        try:
            snapshot = self.market_data.snapshot(signal.symbol)
            executable_entry = self._worst_case_entry(snapshot=snapshot, side=side)
            drift_pct = self._entry_drift_pct(side=side, planned_entry=planned_entry, executable_entry=executable_entry)
            ok = abs(drift_pct) <= threshold
            return {
                "signal_id": signal.id,
                "symbol": signal.symbol,
                "side": side,
                "status": signal.status,
                "planned_entry": planned_entry,
                "executable_entry": round(executable_entry, 8),
                "entry_drift_pct": round(drift_pct, 4),
                "max_entry_drift_pct": threshold,
                "ok": ok,
                "source": snapshot.get("source"),
                "bid": snapshot.get("bid"),
                "ask": snapshot.get("ask"),
                "reason": "ok" if ok else "entry_drift_exceeds_threshold",
            }
        except Exception as exc:
            return {
                "signal_id": signal.id,
                "symbol": signal.symbol,
                "side": side,
                "status": signal.status,
                "planned_entry": planned_entry,
                "ok": False,
                "reason": "market_snapshot_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _worst_case_entry(self, snapshot: dict[str, Any], side: str) -> float:
        slippage_pct = max(float(getattr(settings, "LIVE_SHADOW_SLIPPAGE_PCT", 0.10)), 0.0) / 100.0
        last = float(snapshot.get("last") or 0.0)
        bid = float(snapshot.get("bid") or last)
        ask = float(snapshot.get("ask") or last)
        if side == "short":
            return bid * (1 - slippage_pct)
        return ask * (1 + slippage_pct)

    def _entry_drift_pct(self, side: str, planned_entry: float, executable_entry: float) -> float:
        if planned_entry <= 0:
            return 0.0
        if side == "short":
            return (planned_entry - executable_entry) / planned_entry * 100.0
        return (executable_entry - planned_entry) / planned_entry * 100.0
