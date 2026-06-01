from sqlalchemy.orm import Session

from models.audit_event import AuditEvent


class AuditLogService:
    def record(
        self,
        db: Session,
        action: str,
        resource_type: str | None = None,
        resource_id: str | int | None = None,
        status: str = "ok",
        actor: str = "owner",
        details: dict | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            status=status,
            details_json=details or {},
        )
        db.add(event)
        db.flush()
        return event

    def serialize(self, event: AuditEvent) -> dict:
        return {
            "id": event.id,
            "actor": event.actor,
            "action": event.action,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "status": event.status,
            "details": event.details_json or {},
            "created_at": str(event.created_at),
        }
