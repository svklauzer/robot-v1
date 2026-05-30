from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.payment import Payment, PaymentEvent
from services.billing_service import BillingService
from services.payment_reconciliation import PaymentReconciliationService


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[Payment.__table__, PaymentEvent.__table__])
    return sessionmaker(bind=engine)()


def _payment(status: str, created_at: datetime, telegram_user_id: str = "1"):
    return Payment(
        telegram_user_id=telegram_user_id,
        plan_code="vip_30",
        amount=49.0,
        currency="USDT",
        duration_days=30,
        provider="manual",
        provider_payment_id=f"manual-{telegram_user_id}-{int(created_at.timestamp())}",
        status=status,
        created_at=created_at,
    )


def test_reconciliation_expires_stale_pending_and_records_event():
    db = _db_session()
    now = datetime.now(timezone.utc)

    try:
        stale = _payment("pending", now - timedelta(hours=72), telegram_user_id="old")
        fresh = _payment("pending", now - timedelta(hours=2), telegram_user_id="fresh")
        paid = _payment("paid", now - timedelta(hours=72), telegram_user_id="paid")
        db.add_all([stale, fresh, paid])
        db.commit()

        result = PaymentReconciliationService().reconcile_pending(db, older_than_hours=48)
        db.commit()
        db.refresh(stale)
        db.refresh(fresh)
        db.refresh(paid)

        assert result["status"] == "ok"
        assert result["pending_scanned"] == 2
        assert result["expired"] == 1
        assert result["expired_payment_ids"] == [stale.id]
        assert stale.status == "expired"
        assert "auto_expired_pending_checkout" in stale.notes
        assert fresh.status == "pending"
        assert paid.status == "paid"

        event = db.query(PaymentEvent).one()
        assert event.payment_id == stale.id
        assert event.event_type == "payment_reconciliation"
        assert event.status == "expired"
    finally:
        db.close()


def test_billing_summary_counts_expired_as_failed_not_pending():
    db = _db_session()
    now = datetime.now(timezone.utc)

    try:
        db.add(_payment("expired", now - timedelta(hours=72), telegram_user_id="expired"))
        db.add(_payment("pending", now, telegram_user_id="pending"))
        db.commit()

        summary = BillingService().summary(db)

        assert summary["pending"] == 1
        assert summary["failed"] == 1
    finally:
        db.close()
