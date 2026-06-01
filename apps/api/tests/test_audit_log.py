from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.audit_event import AuditEvent
from services.audit_log import AuditLogService


def test_audit_log_records_owner_action():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[AuditEvent.__table__])
    db = sessionmaker(bind=engine)()

    try:
        service = AuditLogService()
        event = service.record(
            db,
            action="bot_stop",
            resource_type="bot",
            resource_id=1,
            details={"reason": "test"},
        )
        db.commit()

        saved = db.query(AuditEvent).one()
        payload = service.serialize(saved)

        assert saved.id == event.id
        assert payload["action"] == "bot_stop"
        assert payload["resource_type"] == "bot"
        assert payload["resource_id"] == "1"
        assert payload["details"]["reason"] == "test"
    finally:
        db.close()
