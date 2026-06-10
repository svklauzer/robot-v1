from fastapi import APIRouter, Depends
from core.db import SessionLocal
from core.security import require_owner_action
from services.audit_log import AuditLogService

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/events", dependencies=[Depends(require_owner_action)])
def list_audit_events(limit: int = 100, action: str | None = None):
    db = SessionLocal()
    try:
        return AuditLogService().list_events(db, limit=limit, action=action)
    finally:
        db.close()
