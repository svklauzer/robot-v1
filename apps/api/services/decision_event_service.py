# apps/api/services/decision_event_service.py

from models.intelligence_event import IntelligenceEvent


class DecisionEventService:
    def record(
        self,
        db,
        *,
        symbol: str,
        status: str,
        decision: str,
        action: str | None = None,
        regime: str | None = None,
        radar_state: str | None = None,
        confidence_hint: float | None = None,
        setup_score: float | None = None,
        payload: dict | None = None,
    ) -> IntelligenceEvent:
        event = IntelligenceEvent(
            symbol=symbol,
            status=status,
            decision=decision,
            action=action,
            regime=regime,
            radar_state=radar_state,
            confidence_hint=confidence_hint,
            setup_score=setup_score,
            payload_json=payload or {},
        )

        db.add(event)
        db.flush()

        return event